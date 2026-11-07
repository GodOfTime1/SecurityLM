"""Atomic per-identity rate buckets and logical storage quotas in SQLite."""
import time
from fastapi import HTTPException
from .db import connection
from .deployment import production, setting


def rate_limit(user_id, chat=False):
    if not production():
        return
    now = int(time.time() // 60)
    buckets = [('requests', setting('USER_REQUESTS_PER_MINUTE',60))]
    if chat:
        buckets.append(('chat', setting('CHAT_REQUESTS_PER_MINUTE',10)))
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        # Keep recent windows so a delayed request cannot delete a newer bucket.
        db.execute('DELETE FROM request_rates WHERE window<?', (now - 2,))
        for kind, limit in buckets:
            row = db.execute('SELECT count FROM request_rates WHERE user_id=? AND kind=? AND window=?',
                             (user_id, kind, now)).fetchone()
            if row and row['count'] >= limit:
                raise HTTPException(429, 'Request rate limit reached', headers={'Retry-After': str(60-int(time.time())%60)})
        for kind, _ in buckets:
            db.execute('''INSERT INTO request_rates(user_id,kind,window,count) VALUES (?,?,?,1)
                ON CONFLICT(user_id,kind,window) DO UPDATE SET count=count+1''', (user_id,kind,now))


def cap(count, name, default):
    if production() and count >= setting(name, default):
        raise HTTPException(429, 'Demo quota reached; remove unused records or ask the administrator')


def document_quota(db, chunks, replacing=None):
    if not production():
        return
    if replacing is None:
        cap(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 'MAX_DOCUMENTS',100)
    current = db.execute('SELECT COALESCE(SUM(length(CAST(text AS BLOB))),0) FROM chunks WHERE document_id<>?',
                         (replacing or '',)).fetchone()[0]
    size = sum(len(c[3].encode('utf-8')) for c in chunks)
    if current + size > setting('MAX_DOCUMENT_TEXT_BYTES',20*1024*1024):
        raise HTTPException(413, 'Demo document storage quota reached')
