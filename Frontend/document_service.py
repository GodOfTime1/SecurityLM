"""Existing upload and collection APIs, with explicit partial-success handling."""
from api_client import APIError, resource, validate_file


def link_documents(api, assistant_id, new_ids):
    # Fresh read avoids replacing an old cached collection. True compare-and-set
    # protection still requires a versioned/atomic backend collection endpoint.
    assistant = api.request("GET", f"/assistants/{resource(assistant_id)}")
    ids = list(dict.fromkeys(assistant["document_ids"] + list(new_ids)))
    return api.request("PUT", f"/assistants/{resource(assistant_id)}/documents", json={"document_ids": ids})


def upload_batch(api, uploads, visibility, assistant_id=None, on_progress=None):
    # Validate the whole selection before the first write, avoiding avoidable partial uploads.
    if visibility not in {'admin', 'employee'}:
        raise APIError('Choose a supported document visibility.')
    uploads = list(uploads)
    for upload in uploads:
        validate_file(upload.name, upload.getvalue())
    results, ids = [], []
    for index, upload in enumerate(uploads):
        try:
            doc = api.upload(upload.name, upload.getvalue(), visibility)
        except APIError as exc:
            results.append({"name": upload.name, "uploaded": False, "warnings": [str(exc)]})
        else:
            ids.append(doc["id"])
            results.append({"name": upload.name, "id": doc["id"], "uploaded": True, "warnings": doc.get("warnings", [])})
        if on_progress:
            on_progress(index + 1, len(uploads))
    link_error = None
    if ids and assistant_id:
        try:
            link_documents(api, assistant_id, ids)
        except APIError as exc:
            link_error = str(exc)
    return {"files": results, "uploaded_ids": ids, "assistant_id": assistant_id,
            "linked": bool(ids and assistant_id and not link_error), "link_error": link_error}
