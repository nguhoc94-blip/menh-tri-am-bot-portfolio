"""Support CTA — voluntary donation text after value delivery (page bank + Shopee affiliate)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BANK_DIRECT_LABEL = "Qua số tài khoản trên trang chính của page"

SUPPORT_CTA_RETURNING_MIN_HOURS = 48
SUPPORT_CTA_MIN_MEANINGFUL_REPLY_CHARS = 80
SUPPORT_CTA_LABEL_MAX_LEN = 40
SUPPORT_CTA_MAX_SHOPEE_LINKS = 5

REASON_INITIAL_READING = "initial_reading"
REASON_RETURNING_VALUE = "returning_value"

_SHOPEE_HOSTS = frozenset({"s.shopee.vn", "shopee.vn"})


@dataclass(frozen=True)
class ShopeeLink:
    label: str
    url: str


@dataclass(frozen=True)
class SupportCtaTurnContext:
    conversation_mode: str | None = None
    user_message: str = ""
    reply_text: str = ""
    render_enqueued: bool = False
    pending_initial: bool = False
    reading_delivered: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_valid_shopee_hostname(hostname: str) -> bool:
    h = (hostname or "").lower().strip()
    if h in _SHOPEE_HOSTS:
        return True
    if h.endswith(".shopee.vn") and h != "shopee.vn":
        return True
    return False


def _validate_shopee_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    return _is_valid_shopee_hostname(parsed.hostname or "")


def load_shopee_links() -> list[ShopeeLink]:
    raw = (os.environ.get("MESSENGER_SHOPEE_LINKS_JSON") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("support_cta_shopee_json_invalid")
        return []
    if not isinstance(data, list):
        logger.warning("support_cta_shopee_json_not_list")
        return []
    out: list[ShopeeLink] = []
    for item in data:
        if len(out) >= SUPPORT_CTA_MAX_SHOPEE_LINKS:
            break
        if not isinstance(item, dict):
            continue
        label_raw = item.get("label")
        url_raw = item.get("url")
        if not isinstance(label_raw, str) or not isinstance(url_raw, str):
            continue
        label = label_raw.strip()
        url = url_raw.strip()
        if not label or len(label) > SUPPORT_CTA_LABEL_MAX_LEN:
            continue
        if not _validate_shopee_url(url):
            continue
        out.append(ShopeeLink(label=label, url=url))
    return out


def has_support_cta_content() -> bool:
    return True


def build_support_cta_message(
    *,
    include_bank: bool | None = None,
    include_shopee: bool | None = None,
) -> str:
    links = load_shopee_links()
    show_bank = include_bank if include_bank is not None else True
    show_shopee = include_shopee if include_shopee is not None else bool(links)
    if not show_bank and not show_shopee:
        return ""

    lines: list[str] = [
        "Đồng hành cùng Demo Bot",
        "",
        "Nếu phần luận giải và những cuộc trò chuyện với Tri Âm có ích với bạn, "
        "bạn có thể giúp dự án tiếp tục duy trì theo một trong hai cách dưới đây.",
        "",
        "Hoàn toàn tùy tâm — không ủng hộ cũng không ảnh hưởng việc bạn tiếp tục trò chuyện với Tri Âm 🌿",
    ]

    if show_bank:
        lines.extend(["", "🏦 Ủng hộ trực tiếp", _BANK_DIRECT_LABEL])

    if show_shopee and links:
        lines.extend(
            [
                "",
                "🛍️ Hoặc ủng hộ mà không tốn thêm phí",
                "Nếu bạn vốn đang cần mua một món nào đó trên Shopee, có thể mở sản phẩm "
                "qua một trong các link giới thiệu bên dưới rồi mua như bình thường.",
                "",
                "Giá của bạn không tăng vì sử dụng link; nếu đơn được ghi nhận, "
                "Tri Âm có thể nhận một khoản hoa hồng nhỏ từ Shopee.",
            ]
        )
        for link in links:
            lines.append(f"• {link.label} — {link.url}")

    lines.extend(
        [
            "",
            "Không có nhu cầu cũng không sao nhé. Cảm ơn bạn đã dành thời gian ở đây cùng Tri Âm 💛",
        ]
    )
    return "\n".join(lines)


def _latest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _hours_since(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    ref = now or _utcnow()
    return (ref - dt).total_seconds() / 3600.0


def counts_as_meaningful_activity(turn_ctx: SupportCtaTurnContext) -> bool:
    """Substantive follow-up — refreshes inactivity clock even when render is enqueued."""
    from app.services.conversation_bridge import _is_casual_greeting, _is_short_acknowledgment

    if turn_ctx.conversation_mode != "followup":
        return False
    if not turn_ctx.reading_delivered:
        return False
    if _is_casual_greeting(turn_ctx.user_message):
        return False
    if _is_short_acknowledgment(turn_ctx.user_message):
        return False
    if turn_ctx.render_enqueued:
        return True
    reply = (turn_ctx.reply_text or "").strip()
    if len(reply) < SUPPORT_CTA_MIN_MEANINGFUL_REPLY_CHARS:
        return False
    return True


def is_meaningful_followup_turn(turn_ctx: SupportCtaTurnContext) -> bool:
    """Legacy baseline helper — excludes long-render turns in the same job."""
    if turn_ctx.render_enqueued:
        return False
    return counts_as_meaningful_activity(turn_ctx)


def is_high_value_response_turn(turn_ctx: SupportCtaTurnContext) -> bool:
    """Returning CTA signal: substantive bot response (long text or image render)."""
    from app.services.conversation_bridge import _is_casual_greeting, _is_short_acknowledgment

    if turn_ctx.conversation_mode != "followup":
        return False
    if not turn_ctx.reading_delivered:
        return False
    if _is_casual_greeting(turn_ctx.user_message):
        return False
    if _is_short_acknowledgment(turn_ctx.user_message):
        return False
    if turn_ctx.render_enqueued:
        return True
    reply = (turn_ctx.reply_text or "").strip()
    return len(reply) >= SUPPORT_CTA_MIN_MEANINGFUL_REPLY_CHARS


def is_legacy_user_needing_baseline(
    meta: dict,
    *,
    has_reading: bool,
) -> bool:
    if not has_reading:
        return False
    if meta.get("initial_value_delivered_at"):
        return False
    if meta.get("initial_sent_at"):
        return False
    if meta.get("legacy_baseline_at"):
        return False
    return True


def record_initial_value_delivered_at(
    sender_id: str,
    *,
    generation_id: str,
    reading_id: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Record V1 delivery baseline — replaces immediate Donate on initial reading."""
    from app.services.messenger_state_db import DbMessengerStateStore

    ts = (now or _utcnow()).isoformat()
    patch: dict[str, Any] = {
        "initial_value_delivered_at": ts,
        "last_meaningful_activity_at": ts,
    }
    if reading_id is not None:
        patch["initial_reading_id"] = reading_id
    store = DbMessengerStateStore()
    return store.patch_support_cta_metadata_if_generation(
        sender_id, generation_id, patch,
    )


