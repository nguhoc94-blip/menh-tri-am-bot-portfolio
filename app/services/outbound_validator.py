"""Single outbound validator — Nhịp 2. Validates once before Graph API send."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from app.services import payload_specs as P

# Outbound classes (contract for Gate Output / QA)
OUTBOUND_CLASS_TEXT_REPLY = "text_reply"
OUTBOUND_CLASS_SYSTEM_NOTICE = "system_notice"
OUTBOUND_CLASS_PAYMENT_CTA = "payment_cta"
OUTBOUND_CLASS_FAILSAFE = "failsafe_reply"
OUTBOUND_CLASS_ASSISTANT_TRANSITION = "assistant_transition"
OUTBOUND_CLASS_PAGE_OWNER = "page_owner_reply"

_LEAKAGE_PATTERNS = (
    r"system\s*prompt",
    r"NEED_PRODUCT_FINAL",
    r"internal\s*tool",
)

_ALLOWED_OUTBOUND_BY_STATE: dict[str, frozenset[str]] = {
    "CHATTING": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_PAYMENT_CTA,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_ASSISTANT_TRANSITION,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
    "HAS_CHART": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_PAYMENT_CTA,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_ASSISTANT_TRANSITION,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
    "CHECKOUT": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_PAYMENT_CTA,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
    "PAYMENT_VERIFYING": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_PAYMENT_CTA,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
    "PAID_GENERATING": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
    "PAID_READY": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_PAYMENT_CTA,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
    "SUPPORT_HANDOFF": frozenset(
        {
            OUTBOUND_CLASS_TEXT_REPLY,
            OUTBOUND_CLASS_SYSTEM_NOTICE,
            OUTBOUND_CLASS_FAILSAFE,
            OUTBOUND_CLASS_ASSISTANT_TRANSITION,
            OUTBOUND_CLASS_PAGE_OWNER,
        }
    ),
}


def _validator_enabled() -> bool:
    raw = (os.environ.get("OUTBOUND_VALIDATOR_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no")


def _runtime_publish_safe_text(text: str) -> bool:
    """False if text must not reach user when RUNTIME_PUBLISH_SAFE=1."""
    if (os.environ.get("RUNTIME_PUBLISH_SAFE") or "").strip().lower() not in ("1", "true", "yes"):
        return True
    if "NEED_PRODUCT_FINAL" in (text or ""):
        return False
    return True


@dataclass(frozen=True)
class OutboundValidationContext:
    conversation_state: str
    outbound_class: str
    text: str
    structured_payload: str | None = None


def validate_outbound(ctx: OutboundValidationContext) -> tuple[bool, str | None]:
    """
    Returns (ok, reason_if_blocked).
    """
    if not _validator_enabled():
        return True, None

    state = (ctx.conversation_state or "CHATTING").strip().upper()
    oclass = (ctx.outbound_class or OUTBOUND_CLASS_TEXT_REPLY).strip()

    allowed = _ALLOWED_OUTBOUND_BY_STATE.get(state)
    if allowed is None:
        allowed = _ALLOWED_OUTBOUND_BY_STATE["CHATTING"]
    if oclass not in allowed:
        return False, f"outbound_class_not_allowed_for_state:{oclass}:{state}"

    if ctx.structured_payload is not None and str(ctx.structured_payload).strip():
        pl = str(ctx.structured_payload).strip()
        if pl not in P.ALL_BASELINE_PAYLOADS:
            return False, "payload_not_whitelisted"

    t = ctx.text or ""
    if not _runtime_publish_safe_text(t):
        return False, "placeholder_need_product_final"

    for pat in _LEAKAGE_PATTERNS:
        if re.search(pat, t, re.I):
            return False, f"leakage_pattern:{pat}"

    return True, None


def fallback_message_blocked() -> str:
    return (
        "Mình không gửi được nội dung này do kiểm tra an toàn. "
        "Bạn thử lại sau hoặc nhắn để được hỗ trợ nhé."
    )


_BARE_ROUTING_CONTROL_LINE = re.compile(
    r"^(NONE|INTEREST|REQUEST)\s*$",
    re.IGNORECASE,
)


def sanitize_routing_control_lines(text: str) -> str:
    """Defense-in-depth: strip isolated routing tokens at message boundaries."""
    if not text:
        return text
    lines = text.splitlines()
    while lines and _BARE_ROUTING_CONTROL_LINE.match(lines[0].strip()):
        lines.pop(0)
    while lines and _BARE_ROUTING_CONTROL_LINE.match(lines[-1].strip()):
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines).strip()
