"""Development API-key identity; do not trust roles supplied by the browser."""
import os
import secrets
from dataclasses import dataclass
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class User:
    id: str
    role: str


def validate_config():
    keys = [os.environ.get(name, "") for name in ("ADMIN_API_KEY", "EMPLOYEE_API_KEY")]
    if any(not 24 <= len(key) <= 512 or not key.isascii() or any(c.isspace() for c in key) for key in keys) or keys[0] == keys[1]:
        raise RuntimeError("Set distinct ADMIN_API_KEY and EMPLOYEE_API_KEY values of at least 24 characters.")


def authenticate_key(key: str | None) -> User:
    try:
        validate_config()
    except RuntimeError:
        raise HTTPException(503, "Authentication is not configured") from None
    if key and len(key) <= 512:
        for role, name in (("admin", "ADMIN_API_KEY"), ("employee", "EMPLOYEE_API_KEY")):
            expected = os.environ.get(name, "")
            if expected and secrets.compare_digest(key.encode(), expected.encode()):
                return User(id=f"demo-{role}", role=role)
    raise HTTPException(401, "Invalid or missing API key")


def current_user(key: str | None = Depends(key_header)) -> User:
    return authenticate_key(key)


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user
