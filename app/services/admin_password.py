"""PBKDF2 password hashing (stdlib) — Nhịp 2 admin baseline."""

from __future__ import annotations

import base64
import hashlib
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390_000)
    return base64.b64encode(salt + dk).decode("ascii")


def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored.encode("ascii"))
    except (ValueError, OSError):
        return False
    if len(raw) < 17:
        return False
    salt, h = raw[:16], raw[16:]
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390_000)
    return secrets.compare_digest(dk, h)
