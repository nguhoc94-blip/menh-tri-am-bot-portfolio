"""Persistent conversation owner — BOT vs ASSISTANT human takeover."""

from __future__ import annotations

import logging
from enum import Enum

from app.services.user_profile_flags import (
    get_profile_metadata,
    patch_profile_metadata_strict,
)

logger = logging.getLogger(__name__)

_METADATA_KEY = "conversation_owner"


class ConversationOwner(str, Enum):
    BOT = "bot"
    ASSISTANT = "assistant"


def get_conversation_owner(sender_id: str) -> ConversationOwner:
    """Read owner from metadata; missing/invalid/error → BOT (fail-closed)."""
    try:
        metadata = get_profile_metadata(sender_id)
        raw = metadata.get(_METADATA_KEY)
        if raw == ConversationOwner.ASSISTANT.value:
            return ConversationOwner.ASSISTANT
        return ConversationOwner.BOT
    except Exception:
        logger.warning(
            "conversation_owner_read_failed sender_id=%s",
            sender_id,
            exc_info=True,
        )
        return ConversationOwner.BOT


def set_conversation_owner(sender_id: str, owner: ConversationOwner) -> None:
    """Persist owner; write failure propagates."""
    patch_profile_metadata_strict(sender_id, {_METADATA_KEY: owner.value})


def should_bot_process_inbound(sender_id: str) -> bool:
    """True when bot pipeline may handle customer inbound (production + debug parity)."""
    return get_conversation_owner(sender_id) != ConversationOwner.ASSISTANT
