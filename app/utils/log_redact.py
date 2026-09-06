"""
Redact sensitive patterns from log lines.
V9 expansion: added image URL, HMAC secret, bank info, signed URL patterns.
P.1 rule 5 / V9.2 §7.2
"""

from __future__ import annotations

import logging
import re

# Substrings / patterns that must not appear raw in log output
_SENSITIVE_PATTERNS = (
    # API keys / tokens (existing)
    re.compile(r"sk-[A-Za-z0-9]{10,}", re.I),
    re.compile(r"OPENAI_API_KEY\s*=\s*\S+"),
    re.compile(r"FB_PAGE_ACCESS_TOKEN\s*=\s*\S+"),
    re.compile(r"FB_VERIFY_TOKEN\s*=\s*\S+"),
    re.compile(r"DATABASE_URL\s*=\s*\S+"),
    re.compile(r"postgresql://[^\s]+", re.I),
    re.compile(r"https://graph\.facebook\.com/[^\s]+access_token=[^\s&]+", re.I),
    re.compile(r"access_token=[A-Za-z0-9_\-.]{20,}", re.I),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{20,}", re.I),
    # V9 expansion: HMAC / signing secrets
    re.compile(r"USER_HASH_HMAC_SECRET\s*=\s*\S+"),
    re.compile(r"EVENT_USER_ID_HMAC_SECRET\s*=\s*\S+"),
    re.compile(r"R2_SECRET_ACCESS_KEY\s*=\s*\S+"),
    re.compile(r"R2_ACCESS_KEY_ID\s*=\s*\S+"),
    re.compile(r"SENTRY_DSN\s*=\s*\S+"),
    # V9 expansion: bank info (must not appear in logs)
    re.compile(r"DONATE_ACCOUNT_NUMBER\s*=\s*\S+"),
    re.compile(r"\b\d{10,19}\b(?=.*ngân hàng|\b(?=.*bank))", re.I),  # bank account numbers near "bank"
    # V9 expansion: signed URLs (storage URLs with tokens/signatures)
    re.compile(r"https://[^\s]*[?&](X-Amz-Signature|token|sig)=[A-Za-z0-9%_\-.]{20,}", re.I),
    re.compile(r"https://[a-z0-9\-]+\.r2\.cloudflarestorage\.com/[^\s]+", re.I),
    # V9 expansion: raw image data (base64 blobs)
    re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]{50,}", re.I),
)


def redact_log_line(line: str) -> str:
    out = line
    for pat in _SENSITIVE_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def redact_log_text(text: str, *, max_lines: int = 200) -> str:
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(redact_log_line(L) for L in tail)


class RedactingFormatter(logging.Formatter):
    """Apply redact_log_line to final formatted log lines (Nhịp 3)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_line(super().format(record))


def attach_redacting_formatter_to_root(*, fmt: str, datefmt: str | None = None) -> None:
    for h in logging.getLogger().handlers:
        h.setFormatter(RedactingFormatter(fmt, datefmt=datefmt))