def supporter_cta_eligible(
    *,
    suppress_supporter_cta: bool,
    support_cta_meta: dict,
    premium_cta_meta: dict,
    premium_meta_available: bool = True,
    now: datetime | None = None,
) -> bool:
    if suppress_supporter_cta:
        return False
    if not premium_meta_available:
        return False
    from app.services.premium_cta_service import premium_consideration_window_active

    ref = now or _utcnow()
    if premium_consideration_window_active(premium_cta_meta, ref):
        return False
    return True


def should_send_support_cta(
    *,
    reason: str,
    meta: dict,
    sender_id: str,
    reading_id: int | None,
    generation_id: str,
    turn_ctx: SupportCtaTurnContext | None = None,
    previous_activity_at: datetime | None = None,
    render_source: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    from app.services.donate_service import is_verified_supporter

    _ = previous_activity_at  # retained for call-site compatibility; unused in returning rule

    if is_verified_supporter(sender_id):
        return False, "verified_supporter"
    if not has_support_cta_content():
        return False, "no_cta_content"

    if reason == REASON_INITIAL_READING:
        if turn_ctx and not turn_ctx.reading_delivered:
            return False, "delivery_not_success"
        if meta.get("initial_sent_at") or meta.get("initial_reading_id") is not None:
            return False, "initial_already_sent"
        if render_source and render_source not in ("generate", None):
            return False, f"render_source_{render_source}"
        if turn_ctx and turn_ctx.pending_initial:
            pending = meta.get("pending_initial") or {}
            if pending.get("generation_id") and pending.get("generation_id") != generation_id:
                return False, "pending_generation_mismatch"
        return True, "ok"

    if reason == REASON_RETURNING_VALUE:
        if turn_ctx is None or not turn_ctx.reading_delivered:
            return False, "delivery_not_success"
        if not is_high_value_response_turn(turn_ctx):
            return False, "not_high_value_turn"
        if not reading_id:
            return False, "no_reading"

        # Interval runs from the last CTA send OR from the value-delivery
        # baseline, whichever is later — a freshly written baseline must not
        # produce an immediate Donate.
        reference = _latest(
            _parse_iso(meta.get("last_sent_at")),
            _parse_iso(meta.get("initial_value_delivered_at")),
        )
        if reference is not None:
            hours_since_cta = _hours_since(reference, now=now)
            if hours_since_cta is None or hours_since_cta < SUPPORT_CTA_RETURNING_MIN_HOURS:
                return False, "returning_too_soon"

        return True, "ok"

    return False, f"unknown_reason_{reason}"


def build_cta_metadata_patch(
    *,
    reason: str = "",
    reading_id: int | None = None,
    now: datetime | None = None,
    clear_pending_initial: bool = False,
    legacy_baseline_only: bool = False,
    update_meaningful_activity: bool = False,
) -> dict[str, Any]:
    ts = (now or _utcnow()).isoformat()
    patch: dict[str, Any] = {}
    if legacy_baseline_only:
        patch["legacy_baseline_at"] = ts
        patch["last_meaningful_activity_at"] = ts
        # The baseline is also the value-delivery reference for this user, so the
        # returning-CTA interval is measured from it instead of firing at once.
        patch["initial_value_delivered_at"] = ts
        return patch
    if reason == REASON_INITIAL_READING:
        patch["initial_reading_id"] = reading_id
        patch["initial_sent_at"] = ts
        patch["last_sent_at"] = ts
        patch["last_reason"] = REASON_INITIAL_READING
        patch["last_meaningful_activity_at"] = ts
    elif reason == REASON_RETURNING_VALUE:
        patch["last_sent_at"] = ts
        patch["last_reason"] = REASON_RETURNING_VALUE
    if update_meaningful_activity:
        patch["last_meaningful_activity_at"] = ts
    if clear_pending_initial:
        patch["pending_initial"] = None
    return patch


def _send_cta_text(sender_id: str, text: str, *, request_id: str):
    from app.services.messenger_handler import send_outbound_user_text
    from app.services.outbound_validator import OUTBOUND_CLASS_PAYMENT_CTA

    return send_outbound_user_text(
        sender_id,
        text,
        request_id=request_id,
        outbound_class=OUTBOUND_CLASS_PAYMENT_CTA,
    )


def maybe_send_support_cta(
    sender_id: str,
    *,
    reason: str,
    request_id: str,
    generation_id: str,
    reading_id: int | None = None,
    turn_ctx: SupportCtaTurnContext | None = None,
    previous_activity_at: datetime | None = None,
    render_source: str | None = None,
    job_id: int = 0,
    job_kind: str = "chat_turn",
    suppress_supporter_cta: bool = False,
) -> bool:
    from app.services.generation_guard import skip_if_stale_generation
    from app.services.messenger_state_db import DbMessengerStateStore

    store = DbMessengerStateStore()
    session = store.get_or_create(sender_id)
    if skip_if_stale_generation(session, generation_id, job_id=job_id, job_kind=job_kind):
        return False

    meta = store.get_support_cta_metadata(sender_id)
    premium_meta: dict = {}
    premium_meta_available = True
    try:
        raw = store.get_premium_cta_metadata(sender_id)
        premium_meta = dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        premium_meta = {}
        premium_meta_available = False
    if not supporter_cta_eligible(
        suppress_supporter_cta=suppress_supporter_cta,
        support_cta_meta=meta,
        premium_cta_meta=premium_meta,
        premium_meta_available=premium_meta_available,
    ):
        logger.debug(
            "support_cta_skipped sender_id_hash=%s reason=%s skip=supporter_gate",
            sender_id[:8],
            reason,
        )
        return False

    ok, skip_reason = should_send_support_cta(
        reason=reason,
        meta=meta,
        sender_id=sender_id,
        reading_id=reading_id or session.reading_id,
        generation_id=generation_id,
        turn_ctx=turn_ctx,
        previous_activity_at=previous_activity_at,
        render_source=render_source,
    )
    if not ok:
        logger.debug(
            "support_cta_skipped sender_id_hash=%s reason=%s skip=%s",
            sender_id[:8],
            reason,
            skip_reason,
        )
        return False

    body = build_support_cta_message()
    if not body.strip():
        return False

    result = _send_cta_text(sender_id, body, request_id=request_id)
    if not result:
        logger.warning(
            "support_cta_send_failed sender_id_hash=%s reason=%s",
            sender_id[:8],
            reason,
        )
        return False

    patch = build_cta_metadata_patch(
        reason=reason,
        reading_id=reading_id or session.reading_id,
        clear_pending_initial=(reason == REASON_INITIAL_READING),
    )
    patched = store.patch_support_cta_metadata_if_generation(
        sender_id, generation_id, patch,
    )
    if not patched:
        logger.warning(
            "support_cta_patch_failed sender_id_hash=%s reason=%s",
            sender_id[:8],
            reason,
        )
    return True


def maybe_apply_legacy_baseline(
    sender_id: str,
    *,
    generation_id: str,
    turn_ctx: SupportCtaTurnContext,
    job_id: int = 0,
    job_kind: str = "chat_turn",
) -> bool:
    """First meaningful return for legacy user — set baseline, no CTA."""
    from app.services.generation_guard import skip_if_stale_generation
    from app.services.messenger_state_db import DbMessengerStateStore

    if not is_meaningful_followup_turn(turn_ctx):
        return False

    store = DbMessengerStateStore()
    session = store.get_or_create(sender_id)
    if skip_if_stale_generation(session, generation_id, job_id=job_id, job_kind=job_kind):
        return False

    meta = store.get_support_cta_metadata(sender_id)
    if not is_legacy_user_needing_baseline(meta, has_reading=bool(session.reading_id or session.chart_json)):
        return False

    patch = build_cta_metadata_patch(legacy_baseline_only=True)
    return store.patch_support_cta_metadata_if_generation(sender_id, generation_id, patch)


def patch_meaningful_activity_at(
    sender_id: str,
    *,
    generation_id: str,
) -> bool:
    from app.services.messenger_state_db import DbMessengerStateStore

    patch = build_cta_metadata_patch(update_meaningful_activity=True)
    return DbMessengerStateStore.patch_support_cta_metadata_if_generation(
        sender_id, generation_id, patch,
    )


def load_previous_meaningful_activity_at(sender_id: str) -> datetime | None:
    from app.services.messenger_state_db import DbMessengerStateStore

    meta = DbMessengerStateStore.get_support_cta_metadata(sender_id)
    return _parse_iso(meta.get("last_meaningful_activity_at"))
