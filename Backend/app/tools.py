"""Tool registry. Backend code, not the model, decides what exists, what is allowed, and what runs."""
from dataclasses import dataclass
from typing import Annotated, Callable, Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .auth import User, admin_user, current_user
from .db import connection
from .limits import cap


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTicketArgs(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    severity: Literal["low", "medium", "high"] = "medium"


def _create_ticket(args: dict, user: User, action_id: str, db) -> dict:
    # tickets.action_id is UNIQUE, so one approved action can never create two tickets.
    cap(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], "MAX_TICKETS",1000)
    ticket_id = str(uuid4())
    db.execute("INSERT INTO tickets(id,action_id,title,description,severity,created_by) VALUES (?,?,?,?,?,?)",
               (ticket_id, action_id, args["title"], args["description"], args["severity"], user.id))
    return {"ticket_id": ticket_id}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    execute: Callable[[dict, User, str, object], dict]


TOOLS = {"create_ticket": Tool("create_ticket", "Create an IT support ticket.", CreateTicketArgs, _create_ticket)}


def validate_args(tool: str, arguments) -> dict:
    """Return clean arguments or raise ValueError (message lists field names only, never values)."""
    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be an object")
    try:
        return TOOLS[tool].args_model.model_validate(arguments).model_dump()
    except ValueError as exc:
        fields = ", ".join(sorted({".".join(map(str, e["loc"])) or "arguments" for e in getattr(exc, "errors", lambda: [])()}))
        raise ValueError(f"Invalid arguments: {fields or 'check the values'}") from None


router = APIRouter(tags=["tools"])


class ToolNames(StrictModel):
    tools: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(max_length=20)


def _assistant_exists(db, assistant_id: str):
    if not db.execute("SELECT 1 FROM assistants WHERE id=?", (assistant_id,)).fetchone():
        raise HTTPException(404, "Assistant not found")


@router.get("/tools")
def list_tools(user: User = Depends(current_user)):
    return {"tools": [{"name": t.name, "description": t.description, "requires_approval": True,
                       "parameters": t.args_model.model_json_schema()} for t in TOOLS.values()]}


@router.get("/assistants/{assistant_id}/tools")
def get_assistant_tools(assistant_id: str, user: User = Depends(current_user)):
    with connection() as db:
        _assistant_exists(db, assistant_id)
        rows = db.execute("SELECT tool FROM assistant_tools WHERE assistant_id=? ORDER BY tool", (assistant_id,))
        return {"tools": [r["tool"] for r in rows]}


@router.put("/assistants/{assistant_id}/tools")
def set_assistant_tools(assistant_id: str, body: ToolNames, user: User = Depends(admin_user)):
    names = list(dict.fromkeys(body.tools))
    unknown = [n for n in names if n not in TOOLS]
    if unknown:
        raise HTTPException(422, f"Unknown tools: {unknown}")
    with connection() as db:
        _assistant_exists(db, assistant_id)
        db.execute("DELETE FROM assistant_tools WHERE assistant_id=?", (assistant_id,))
        db.executemany("INSERT INTO assistant_tools(assistant_id,tool) VALUES (?,?)", [(assistant_id, n) for n in names])
    return {"tools": sorted(names)}


@router.get("/tickets")
def list_tickets(user: User = Depends(admin_user), limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
    with connection() as db:
        rows = db.execute("SELECT id,action_id,title,description,severity,created_by,created_at FROM tickets "
                          "ORDER BY created_at,id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"tickets": [dict(r) for r in rows]}
