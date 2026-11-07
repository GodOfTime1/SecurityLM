"""SQLite persistence. One fictional organization for this prototype."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connection():
    path = Path(os.environ.get("DATABASE_PATH", "runtime/company.db"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Restrict newly created DBs on POSIX; tighten an existing DB as well.
    path.touch(mode=0o600, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o600)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('employee', 'admin')),
            page_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assistants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            system_prompt TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assistant_documents (
            assistant_id TEXT NOT NULL REFERENCES assistants(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            PRIMARY KEY (assistant_id, document_id)
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL REFERENCES assistants(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            citations TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
        CREATE TABLE IF NOT EXISTS tool_calls (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            tool TEXT NOT NULL,
            arguments TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK(status IN ('ok', 'rejected', 'error')),
            result TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assistant_tools (
            assistant_id TEXT NOT NULL REFERENCES assistants(id) ON DELETE CASCADE,
            tool TEXT NOT NULL,
            PRIMARY KEY (assistant_id, tool)
        );
        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            tool TEXT NOT NULL,
            arguments TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL CHECK(status IN ('draft', 'executing', 'executed', 'failed', 'cancelled')),
            result TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            severity TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(chunk_id UNINDEXED, text);
        """)

        db.execute("BEGIN IMMEDIATE")
        # NULL means legacy/unverified provenance; never guess it from citations.
        columns = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
        if "source_chunks" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN source_chunks TEXT")
        for table, column, definition in (
            ('documents', 'version', 'INTEGER NOT NULL DEFAULT 1'),
            ('conversations', 'revision', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            if column not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_requests (
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
            token TEXT,
            lease_until REAL NOT NULL DEFAULT 0,
            user_message_id TEXT,
            assistant_message_id TEXT,
            revision INTEGER,
            PRIMARY KEY(conversation_id,request_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_turn
            ON chat_requests(conversation_id) WHERE status='pending';
        """)

        db.execute("""CREATE TABLE IF NOT EXISTS request_rates (
            user_id TEXT NOT NULL, kind TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL,
            PRIMARY KEY(user_id,kind,window))""")
