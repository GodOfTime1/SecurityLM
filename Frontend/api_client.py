"""Server-side API boundary shared by Streamlit pages. No global user/client cache."""
import ipaddress
import os
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from constants import FILE_TYPES, MAX_UPLOAD_BYTES


class APIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def backend_url():
    url = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        local = host == "localhost"
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parts.username or parts.password or parts.query or parts.fragment
                or parts.path or (parts.scheme != "https" and not (parts.scheme == "http" and local))):
            raise ValueError
        _ = parts.port
    except ValueError:
        raise APIError("BACKEND_URL must be an HTTPS origin, or loopback HTTP for local development.") from None
    return url


def resource(value):
    return quote(str(value), safe="")


def validate_file(filename, data):
    if not filename.lower().endswith(tuple('.'+ext for ext in FILE_TYPES)):
        raise APIError("Use PDF, TXT, or MD files.")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise APIError("Choose a nonempty file no larger than 10 MiB.")


class BackendAPI:
    def __init__(self, key, *, transport=None):
        self.key = key
        self.transport = transport
        self.on_mutation = None

    def request(self, method, path, **kwargs):
        # The browser/user cannot choose a destination for their credential.
        parts = urlsplit(path)
        if not path.startswith('/') or path.startswith('//') or parts.scheme or parts.netloc or '\\' in path:
            raise APIError('API requests must use a relative backend path.')
        try:
            with httpx.Client(base_url=backend_url(), headers={"X-API-Key": self.key},
                              timeout=httpx.Timeout(150, connect=5), trust_env=False,
                              follow_redirects=False, transport=self.transport) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError:
            message = ('The backend did not respond. Refresh and try again.' if method.upper() in {'GET', 'HEAD'}
                       else 'The response was lost; this change may already be saved. Refresh before repeating it.')
            raise APIError(message) from None
        if response.is_redirect:
            raise APIError("Backend redirects are disabled. Check BACKEND_URL.", response.status_code)
        if response.is_error:
            defaults = {401: "Credential rejected. Sign out and reconnect.",
                        403: "You do not have permission for this operation.",
                        404: "This item is unavailable or you no longer have access.",
                        409: "State changed or a request is in progress. Refresh before continuing.",
                        413: "The file, request, or storage quota is too large.",
                        429: "Request limit reached. Wait before retrying.",
                        503: "Backend or model unavailable. Check runtime setup or retry shortly."}
            message = defaults.get(response.status_code, "Backend rejected the request.")
            # Do not echo arbitrary upstream bodies, tracebacks, or submitted secrets.
            if response.status_code in {415, 422}:
                if path.startswith('/model-selector/'):
                    message = "Invalid advisor input. Check your requirements, the 2000-character question limit, and bounded history."
                elif path.startswith('/documents'):
                    message = "Invalid document. Use readable PDF/TXT/MD up to 10 MiB and valid field lengths."
                else:
                    message = "Invalid input. Check the required fields and their limits."
            raise APIError(message, response.status_code)
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and self.on_mutation:
            self.on_mutation()
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise APIError("The backend returned an unexpected response.") from None

    def all_items(self, path, field):
        items = []
        offset = 0
        seen = set()
        while True:
            result = self.request("GET", path, params={"limit": 100, "offset": offset})
            page = result.get(field) if isinstance(result, dict) else None
            if not isinstance(page, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in page):
                raise APIError('The backend returned an unexpected list response.')
            if any(item['id'] in seen for item in page) or len({item['id'] for item in page}) != len(page):
                raise APIError('The list changed while loading. Refresh to load it again.')
            seen.update(item['id'] for item in page)
            items.extend(page)
            if len(page) < 100:
                return items
            offset += len(page)

    def conversation(self, conversation_id):
        # Backend allows up to 200 messages per conversation.
        return self.request("GET", f"/conversations/{resource(conversation_id)}", params={"limit": 200})

    def upload(self, filename, data, visibility):
        validate_file(filename, data)
        return self.request("POST", "/documents", files={"file": (filename, data)}, data={"visibility": visibility})


def new_turn(content, revision):
    return {"content": content, "request_id": str(uuid4()), "expected_revision": revision}
