"""Persistent chat idempotency and expiring, fenced per-conversation reservations."""
import hashlib
import json
import time
from dataclasses import dataclass
from uuid import uuid4
from fastapi import HTTPException
from .conversations import message_view, owned_conversation
from .db import connection
from .limits import cap

# Longer than the built-in model client's maximum 120-second total timeout.
# Custom adapters must also return before this lease expires.
LEASE_SECONDS = 180


@dataclass(frozen=True)
class Reservation:
    conversation_id: str
    request_id: str
    token: str
    revision: int


def turn_response(db, row, user, replayed):
    owned_conversation(db, row['conversation_id'], user)
    messages = {r['id']: r for r in db.execute('SELECT * FROM messages WHERE id IN (?,?)',
        (row['user_message_id'], row['assistant_message_id']))}
    if len(messages) != 2:
        raise HTTPException(409, 'Saved turn is unavailable')
    # Re-render with current ACLs and action status; never cache sensitive JSON.
    return {'request_id': row['request_id'], 'revision': row['revision'], 'replayed': replayed,
            'user_message': message_view(db, messages[row['user_message_id']], user),
            'assistant_message': message_view(db, messages[row['assistant_message_id']], user)}


def reserve_turn(conversation_id, request_id, content, expected_revision, user):
    fingerprint = hashlib.sha256(json.dumps([content, expected_revision], ensure_ascii=True).encode()).hexdigest()
    now = time.time()
    token = str(uuid4())
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        conv = owned_conversation(db, conversation_id, user)
        previous = db.execute('SELECT * FROM chat_requests WHERE conversation_id=? AND request_id=?',
                              (conversation_id, request_id)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise HTTPException(409, 'Request ID was already used for a different payload')
            if previous['status'] == 'completed':
                return None, turn_response(db, previous, user, True)
        messages = db.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=?', (conversation_id,)).fetchone()[0]
        # A turn needs room for both a user and an assistant message.
        cap(messages + 1, 'MAX_MESSAGES_PER_CONVERSATION',200)
        if not previous:
            cap(db.execute('SELECT COUNT(*) FROM chat_requests WHERE conversation_id=?', (conversation_id,)).fetchone()[0],
                'MAX_REQUEST_IDS_PER_CONVERSATION',400)
        # A crashed/expired attempt must not block the conversation forever.
        db.execute("UPDATE chat_requests SET status='failed',token=NULL WHERE conversation_id=? "
                   "AND status='pending' AND lease_until<=?", (conversation_id, now))
        if db.execute("SELECT 1 FROM chat_requests WHERE conversation_id=? AND status='pending'",
                      (conversation_id,)).fetchone():
            raise HTTPException(409, 'A message is already being processed; retry later', headers={'Retry-After': '1'})
        if expected_revision is not None and expected_revision != conv['revision']:
            raise HTTPException(409, 'Conversation changed; reload it before sending this message')
        db.execute("""INSERT INTO chat_requests(conversation_id,request_id,fingerprint,status,token,lease_until)
            VALUES (?,?,?,'pending',?,?) ON CONFLICT(conversation_id,request_id) DO UPDATE SET
            status='pending',token=excluded.token,lease_until=excluded.lease_until""",
            (conversation_id, request_id, fingerprint, token, now + LEASE_SECONDS))
        return Reservation(conversation_id, request_id, token, conv['revision']), None


def check_reservation(db, reservation, user):
    conv = owned_conversation(db, reservation.conversation_id, user)
    row = db.execute('SELECT * FROM chat_requests WHERE conversation_id=? AND request_id=?',
                     (reservation.conversation_id, reservation.request_id)).fetchone()
    if (not row or row['status'] != 'pending' or row['token'] != reservation.token
            or row['lease_until'] <= time.time() or conv['revision'] != reservation.revision):
        raise HTTPException(409, 'Message attempt expired or conversation changed; reload and retry')


def complete_turn(db, reservation, user_message_id, assistant_message_id, user):
    # Caller holds BEGIN IMMEDIATE and has checked the reservation in this transaction.
    revision = reservation.revision + 1
    db.execute('UPDATE conversations SET revision=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (revision, reservation.conversation_id))
    db.execute("""UPDATE chat_requests SET status='completed',token=NULL,lease_until=0,
        user_message_id=?,assistant_message_id=?,revision=? WHERE conversation_id=? AND request_id=?""",
        (user_message_id, assistant_message_id, revision, reservation.conversation_id, reservation.request_id))
    row = db.execute('SELECT * FROM chat_requests WHERE conversation_id=? AND request_id=?',
                     (reservation.conversation_id, reservation.request_id)).fetchone()
    return turn_response(db, row, user, False)


def release_turn(reservation):
    with connection() as db:
        # An expired attempt must never release a newer attempt's reservation.
        db.execute("""UPDATE chat_requests SET status='failed',token=NULL WHERE conversation_id=?
            AND request_id=? AND token=? AND status='pending'""",
            (reservation.conversation_id, reservation.request_id, reservation.token))
