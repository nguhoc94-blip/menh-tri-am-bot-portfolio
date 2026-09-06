"""
Input sanitizer — Slice 6.
Sanitize user text before any analysis or routing.
docs/ARCHITECTURE/10_cost_and_abuse.md

Rules:
- Strip leading/trailing whitespace
- Remove null bytes and control characters (except \n, \t)
- Collapse runs of whitespace/newlines
- Hard cap at MAX_INPUT_CHARS (prevents runaway prompt injection payloads)
- Unicode normalize NFC for consistent comparison
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = int((os.environ.get("MAX_INPUT_CHARS") or "2000").strip() or "2000")

# Control characters except \n (0x0A) and \t (0x09)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]")

# Collapse excessive blank lines (>2 consecutive newlines)
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")


def sanitize_text(text: str | None, *, max_chars: int = MAX_INPUT_CHARS) -> str:
    """
    Sanitize user-supplied text for safe use in analysis/routing.

    Returns a clean string, never None. Empty input returns "".
    Logs a warning if text was truncated or had control chars removed.
    """
    if not text:
        return ""

    # NFC normalization (canonical decomposition then canonical composition)
    cleaned = unicodedata.normalize("NFC", text)

    # Remove control chars (keep \n \t)
    original_len = len(cleaned)
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    if len(cleaned) < original_len:
        logger.debug("input_sanitizer_removed_control_chars original=%d cleaned=%d", original_len, len(cleaned))

    # Collapse excessive newlines
    cleaned = _EXCESS_NEWLINES_RE.sub("\n\n", cleaned)

    # Strip surrounding whitespace
    cleaned = cleaned.strip()

    # Hard length cap
    if len(cleaned) > max_chars:
        logger.warning(
            "input_sanitizer_truncated original_len=%d cap=%d",
            len(cleaned), max_chars,
        )
        cleaned = cleaned[:max_chars]

    return cleaned


def is_empty_after_sanitize(text: str | None) -> bool:
    """Return True if text is empty or whitespace-only after sanitization."""
    return not sanitize_text(text).strip()
