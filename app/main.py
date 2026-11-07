from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Literal
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .security import SecurityMiddleware
from .deployment import DeploymentMiddleware, validate_deployment
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, ConfigDict
from .auth import User, admin_user, current_user, validate_config
from .db import connection, initialize
from .actions import router as actions_router
from .assistants import router as assistants_router
from .chat import router as chat_router
from .conversations import router as conversations_router
from .tools import router as tools_router
from .documents import MAX_BYTES, delete_document, extract_pages, store_document, replace_document
from .retrieval import search_documents


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    validate_deployment()
    initialize()
    yield


app = FastAPI(title="Private Agent — Document Backend", lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.add_middleware(DeploymentMiddleware)
app.include_router(assistants_router)
app.include_router(conversations_router)
app.include_router(chat_router)
app.include_router(actions_router)
app.include_router(tools_router)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Validation errors normally echo client input, including uploaded text/prompts.
    errors = [{k: e[k] for k in ("type", "loc", "msg") if k in e} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "role": user.role}


@app.post("/documents", status_code=201)
def upload_document(file: UploadFile = File(...),
                    visibility: Literal["employee", "admin"] = Form("admin"),
                    user: User = Depends(admin_user)):
    filename, data = _read_upload(file)
    return store_document(filename, visibility, extract_pages(filename, data))


def _read_upload(file):
    try:
        data = file.file.read(MAX_BYTES + 1)
    finally:
        file.file.close()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Maximum file size is 10 MiB")
    # Names are metadata only; they are never used as storage paths.
    filename = PurePosixPath((file.filename or "document").replace("\\", "/")).name[:200]
    if not filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise HTTPException(422, "Invalid filename")
    return filename, data


@app.put("/documents/{document_id}")
def replace_uploaded_document(document_id: str, file: UploadFile = File(...),
                              expected_version: int = Form(..., ge=1), user: User = Depends(admin_user)):
    # Reject missing/stale targets before spending resources on PDF extraction.
    with connection() as db:
        document = db.execute("SELECT version FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        if document["version"] != expected_version:
            raise HTTPException(409, "Document changed; reload its version before replacing")
    filename, data = _read_upload(file)
    return replace_document(document_id, expected_version, filename, extract_pages(filename, data))


@app.get("/documents")
def list_documents(user: User = Depends(current_user),
                   limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
    with connection() as db:
        rows = db.execute("""SELECT id,filename,visibility,page_count,created_at,version FROM documents
            WHERE visibility='employee' OR ?='admin' ORDER BY created_at,id LIMIT ? OFFSET ?""", (user.role, limit, offset)).fetchall()
    return {"documents": [dict(row) for row in rows]}


@app.delete("/documents/{document_id}", status_code=204)
def remove_document(document_id: str, user: User = Depends(admin_user)):
    if not delete_document(document_id):
        raise HTTPException(404, "Document not found")


@app.get("/documents/{document_id}/chunks")
def document_chunks(document_id: str, limit: int = Query(20, ge=1, le=100),
                    offset: int = Query(0, ge=0), user: User = Depends(current_user)):
    with connection() as db:
        document = db.execute("""SELECT id FROM documents WHERE id=?
            AND (visibility='employee' OR ?='admin')""", (document_id, user.role)).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        rows = db.execute("""SELECT id AS chunk_id, document_id, page_number, text FROM chunks
            WHERE document_id=? ORDER BY page_number,rowid LIMIT ? OFFSET ?""",
                          (document_id, limit, offset)).fetchall()
    return {"chunks": [dict(row) for row in rows], "offset": offset, "limit": limit}


@app.post("/search")
def search(body: SearchRequest, user: User = Depends(current_user)):
    return {"results": search_documents(user, body.query, body.limit)}
