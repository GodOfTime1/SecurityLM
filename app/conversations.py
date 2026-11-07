"""User-owned conversations. Every query is scoped to the caller; other users get 404."""
import json
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .actions import action_view
from .auth import User, current_user
from .db import connection
from .provenance import message_accessible

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assistant_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=200)


def owned_conversation(db, conversation_id: str, user: User):
    row = db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user.id)).fetchone()
    if not row:
        raise HTTPException(404, "Conversation not found")
    return row


def conversation_view(row) -> dict:
    return {k: row[k] for k in ("id", "assistant_id", "title", "created_at", "updated_at")}


def resolve_citations(db, citations: list[dict], user: User) -> list[dict]:
    """Re-check access every time a conversation is read. If a source was deleted or is no longer
    visible to this user, the citation stays but its text and filename are withheld."""
    out = []
    for c in citations:
        row = db.execute("""SELECT c.id AS chunk_id, d.id AS document_id, d.filename, c.page_number, c.text
            FROM chunks c JOIN documents d ON d.id=c.document_id
            WHERE c.id=? AND (d.visibility='employee' OR ?='admin')""", (c["chunk_id"], user.role)).fetchone()
        out.append({"n": c["n"], **dict(row), "available": True} if row
                   else {"n": c["n"], "chunk_id": c["chunk_id"], "available": False})
    return out


def message_view(db, row, user: User) -> dict:
    view = {"id": row["id"], "role": row["role"], "content": row["content"], "created_at": row["created_at"],
            "citations": [], "actions": [], "tool_calls": []}
    if row["role"] == "assistant":
        conv = db.execute("SELECT assistant_id FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
        if not conv or not message_accessible(db, row, user, conv["assistant_id"]):
            # Redact derived answer, proposals, and traces as well as citation text.
            view.update(content="[Answer withheld: a source is unavailable or its access changed.]", redacted=True)
            return view
        view["citations"] = resolve_citations(db, json.loads(row["citations"]), user)
        view["actions"] = [action_view(a) for a in db.execute(
            "SELECT * FROM actions WHERE message_id=? ORDER BY rowid", (row["id"],))]
        view["tool_calls"] = [{"tool": t["tool"], "status": t["status"], "arguments": json.loads(t["arguments"]),
                               "result": json.loads(t["result"]), "created_at": t["created_at"]}
                              for t in db.execute("SELECT * FROM tool_calls WHERE message_id=? ORDER BY rowid", (row["id"],))]
    return view


@router.post("", status_code=201)
def create_conversation(body: ConversationIn, user: User = Depends(current_user)):
    conversation_id = str(uuid4())
    with connection() as db:
        if not db.execute("SELECT 1 FROM assistants WHERE id=?", (body.assistant_id,)).fetchone():
            raise HTTPException(404, "Assistant not found")
        db.execute("INSERT INTO conversations(id,user_id,assistant_id,title) VALUES (?,?,?,?)",
                   (conversation_id, user.id, body.assistant_id, body.title))
        return conversation_view(owned_conversation(db, conversation_id, user))


@router.get("")
def list_conversations(user: User = Depends(current_user),
                       limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    with connection() as db:
        rows = db.execute("SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                          (user.id, limit, offset)).fetchall()
    return {"conversations": [conversation_view(r) for r in rows]}


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, user: User = Depends(current_user),
                     limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    with connection() as db:
        conv = owned_conversation(db, conversation_id, user)
        rows = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid LIMIT ? OFFSET ?",
                          (conversation_id, limit, offset)).fetchall()
        return {**conversation_view(conv), "messages": [message_view(db, r, user) for r in rows]}


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, user: User = Depends(current_user)):
    with connection() as db:
        owned_conversation(db, conversation_id, user)
        db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
