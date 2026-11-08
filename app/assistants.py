"""Assistant configuration and document collections."""
from uuid import uuid4
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from .auth import User, admin_user, current_user
from .db import connection
from .retrieval import search_documents

router = APIRouter(prefix="/assistants", tags=["assistants"])


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantIn(StrictInput):
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=100)
    system_prompt: str = Field(default="", max_length=8000)


class AssistantPatch(StrictInput):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    purpose: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=100)
    system_prompt: str | None = Field(default=None, max_length=8000)


class DocumentIds(StrictInput):
    document_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(max_length=200)


class AssistantSearch(StrictInput):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


def _view(row, user: User, doc_ids: list[str]):
    out = {k: row[k] for k in ("id", "name", "purpose", "model", "created_at", "updated_at")}
    out["document_ids"] = doc_ids
    if user.role == "admin":  # employees never see the system prompt
        out["system_prompt"] = row["system_prompt"]
    return out


def _load(db, assistant_id: str, user: User):
    row = db.execute("SELECT * FROM assistants WHERE id=?", (assistant_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Assistant not found")
    # Employees only see ids of documents they are allowed to read.
    docs = db.execute("""SELECT d.id FROM assistant_documents ad JOIN documents d ON d.id=ad.document_id
        WHERE ad.assistant_id=? AND (d.visibility='employee' OR ?='admin') ORDER BY d.created_at,d.id""",
                      (assistant_id, user.role)).fetchall()
    return _view(row, user, [d["id"] for d in docs])


@router.post("", status_code=201)
def create_assistant(body: AssistantIn, user: User = Depends(admin_user)):
    assistant_id = str(uuid4())
    with connection() as db:
        db.execute("INSERT INTO assistants(id,name,purpose,model,system_prompt,created_by) VALUES (?,?,?,?,?,?)",
                   (assistant_id, body.name, body.purpose, body.model, body.system_prompt, user.id))
        return _load(db, assistant_id, user)


@router.get("")
def list_assistants(user: User = Depends(current_user),
                    limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
    with connection() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM assistants ORDER BY created_at,id LIMIT ? OFFSET ?", (limit, offset))]
        return {"assistants": [_load(db, i, user) for i in ids]}


@router.get("/{assistant_id}")
def get_assistant(assistant_id: str, user: User = Depends(current_user)):
    with connection() as db:
        return _load(db, assistant_id, user)


@router.patch("/{assistant_id}")
def update_assistant(assistant_id: str, body: AssistantPatch, user: User = Depends(admin_user)):
    changes = body.model_dump(exclude_none=True)
    with connection() as db:
        _load(db, assistant_id, user)  # 404 if missing
        if changes:
            sets = ", ".join(f"{k}=?" for k in changes)  # keys come from the model, never from the client
            db.execute(f"UPDATE assistants SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (*changes.values(), assistant_id))
        return _load(db, assistant_id, user)


@router.delete("/{assistant_id}", status_code=204)
def delete_assistant(assistant_id: str, user: User = Depends(admin_user)):
    with connection() as db:
        if db.execute("DELETE FROM assistants WHERE id=?", (assistant_id,)).rowcount == 0:
            raise HTTPException(404, "Assistant not found")


@router.put("/{assistant_id}/documents")
def set_collection(assistant_id: str, body: DocumentIds, user: User = Depends(admin_user)):
    ids = list(dict.fromkeys(body.document_ids))
    with connection() as db:
        _load(db, assistant_id, user)
        found = {r["id"] for r in db.execute(
            f"SELECT id FROM documents WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        missing = [i for i in ids if i not in found]
        if missing:
            raise HTTPException(422, f"Unknown document ids: {missing}")
        db.execute("DELETE FROM assistant_documents WHERE assistant_id=?", (assistant_id,))
        db.executemany("INSERT INTO assistant_documents(assistant_id,document_id) VALUES (?,?)",
                       [(assistant_id, i) for i in ids])
        db.execute("UPDATE assistants SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (assistant_id,))
        return _load(db, assistant_id, user)


@router.post("/{assistant_id}/search")
def search_collection(assistant_id: str, body: AssistantSearch, user: User = Depends(current_user)):
    """Search only this assistant's documents, still filtered by the caller's role."""
    with connection() as db:
        _load(db, assistant_id, user)
    return {"results": search_documents(user, body.query, body.limit, assistant_id=assistant_id)}
