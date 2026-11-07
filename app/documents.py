"""Local extraction and chunking. Scanned PDFs require a future OCR stage."""
import json
import os
import subprocess
import sys
from threading import BoundedSemaphore
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from pypdf import PdfReader
from fastapi import HTTPException
from .db import connection

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
MAX_TEXT = 2_000_000
CHUNK_SIZE = 1200
OVERLAP = 200


def _extract_pages(filename: str, data: bytes) -> list[str]:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".pdf":
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise HTTPException(422, "Encrypted PDFs are not supported")
            if len(reader.pages) > MAX_PAGES:
                raise HTTPException(413, "PDF exceeds 100 pages")
            pages = []
            total = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                total += len(text)
                if total > MAX_TEXT:
                    raise HTTPException(413, "Extracted text is too large")
                pages.append(text)
        elif suffix in {".txt", ".md"}:
            pages = [data.decode("utf-8")]
        else:
            raise HTTPException(415, "Use .pdf, .txt, or .md")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Unable to read document") from None
    if sum(map(len, pages)) > MAX_TEXT:
        raise HTTPException(413, "Extracted text is too large")
    if not any(page.strip() for page in pages):
        raise HTTPException(422, "No readable text found. Scanned PDFs need OCR.")
    return pages


def store_document(filename: str, visibility: str, pages: list[str]):
    document_id = str(uuid4())
    chunks = []
    for number, page in enumerate(pages, start=1):
        page = page.strip()
        start = 0
        while start < len(page):
            end = min(start + CHUNK_SIZE, len(page))
            chunks.append((str(uuid4()), document_id, number, page[start:end]))
            if end == len(page):
                break
            start = end - OVERLAP
    with connection() as db:
        db.execute("INSERT INTO documents(id,filename,visibility,page_count) VALUES (?,?,?,?)",
                   (document_id, filename, visibility, len(pages)))
        db.executemany("INSERT INTO chunks(id,document_id,page_number,text) VALUES (?,?,?,?)", chunks)
        db.executemany("INSERT INTO chunk_search(chunk_id,text) VALUES (?,?)", [(c[0], c[3]) for c in chunks])
    empty = [i for i, page in enumerate(pages, 1) if not page.strip()]
    warnings = []
    if empty:
        warnings.append(f"{len(empty)} of {len(pages)} pages had no extractable text "
                        "(possibly scanned images). Those pages are not searchable until OCR is added.")
    return {"id": document_id, "filename": filename, "visibility": visibility,
            "page_count": len(pages), "chunk_count": len(chunks),
            "empty_pages": empty, "warnings": warnings}


def delete_document(document_id: str) -> bool:
    """Remove a document, its chunks, and its search rows in one transaction."""
    with connection() as db:
        if not db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
            return False
        db.execute("DELETE FROM chunk_search WHERE chunk_id IN "
                   "(SELECT id FROM chunks WHERE document_id=?)", (document_id,))
        db.execute("DELETE FROM documents WHERE id=?", (document_id,))  # cascades to chunks, collections
    return True


_pdf_slots = BoundedSemaphore(2)


def extract_pages(filename: str, data: bytes) -> list[str]:
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Maximum file size is 10 MiB")
    if Path(filename).suffix.lower() != ".pdf":
        return _extract_pages(filename, data)
    if not _pdf_slots.acquire(blocking=False):
        raise HTTPException(503, "PDF extraction busy; retry later")
    try:
        # Do not pass API credentials or database configuration to the parser.
        env = {k: v for k, v in os.environ.items()
               if k in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}}
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).with_name("pdf_worker.py"))],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=15, env=env, check=False)
        except subprocess.TimeoutExpired:
            raise HTTPException(422, "PDF extraction exceeded time limit") from None
        if result.returncode != 0:
            raise HTTPException(422, "PDF extraction failed or exceeded resource limits")
        output = json.loads(result.stdout)
        if "error" in output:
            raise HTTPException(output["status"], output["error"])
        return output["pages"]
    finally:
        _pdf_slots.release()
