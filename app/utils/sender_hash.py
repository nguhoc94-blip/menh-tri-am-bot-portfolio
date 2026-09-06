"""Stable short hash for PSID logging — never log raw sender_id."""

from __future__ import annotations

import hashlib


def hash_sender_id(sender_id: str) -> str:
    """Return first 12 hex chars of SHA-256(sender_id). Empty input → empty string."""
    if not sender_id:
        return ""
    return hashlib.sha256(sender_id.encode()).hexdigest()[:12]
