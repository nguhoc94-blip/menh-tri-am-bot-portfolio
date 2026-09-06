"""Profile/chart transition helpers — invalidate stale chart on correction or switch."""

from __future__ import annotations

import logging
from typing import Any

from app.services.messenger_state import ConversationState, MessengerSession

logger = logging.getLogger(__name__)

_RENDER_ROUTING_KEYS = (
    "full_reading",
    "reading_render_job_id",
    "reading_render_enqueued",
    "outbound_class",
    "delivery_mode",
    "last_render_source",
)


def invalidate_current_reading(session: MessengerSession, *, reason: str) -> str:
    """Clear chart/reading artifacts and bump generation."""
    new_gen = session.bump_generation(reason=reason)
    session.chart_json = None
    session.reading_id = None
    session.state = ConversationState.CHATTING

    routing = session.routing if isinstance(session.routing, dict) else {}
    for key in _RENDER_ROUTING_KEYS:
        routing.pop(key, None)
    routing.pop("kb2_awaiting_confirm", None)
    routing.pop("kb2_payload_confirm", None)
    routing.pop("kb2_payload_edit", None)
    routing.pop("birth_assumptions", None)
    routing.pop("bundle_deferred", None)
    routing.pop("bundle_deferred_tier", None)
    from app.services.delivery_progress import clear_birth_date_lock

    clear_birth_date_lock(session)
    session.routing = routing

    logger.info(
        "profile_reading_invalidated sender_id=%s reason=%s generation_id=%s",
        session.sender_id,
        reason,
        new_gen,
    )
    return new_gen


def apply_self_birth_correction(
    session: MessengerSession,
    fields: dict[str, Any],
    *,
    reason: str = "self_birth_correction",
) -> None:
    if session.chart_json and fields:
        invalidate_current_reading(session, reason=reason)
    session.birth_data.update(fields)


def promote_pending_profile(
    session: MessengerSession,
    *,
    reason: str = "profile_switch_accept",
) -> dict[str, Any]:
    routing = session.routing if isinstance(session.routing, dict) else {}
    pending = dict(routing.get("pending_profile_birth") or {})
    invalidate_current_reading(session, reason=reason)
    session.birth_data = pending
    routing.pop("pending_profile_birth", None)
    routing.pop("profile_clarification_shown", None)
    routing.pop("profile_switch_awaiting_confirm", None)
    session.routing = routing
    return pending


def decline_pending_profile(session: MessengerSession) -> None:
    routing = session.routing if isinstance(session.routing, dict) else {}
    routing.pop("pending_profile_birth", None)
    routing.pop("profile_switch_awaiting_confirm", None)
    session.routing = routing


def mark_profile_switch_pending(session: MessengerSession) -> None:
    routing = session.routing if isinstance(session.routing, dict) else {}
    routing["profile_switch_awaiting_confirm"] = True
    session.routing = routing


_PROFILE_SWITCH_ACCEPT_REPLY = (
    "Mình đã chuyển sang hồ sơ mới. "
    "Bạn cho mình biết thêm nếu còn thiếu thông tin nhé."
)

_PROFILE_SWITCH_DECLINE_REPLY = (
    "Ok, mình tiếp tục hỗ trợ theo hồ sơ hiện tại của bạn."
)

_PROFILE_SWITCH_AMBIGUOUS_REPLY = (
    "Bạn xác nhận giúp mình: muốn chuyển sang xem cho người vừa nhắc, "
    "hay vẫn hỏi theo hồ sơ hiện tại?"
)

_CORRECTION_UPDATED_REPLY = (
    "Mình đã cập nhật thông tin sinh của bạn. "
    "Khi bạn sẵn sàng, nói mình lập lá số mới nhé."
)


def profile_switch_accept_reply() -> str:
    return _PROFILE_SWITCH_ACCEPT_REPLY


def profile_switch_decline_reply() -> str:
    return _PROFILE_SWITCH_DECLINE_REPLY


def profile_switch_ambiguous_reply() -> str:
    return _PROFILE_SWITCH_AMBIGUOUS_REPLY


def correction_updated_reply() -> str:
    return _CORRECTION_UPDATED_REPLY
