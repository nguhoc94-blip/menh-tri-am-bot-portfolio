from __future__ import annotations

import json
import logging
import os
from typing import Any

from psycopg.rows import dict_row

from app.db import get_connection

logger = logging.getLogger(__name__)


def _runtime_publish_safe_enabled() -> bool:
    return (os.environ.get("RUNTIME_PUBLISH_SAFE") or "").strip().lower() in ("1", "true", "yes")


def _blocked_placeholder(val: dict[str, Any] | None, text: str) -> bool:
    if not _runtime_publish_safe_enabled():
        return False
    if "NEED_PRODUCT_FINAL" in (text or ""):
        return True
    if val and val.get("placeholder_tag") == "NEED_PRODUCT_FINAL":
        return True
    return False


def runtime_publish_fallback_message() -> str:
    """User-facing safe line when placeholder would leak."""
    return get_config_text(
        "runtime_publish_fallback",
        "Đang cập nhật nội dung. Bạn thử lại sau hoặc liên hệ hỗ trợ.",
    )


def get_config_text_publish_safe(config_key: str, default: str = "") -> str:
    """Same as get_config_text but blocks NEED_PRODUCT_FINAL when RUNTIME_PUBLISH_SAFE=1."""
    val = get_config_value(config_key)
    raw = get_config_text(config_key, default)
    if _blocked_placeholder(val, raw):
        return runtime_publish_fallback_message()
    return raw


def get_config_text_first_publish_safe(*keys: str, default: str = "") -> str:
    for k in keys:
        if not k:
            continue
        t = get_config_text_publish_safe(k, "")
        if t:
            return t
    return default


def is_social_proof_publish_allowed() -> bool:
    val = get_config_value("feature_social_proof_publish_allowed")
    if not val:
        return False
    return bool(val.get("allowed"))


def get_social_proof_snippet_publish_safe(config_key: str = "social_proof_publish_snippet_template") -> str:
    if not is_social_proof_publish_allowed():
        return ""
    return get_config_text_publish_safe(config_key, "")


def get_config_value(config_key: str) -> dict[str, Any] | None:
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT config_value
                    FROM app_config
                    WHERE config_key = %s
                    """,
                    (config_key,),
                )
                row = cur.fetchone()
        if not row:
            return None
        raw = row["config_value"]
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw or {})
    except Exception:
        logger.debug("app_config_get_failed key=%s", config_key, exc_info=True)
        return None


def get_config_text(config_key: str, default: str = "") -> str:
    val = get_config_value(config_key)
    if not val:
        return default
    t = val.get("text")
    return str(t).strip() if t is not None else default


# Runtime Messenger chỉ đọc cột published (config_value). draft_value chỉ dùng trong admin.
_TRUST_FINAL_FALLBACK: dict[str, str] = {
    "trust_bridge_final_love": "trust_bridge_new_user_love",
    "trust_bridge_final_career": "trust_bridge_new_user_career",
    "trust_bridge_final_general": "trust_bridge_new_user_general",
    "trust_bridge_final_returning_unpaid": "trust_bridge_returning_unpaid",
    "trust_bridge_final_intake_resume": "",
    "trust_bridge_final_paid_repeat": "trust_bridge_paid_repeat",
}


def get_config_text_first(*keys: str, default: str = "") -> str:
    """Thử lần lượt các key published; dùng cho CP3 final + fallback CP2."""
    for k in keys:
        if not k:
            continue
        t = get_config_text(k, "")
        if t:
            return t
    return default


def get_config_text_trust_final(trust_final_key: str) -> str:
    fb = _TRUST_FINAL_FALLBACK.get(trust_final_key, "")
    return get_config_text_first(trust_final_key, fb)


def compose_opening_text(greeting_key: str, trust_key: str | None, opening_key: str) -> str:
    from app.services.intake_policy import append_session_reset_hint

    parts: list[str] = []
    g = get_config_text(greeting_key)
    if g:
        parts.append(g)
    if trust_key:
        t = (
            get_config_text_trust_final(trust_key)
            if trust_key.startswith("trust_bridge_final_")
            else get_config_text(trust_key)
        )
        if t:
            parts.append(t)
    o = get_config_text(opening_key)
    if o:
        parts.append(o)
    if not parts:
        return ""
    return append_session_reset_hint("\n\n".join(parts))


def append_payment_checkout_url(text: str) -> str:
    """
    Nối URL thanh toán sau copy từ app_config. COO/ops set PAYMENT_CHECKOUT_URL trên host.
    """
    url = (os.environ.get("PAYMENT_CHECKOUT_URL") or "").strip()
    if not url:
        return text
    base = (text or "").rstrip()
    if not base:
        return url
    return f"{base}\n{url}"


def compose_opening_text_publish_safe(greeting_key: str, trust_key: str | None, opening_key: str) -> str:
    """Opening package with placeholder guard for Gate Output / publish runtime."""
    from app.services.intake_policy import append_session_reset_hint

    parts: list[str] = []
    g = get_config_text_publish_safe(greeting_key)
    if g:
        parts.append(g)
    if trust_key:
        if trust_key.startswith("trust_bridge_final_"):
            fb = _TRUST_FINAL_FALLBACK.get(trust_key, "")
            t = get_config_text_first_publish_safe(trust_key, fb)
        else:
            t = get_config_text_publish_safe(trust_key)
        if t:
            parts.append(t)
    o = get_config_text_publish_safe(opening_key)
    if o:
        parts.append(o)
    if not parts:
        return ""
    return append_session_reset_hint("\n\n".join(parts))
