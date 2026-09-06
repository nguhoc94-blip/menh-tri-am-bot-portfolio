from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services import payload_specs as P
from app.services.app_config_store import (
    append_payment_checkout_url,
    compose_opening_text,
    get_config_text,
    get_config_text_first,
    get_config_text_first_publish_safe,
)
from app.services.flow_routing import (
    ENTRY_CAREER,
    ENTRY_LOVE,
    ENTRY_ORGANIC_GENERAL,
    cta_config_keys_after_free,
    opening_package_config_keys,
    resolve_active_flow,
)
from app.services.messenger_funnel_bridge import record_funnel_event
from app.services.messenger_state import ConversationState, MessengerSession
from app.services.messenger_state_db import DbMessengerStateStore
from app.services.order_service import create_draft_order
from app.services.user_profile_flags import entitlement_paid_unlocked, get_profile_metadata

logger = logging.getLogger(__name__)

_store = DbMessengerStateStore()


@dataclass
class PayloadApplyResult:
    """After structured payload handling."""

    reply_immediate: str | None
    """When set, messenger_handler should send this (postback-only openings)."""

    continue_with_text: bool
    """True if a user text message should still run through conversation_bridge."""


def _routing(session: MessengerSession) -> dict:
    r = session.routing
    if not isinstance(r, dict):
        session.routing = {}
        return session.routing
    return r


def _ensure_checkout_order(session: MessengerSession, sender_id: str) -> None:
    if session.order_id is None:
        session.order_id = create_draft_order(sender_id)
    session.state = ConversationState.CHECKOUT


def _sync_active_flow(session: MessengerSession, metadata: dict, *, sender_id: str) -> str:
    paid = entitlement_paid_unlocked(sender_id, session.order_id)
    has_free = bool(session.chart_json or session.reading_id)
    partial = session.has_partial_intake()
    entry = _routing(session).get("entry_source")
    if isinstance(entry, str):
        src = entry
    else:
        src = None
    flow = resolve_active_flow(
        has_partial_intake=partial,
        paid_once=paid,
        has_free_result=has_free,
        entry_source=src,
    )
    _routing(session)["active_flow"] = flow
    return flow


def _opening_for_flow(flow: str) -> str:
    gk, tk, ok = opening_package_config_keys(flow)
    return compose_opening_text(gk, tk, ok)


