"""Proposed actions: editable drafts, version-bound approval, single execution."""
import json
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .auth import User, current_user
from .db import connection
from .tools import TOOLS, validate_args
from .provenance import message_accessible

router = APIRouter(prefix="/actions", tags=["actions"])


class ToolRejected(Exception):
    pass


class ActionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict
    version: int = Field(ge=1)


class ActionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


def action_view(row) -> dict:
    return {"id": row["id"], "conversation_id": row["conversation_id"], "message_id": row["message_id"],
            "tool": row["tool"], "arguments": json.loads(row["arguments"]), "version": row["version"],
            "status": row["status"], "result": json.loads(row["result"]) if row["result"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def create_draft(db, *, conversation_id: str, message_id: str, assistant_id: str, tool, arguments) -> str:
    """Validate a model proposal and store it as a draft. Raises ToolRejected with a safe reason."""
    if not isinstance(tool, str) or tool not in TOOLS:
        raise ToolRejected("Unknown tool")
    if not db.execute("SELECT 1 FROM assistant_tools WHERE assistant_id=? AND tool=?", (assistant_id, tool)).fetchone():
        raise ToolRejected("Tool is not enabled for this assistant")
    try:
        clean = validate_args(tool, arguments)
    except ValueError as exc:
        raise ToolRejected(str(exc)) from None
    action_id = str(uuid4())
    db.execute("INSERT INTO actions(id,conversation_id,message_id,tool,arguments,status) VALUES (?,?,?,?,?,'draft')",
               (action_id, conversation_id, message_id, tool, json.dumps(clean)))
    return action_id


def _owned(db, action_id: str, user: User):
    row = db.execute("""SELECT a.* FROM actions a JOIN conversations c ON c.id=a.conversation_id
                        WHERE a.id=? AND c.user_id=?""", (action_id, user.id)).fetchone()
    if not row:
        raise HTTPException(404, "Action not found")
    message = db.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    assistant = db.execute("SELECT assistant_id FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
    if not message or not assistant or not message_accessible(db, message, user, assistant["assistant_id"], require_current=True):
        raise HTTPException(404, "Action not found")
    return row


@router.get("/{action_id}")
def get_action(action_id: str, user: User = Depends(current_user)):
    with connection() as db:
        return action_view(_owned(db, action_id, user))


@router.patch("/{action_id}")
def edit_action(action_id: str, body: ActionEdit, user: User = Depends(current_user)):
    with connection() as db:
        row = _owned(db, action_id, user)
        try:
            clean = validate_args(row["tool"], body.arguments)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        changed = db.execute("""UPDATE actions SET arguments=?, version=version+1, updated_at=CURRENT_TIMESTAMP
                                WHERE id=? AND status='draft' AND version=?""",
                             (json.dumps(clean), action_id, body.version)).rowcount
        if not changed:
            raise HTTPException(409, "Action was changed or is no longer a draft. Reload it.")
        return action_view(_owned(db, action_id, user))


@router.post("/{action_id}/cancel")
def cancel_action(action_id: str, user: User = Depends(current_user)):
    with connection() as db:
        _owned(db, action_id, user)
        if not db.execute("UPDATE actions SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
                          "WHERE id=? AND status='draft'", (action_id,)).rowcount:
            raise HTTPException(409, "Only drafts can be cancelled")
        return action_view(_owned(db, action_id, user))


@router.post("/{action_id}/approve")
def approve_action(action_id: str, body: ActionVersion, user: User = Depends(current_user)):
    """Approve the exact version the user saw. The draft->executing step is atomic, so a double
    click or retry cannot run the action twice, and an edit after review invalidates the approval."""
    # Local SQLite tools only: keep ownership, provenance, current arguments,
    # tool permission, execution, and status in one write transaction.
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _owned(db, action_id, user)
        if row["status"] != "draft" or row["version"] != body.version:
            raise HTTPException(409, "Action was changed, cancelled, or already approved. Reload it.")
        if row["tool"] not in TOOLS or not db.execute(
            """SELECT 1 FROM conversations c JOIN assistant_tools t ON t.assistant_id=c.assistant_id
               WHERE c.id=? AND t.tool=?""", (row["conversation_id"], row["tool"])).fetchone():
            raise HTTPException(403, "This tool is no longer enabled for the assistant")
        db.execute("UPDATE actions SET status='executing' WHERE id=?", (action_id,))
        db.execute("SAVEPOINT tool_effects")
        try:
            result = TOOLS[row["tool"]].execute(validate_args(row["tool"], json.loads(row["arguments"])), user, action_id, db)
            db.execute("RELEASE tool_effects")
            status = "executed"
        except Exception:
            db.execute("ROLLBACK TO tool_effects")
            db.execute("RELEASE tool_effects")
            status, result = "failed", {"error": "Execution failed"}
        db.execute("UPDATE actions SET status=?, result=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (status, json.dumps(result), action_id))
        return action_view(_owned(db, action_id, user))
