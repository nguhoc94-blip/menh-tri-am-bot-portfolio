"""Page-owner premium grant — verified Page echo / admin send only (not customer inbound)."""

from __future__ import annotations

import logging
import os

from app.services.assistant_takeover import (
    ASSISTANT_RETURN_PREMIUM_TEXT,
    lookup_assistant_return_tier,
    normalize_echo_command_text,
)
from app.services.chat_tier import ChatTier
from app.services.subject_detector import fold_vi

logger = logging.getLogger(__name__)

# Flexible match: Page messages containing any of these core phrases grant premium.
_PREMIUM_GRANT_PHRASES_FOLDED: tuple[str, ...] = (
    fold_vi("trợ lý premium sẽ tiếp tục đồng hành"),
    fold_vi("trợ lý premium sẽ đồng hành"),
    fold_vi("từ giờ trợ lý premium"),
    fold_vi("từ đây trợ lý premium"),
)


def _matches_premium_grant_phrase(folded: str) -> bool:
    return any(phrase in folded for phrase in _PREMIUM_GRANT_PHRASES_FOLDED)


def _page_owner_premium_grant_enabled() -> bool:
    return (os.environ.get("PAGE_OWNER_PREMIUM_GRANT_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def is_page_owner_premium_grant_text(text: str) -> bool:
    """True when text is a page-owner premium grant command (exact or phrase match)."""
    if not _page_owner_premium_grant_enabled():
        return False
    normalized = normalize_echo_command_text(text)
    if not normalized:
        return False
    if lookup_assistant_return_tier(normalized) == ChatTier.PREMIUM:
        return True
    return _matches_premium_grant_phrase(fold_vi(normalized))


def try_handle_page_owner_premium_grant(
    customer_psid: str,
    raw_text: str,
    *,
    request_id: str,
) -> bool:
    """Apply premium grant from a verified page-owner channel."""
    if not is_page_owner_premium_grant_text(raw_text):
        return False

    from app.services.conversation_bridge import apply_page_owner_premium_grant

    applied = apply_page_owner_premium_grant(
        customer_psid,
        request_id=request_id,
        source="page_owner_grant",
    )
    if applied:
        logger.info(
            "page_owner_premium_grant request_id=%s customer_psid=%s chat_tier=premium "
            "event=page_owner_premium_grant",
            request_id,
            customer_psid,
        )
    return applied
