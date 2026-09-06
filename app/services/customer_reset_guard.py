"""Block customer-facing reset phrases before GPT — session is not cleared."""

from __future__ import annotations

import re

from app.services.subject_detector import fold_vi
from app.utils.input_sanitizer import is_empty_after_sanitize, sanitize_text

CUSTOMER_RESET_NOOP_REPLY = (
    "Mình vẫn nhớ thông tin trong phiên này. Bạn muốn hỏi tiếp phần nào?"
)

# Whole-message exact matches after Vietnamese fold + whitespace collapse.
_CUSTOMER_RESET_EXACT: frozenset[str] = frozenset(
    {
        "reset",
        "reset lai",
        "reset session",
        "restart",
        "bat dau lai",
        "bat dau lai tu dau",
        "start over",
        "start",
        "lam lai",
        "lam lai tu dau",
        "nhap lai tu dau",
        "nhap lai",
        "quen het",
        "xoa het",
        "lam moi",
        "clear session",
        "new session",
    }
)


def _normalize_for_reset_match(text: str) -> str:
    folded = fold_vi(text or "")
    return re.sub(r"\s+", " ", folded).strip()


def is_customer_reset_attempt(text: str) -> bool:
    """True when user asks to reset/restart the chat (not the secret admin code)."""
    normalized = sanitize_text(text)
    if is_empty_after_sanitize(normalized):
        return False
    folded = _normalize_for_reset_match(normalized)
    return folded in _CUSTOMER_RESET_EXACT
