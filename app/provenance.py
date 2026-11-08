"""Fail closed when stored model content depends on a withdrawn source."""
import json

MAX_SOURCES = 256


def message_sources(row):
    try:
        sources = json.loads(row['source_chunks'])
        if (not isinstance(sources, list) or len(sources) > MAX_SOURCES
                or any(not isinstance(s, str) or not 1 <= len(s) <= 100 for s in sources)):
            return None
        return sources
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def sources_accessible(db, sources, user, assistant_id):
    if sources is None or len(sources) > MAX_SOURCES:
        return False
    if not sources:
        return True
    ids = list(set(sources))
    found = db.execute(f'''SELECT COUNT(DISTINCT c.id) FROM chunks c
        JOIN documents d ON d.id=c.document_id
        JOIN assistant_documents ad ON ad.document_id=d.id
        WHERE ad.assistant_id=? AND (d.visibility='employee' OR ?='admin')
          AND c.id IN ({','.join('?' for _ in ids)})''', (assistant_id, user.role, *ids)).fetchone()[0]
    return found == len(ids)


def message_accessible(db, row, user, assistant_id):
    return row['role'] != 'assistant' or sources_accessible(db, message_sources(row), user, assistant_id)
