"""Server-side API boundary shared by Streamlit pages. No global user/client cache."""
import ipaddress
import os
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


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


class BackendAPI:
    def __init__(self, key, *, transport=None):
        self.key = key
        self.transport = transport

    def request(self, method, path, **kwargs):
        # The browser/user cannot choose a destination for their credential.
        try:
            with httpx.Client(base_url=backend_url(), headers={"X-API-Key": self.key},
                              timeout=httpx.Timeout(150, connect=5), trust_env=False,
                              follow_redirects=False, transport=self.transport) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise APIError("The backend did not respond. For a chat message, use Retry to keep the same request ID.") from None
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
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise APIError("The backend returned an unexpected response.") from None

    def all_items(self, path, field):
        items = []
        offset = 0
        while True:
            page = self.request("GET", path, params={"limit": 100, "offset": offset})[field]
            items.extend(page)
            if len(page) < 100:
                return items
            offset += len(page)

    def conversation(self, conversation_id):
        # Backend allows up to 200 messages per conversation.
        return self.request("GET", f"/conversations/{resource(conversation_id)}", params={"limit": 200})

    def upload(self, filename, data, visibility):
        if not filename.lower().endswith((".pdf", ".txt", ".md")):
            raise APIError("Use PDF, TXT, or MD files.")
        if not data or len(data) > 10 * 1024 * 1024:
            raise APIError("Choose a nonempty file no larger than 10 MiB.")
        return self.request("POST", "/documents", files={"file": (filename, data)}, data={"visibility": visibility})


def new_turn(content, revision):
    return {"content": content, "request_id": str(uuid4()), "expected_revision": revision}
