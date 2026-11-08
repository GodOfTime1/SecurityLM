"""Server-configured API-key identities. Individual keys are a prototype, not full sessions."""
import json
import os
import re
import secrets
from dataclasses import dataclass
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class User:
    id: str
    role: str


def _configured_users():
    raw = os.environ.get("USER_API_KEYS")
    if os.environ.get("APP_ENV") == "production" and raw is None:
        raise RuntimeError("Production requires individual USER_API_KEYS; shared demo credentials are disabled")
    if raw is None:
        entries = [{"key": os.environ.get(name, ""), "id": f"demo-{role}", "role": role}
                   for role, name in (("admin", "ADMIN_API_KEY"), ("employee", "EMPLOYEE_API_KEY"))]
    else:
        try:
            entries = json.loads(raw)
        except (ValueError, RecursionError):
            raise RuntimeError("Invalid USER_API_KEYS configuration") from None
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise RuntimeError("Configure 1 to 100 individual API-key identities")
    keys, identities, users = set(), set(), []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"key", "id", "role"}:
            raise RuntimeError("Each API-key identity requires key, id, and role")
        key, identity, role = entry['key'], entry['id'], entry['role']
        if (not isinstance(key, str) or not 24 <= len(key) <= 512 or not key.isascii()
                or any(c.isspace() for c in key) or key in keys
                or not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", identity)
                or identity in identities or not isinstance(role, str) or role not in {"admin", "employee"}):
            raise RuntimeError("Configure distinct identities and strong, distinct ASCII API keys")
        if os.environ.get("APP_ENV") == "production" and (len(key) < 43 or identity.startswith('demo-')):
            raise RuntimeError("Use individual production identities with generated keys of at least 43 characters")
        keys.add(key)
        identities.add(identity)
        users.append((key, User(identity, role)))
    return users


def validate_config():
    _configured_users()


def authenticate_key(key: str | None) -> User:
    try:
        users = _configured_users()
    except RuntimeError:
        raise HTTPException(503, "Authentication is not configured") from None
    matched = None
    if key and len(key) <= 512:
        for expected, user in users:
            if secrets.compare_digest(key.encode(), expected.encode()):
                matched = user
    if matched:
        return matched
    raise HTTPException(401, "Invalid or missing API key")


def current_user(key: str | None = Depends(key_header)) -> User:
    return authenticate_key(key)


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user
