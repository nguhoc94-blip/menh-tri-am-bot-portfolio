"""Intent confirmation payload context validation — Phase 4.1."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.messenger_state import ConversationState, MessengerSession
from app.services.payload_specs import (
    INTENT_CONFIRMATION_PAYLOADS,
    PROVISIONAL_ACCEPT,
    PROVISIONAL_DECLINE,
    PROFILE_SWITCH_ACCEPT,
    PROFILE_SWITCH_DECLINE,
)

_BLOCKED_FOR_INTENT = frozenset({
    ConversationState.ANALYZING,
    ConversationState.RENDERING,
    ConversationState.DELIVERING,
    ConversationState.PAYMENT_VERIFYING,
    ConversationState.PAID_GENERATING,
    ConversationState.SUPPORT_HANDOFF,
})


@dataclass(frozen=True)
class PayloadValidation:
    valid: bool
    reply: str | None = None


def is_intent_confirmation_payload(payload: str | None) -> bool:
    return bool(payload and payload in INTENT_CONFIRMATION_PAYLOADS)


def _routing(session: MessengerSession) -> dict:
    r = session.routing
    return r if isinstance(r, dict) else {}


def validate_intent_payload(session: MessengerSession, payload: str | None) -> PayloadValidation:
    """Validate payload context before IntentDecision side effects."""
    if not payload or payload not in INTENT_CONFIRMATION_PAYLOADS:
        return PayloadValidation(valid=True)

    if session.state in _BLOCKED_FOR_INTENT:
        return PayloadValidation(
            valid=False,
            reply="Mình đang xử lý yêu cầu trước đó. Bạn thử lại sau nhé.",
        )

    routing = _routing(session)

    if payload == PROVISIONAL_ACCEPT:
        if session.chart_json:
            return PayloadValidation(
                valid=False,
                reply="Bạn đã có lá số rồi. Hãy hỏi tiếp về lá số hiện tại nhé.",
            )
        if not session.has_minimum_birth_for_provisional_reading():
            return PayloadValidation(
                valid=False,
                reply="Mình cần đủ ngày/tháng/năm sinh trước khi xem tạm. Bạn bổ sung giúp mình nhé.",
            )
        if session.is_birth_complete():
            return PayloadValidation(
                valid=False,
                reply="Thông tin sinh đã đủ. Bạn xác nhận để mình lập lá số nhé.",
            )
        if not routing.get("provisional_offer_pending") and not session.has_any_birth_data():
            return PayloadValidation(
                valid=False,
                reply="Hiện chưa có đề xuất xem tạm phù hợp. Bạn cho mình biết thông tin sinh trước nhé.",
            )
        return PayloadValidation(valid=True)

    if payload == PROVISIONAL_DECLINE:
        if not routing.get("provisional_offer_pending") and session.is_birth_complete():
            return PayloadValidation(valid=True)
        if not session.has_any_birth_data():
            return PayloadValidation(
                valid=False,
                reply="Mình chưa có thông tin sinh để tiếp tục. Bạn nhập giúp mình nhé.",
            )
        return PayloadValidation(valid=True)

    if payload == PROFILE_SWITCH_ACCEPT:
        if not routing.get("pending_profile_birth"):
            return PayloadValidation(
                valid=False,
                reply=(
                    "Hiện không có hồ sơ người khác đang chờ xác nhận. "
                    "Bạn mô tả rõ người cần xem giúp mình nhé."
                ),
            )
        return PayloadValidation(valid=True)

    if payload == PROFILE_SWITCH_DECLINE:
        if not routing.get("pending_profile_birth") and not routing.get("profile_switch_awaiting_confirm"):
            return PayloadValidation(valid=True)
        return PayloadValidation(valid=True)

    return PayloadValidation(valid=True)


def clear_provisional_markers(session: MessengerSession) -> None:
    routing = _routing(session)
    routing.pop("provisional_offer_pending", None)
    routing.pop("birth_assumptions", None)
    session.routing = routing


def mark_provisional_offer_pending(session: MessengerSession) -> None:
    routing = _routing(session)
    routing["provisional_offer_pending"] = True
    session.routing = routing