def apply_structured_payload(
    *,
    sender_id: str,
    payload: str,
    request_id: str,
    had_user_text: bool,
) -> PayloadApplyResult:
    """
    Mutates and persists session when needed. Records funnel events for baseline payloads.
    """
    p = (payload or "").strip()
    if not p or p not in P.ALL_BASELINE_PAYLOADS:
        # Không chặn pipeline: postback lạ vẫn rơi xuống nhánh logging phía dưới.
        return PayloadApplyResult(reply_immediate=None, continue_with_text=True)

    session = _store.get_or_create(sender_id)
    metadata = get_profile_metadata(sender_id)
    routing = _routing(session)

    reply: str | None = None
    continue_text = bool(had_user_text)

    def _save() -> None:
        _sync_active_flow(session, get_profile_metadata(sender_id), sender_id=sender_id)
        _store.save(session)

    # --- Entry ---
    if p == P.TV_ENTRY_LOVE:
        routing["entry_source"] = ENTRY_LOVE
        record_funnel_event(
            sender_id=sender_id, event_type="entry_cta_clicked", request_id=request_id, payload={"payload": p}
        )
        flow = _sync_active_flow(session, metadata, sender_id=sender_id)
        reply = _opening_for_flow(flow)
        continue_text = False
    elif p == P.TV_ENTRY_CAREER:
        routing["entry_source"] = ENTRY_CAREER
        record_funnel_event(
            sender_id=sender_id, event_type="entry_cta_clicked", request_id=request_id, payload={"payload": p}
        )
        flow = _sync_active_flow(session, metadata, sender_id=sender_id)
        reply = _opening_for_flow(flow)
        continue_text = False
    elif p == P.TV_ENTRY_ORGANIC_GENERAL:
        routing["entry_source"] = ENTRY_ORGANIC_GENERAL
        record_funnel_event(
            sender_id=sender_id, event_type="entry_cta_clicked", request_id=request_id, payload={"payload": p}
        )
        flow = _sync_active_flow(session, metadata, sender_id=sender_id)
        reply = _opening_for_flow(flow)
        continue_text = False

    # --- Interest rescue ---
    elif p == P.TV_INTEREST_LOVE:
        routing["entry_source"] = ENTRY_LOVE
        routing["general_interest_resolved"] = True
        record_funnel_event(
            sender_id=sender_id, event_type="interest_rescue_clicked", request_id=request_id, payload={"payload": p}
        )
        flow = _sync_active_flow(session, metadata, sender_id=sender_id)
        reply = _opening_for_flow(flow)
        continue_text = False
    elif p == P.TV_INTEREST_CAREER:
        routing["entry_source"] = ENTRY_CAREER
        routing["general_interest_resolved"] = True
        record_funnel_event(
            sender_id=sender_id, event_type="interest_rescue_clicked", request_id=request_id, payload={"payload": p}
        )
        flow = _sync_active_flow(session, metadata, sender_id=sender_id)
        reply = _opening_for_flow(flow)
        continue_text = False
    elif p == P.TV_INTEREST_PATH_GENERAL:
        routing["entry_source"] = ENTRY_ORGANIC_GENERAL
        routing["general_interest_resolved"] = True
        record_funnel_event(
            sender_id=sender_id, event_type="interest_rescue_clicked", request_id=request_id, payload={"payload": p}
        )
        flow = _sync_active_flow(session, metadata, sender_id=sender_id)
        reply = _opening_for_flow(flow)
        continue_text = False

    # --- Intake resume/edit ---
    elif p == P.TV_INTAKE_RESUME:
        routing["intake_resume_choice"] = "resume"
        record_funnel_event(
            sender_id=sender_id, event_type="intake_resume_clicked", request_id=request_id, payload={"payload": p}
        )
        reply = get_config_text(
            "intake_resume_qr_resume",
            "Được rồi, mình tiếp tục cùng bạn nhé. Bạn cho mình biết thông tin còn thiếu.",
        )
        continue_text = False
    elif p == P.TV_INTAKE_EDIT:
        session.bump_generation(reason="intake_edit")
        session.birth_data = {}
        routing["intake_resume_choice"] = "edit"
        record_funnel_event(
            sender_id=sender_id, event_type="intake_edit_clicked", request_id=request_id, payload={"payload": p}
        )
        reply = get_config_text("intake_resume_qr_edit") or (
            "Đã xoá phần đã nhập. Bạn cho mình họ tên và ngày giờ sinh lần nữa nhé."
        )

    # --- CTA sau free / returning / paid repeat ---
    elif p in (
        P.TV_CTA_DEEP_LOVE,
        P.TV_CTA_DEEP_CAREER,
        P.TV_CTA_DEEP_GENERAL,
        P.TV_CTA_PRIMARY_RETURNING_UNPAID,
        P.TV_CTA_PRIMARY_PAID_REPEAT,
    ):
        record_funnel_event(
            sender_id=sender_id, event_type="cta_primary_clicked", request_id=request_id, payload={"payload": p}
        )
        if p == P.TV_CTA_PRIMARY_RETURNING_UNPAID:
            _ensure_checkout_order(session, sender_id)
            reply = append_payment_checkout_url(
                get_config_text_first_publish_safe(
                    "payment_link_label_final",
                    "payment_link_placeholder_final",
                    "payment_link_label",
                )
            )
        elif p == P.TV_CTA_PRIMARY_PAID_REPEAT:
            reply = get_config_text(
                "opening_question_paid_repeat",
                "Bạn muốn xem thêm phần nào? Hãy nhắn ngắn gọn nhu cầu của bạn.",
            )
        else:
            offer = ""
            if p == P.TV_CTA_DEEP_LOVE:
                offer = get_config_text("offer_label_love_deep")
            elif p == P.TV_CTA_DEEP_CAREER:
                offer = get_config_text("offer_label_career_deep")
            else:
                offer = get_config_text("offer_label_general_deep")
            reply = offer or "Bạn có thể nhắn tiếp để mình hỗ trợ bước thanh toán thủ công."
        continue_text = False
    elif p in (
        P.TV_CTA_DEFER_LOVE,
        P.TV_CTA_DEFER_CAREER,
        P.TV_CTA_DEFER_GENERAL,
        P.TV_CTA_SECONDARY_PAID_REPEAT,
    ):
        record_funnel_event(
            sender_id=sender_id, event_type="cta_secondary_clicked", request_id=request_id, payload={"payload": p}
        )
        reply = get_config_text_first(
            "footer_final_common",
            "footer_baseline",
            default="Cảm ơn bạn.",
        )
        continue_text = False
    elif p == P.TV_CTA_SECONDARY_RETURNING_UNPAID:
        record_funnel_event(
            sender_id=sender_id,
            event_type="upsell_secondary_clicked",
            request_id=request_id,
            payload={"payload": p},
        )
        reply = get_config_text(
            "cta_secondary_after_free_returning_unpaid",
            "Bạn có thể hỏi lại về lá số bất cứ lúc nào.",
        )
        continue_text = False

    # --- Universal confirm: merge birth hints ---
    elif p == P.TV_CAL_SOLAR:
        session.birth_data["calendar_type"] = "solar"
        record_funnel_event(sender_id=sender_id, event_type="quick_reply_calendar", request_id=request_id, payload={"value": "solar"})
    elif p == P.TV_CAL_LUNAR:
        session.birth_data["calendar_type"] = "lunar"
        record_funnel_event(sender_id=sender_id, event_type="quick_reply_calendar", request_id=request_id, payload={"value": "lunar"})
    elif p == P.TV_GENDER_MALE:
        session.birth_data["gender"] = "male"
        record_funnel_event(sender_id=sender_id, event_type="quick_reply_gender", request_id=request_id, payload={"value": "male"})
    elif p == P.TV_GENDER_FEMALE:
        session.birth_data["gender"] = "female"
        record_funnel_event(sender_id=sender_id, event_type="quick_reply_gender", request_id=request_id, payload={"value": "female"})
    elif p == P.TV_CONFIRM_EDIT_INFO:
        session.birth_data = {}
        routing["kb2_awaiting_confirm"] = False
        routing.pop("kb2_payload_confirm", None)
        record_funnel_event(sender_id=sender_id, event_type="confirm_edit_info", request_id=request_id, payload={})
        reply = "Đã xoá phần đã nhập. Mình sẽ hỏi lại từ đầu nhé."
        continue_text = False
    elif p == P.TV_CONFIRM_OK:
        record_funnel_event(sender_id=sender_id, event_type="confirm_ok", request_id=request_id, payload={})

    elif p == P.TV_OFFER_OPEN_PAYMENT:
        record_funnel_event(sender_id=sender_id, event_type="offer_open_payment", request_id=request_id, payload={})
        _ensure_checkout_order(session, sender_id)
        reply = append_payment_checkout_url(
            get_config_text_first_publish_safe(
                "payment_link_label_final",
                "payment_link_placeholder_final",
                "payment_link_label",
            )
        )
        continue_text = False
    elif p == P.TV_OFFER_NEED_HUMAN:
        record_funnel_event(sender_id=sender_id, event_type="offer_need_human", request_id=request_id, payload={})
        reply = get_config_text_first_publish_safe(
            "premium_secondary_cta_final",
            "premium_expectation_setting_final",
            "cta_secondary_after_free_paid_repeat",
            default="Mình đã ghi nhận. Đội ngũ sẽ liên hệ thủ công khi có lịch hỗ trợ.",
        )
        continue_text = False

    else:
        return PayloadApplyResult(reply_immediate=None, continue_with_text=bool(had_user_text))

    _save()
    # Tránh gửi trùng: quick_reply kèm text thì chỉ merge session, không gửi reply_immediate.
    reply_out = None if had_user_text else reply
    continue_out = bool(had_user_text)
    logger.info(
        "payload_applied request_id=%s sender_id=%s payload=%s reply=%s continue_text=%s",
        request_id,
        sender_id,
        p,
        bool(reply_out),
        continue_out,
    )
    return PayloadApplyResult(reply_immediate=reply_out, continue_with_text=continue_out)


def append_cta_after_free_hint(flow: str) -> str:
    """Append primary/secondary labels from app_config (text-only Messenger baseline)."""
    pair = cta_config_keys_after_free(flow)
    if not pair:
        return ""
    a, b, la, lb = pair
    t1 = get_config_text_first_publish_safe(a, la)
    t2 = get_config_text_first_publish_safe(b, lb)
    if not t1 and not t2:
        return ""
    lines = []
    if t1:
        lines.append(f"• {t1}")
    if t2:
        lines.append(f"• {t2}")
    return "\n".join(lines)
