"""Foreground conversational operation BUSY gate for Messenger sessions."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.messenger_state_db import DbMessengerStateStore
from app.utils.sender_hash import hash_sender_id
from app.workers.job_outcome import CancelledStaleError

logger = logging.getLogger(__name__)

_BUSY_PREVIEW_SHORT_MAX = 80
_BUSY_PREVIEW_EDGE_CHARS = 35
_BUSY_PREVIEW_STORE_MAX = 500

BUSY_MERGE_HINT = (
    "Lần sau, hãy nhắn lời tâm tình của bạn thành một tin nhắn, "
    "tránh gửi quá nhiều tin để mình tiện hiểu bạn nhé 🌿"
)

_DEFAULT_BUSY_MAX_LIFETIME_SECONDS = 1800


def busy_max_lifetime_seconds() -> int:
    raw = (os.environ.get("BUSY_MAX_LIFETIME_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_BUSY_MAX_LIFETIME_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_BUSY_MAX_LIFETIME_SECONDS


def _normalize_preview_text(text: str) -> str:
    return " ".join(text.split())


def format_request_preview(text: str | None) -> str | None:
    """Short preview for BUSY notice: full text if short, else head…tail."""
    if not text or not text.strip():
        return None
    normalized = _normalize_preview_text(text.strip())
    if len(normalized) <= _BUSY_PREVIEW_SHORT_MAX:
        return normalized
    head = normalized[:_BUSY_PREVIEW_EDGE_CHARS]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    tail = normalized[-_BUSY_PREVIEW_EDGE_CHARS:]
    if " " in tail:
        tail = tail.split(" ", 1)[-1]
    return f"{head}…{tail}"


def format_busy_notice(request_preview: str | None = None) -> str:
    if request_preview:
        core = (
            f'Mình đang xử lý yêu cầu trước của bạn ("{request_preview}") '
            "nên chưa thể nhận thêm nội dung lúc này. "
            "Bạn đợi mình hoàn thành rồi nhắn tiếp nhé 🌿"
        )
    else:
        core = (
            "Mình đang xử lý yêu cầu trước của bạn nên chưa thể nhận thêm nội dung lúc này. "
            "Bạn đợi mình hoàn thành rồi nhắn tiếp nhé 🌿"
        )
    return f"{core}\n\n{BUSY_MERGE_HINT}"


# Backward-compatible alias for tests that assert generic notice shape.
BUSY_NOTICE_TEXT = format_busy_notice()


@dataclass(frozen=True)
class BusyStatus:
    is_busy: bool
    owner_id: str | None = None
    started_at: datetime | None = None
    notice_sent: bool = False
    request_preview: str | None = None
    expired_reclaimed: bool = False


def _parse_started_at(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    return None


def _stored_request_preview(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def read_busy_status(sender_id: str) -> BusyStatus:
    row = DbMessengerStateStore.read_busy_status(sender_id)
    if row is None:
        return BusyStatus(is_busy=False)
    owner_raw = row.get("busy_owner_id")
    if owner_raw is None:
        return BusyStatus(is_busy=False)
    owner_id = str(owner_raw)
    started_at = _parse_started_at(row.get("busy_started_at"))
    notice_sent = bool(row.get("busy_notice_sent"))
    request_preview = _stored_request_preview(row.get("busy_request_preview"))
    if started_at is not None:
        age_sec = (datetime.now(timezone.utc) - started_at).total_seconds()
        if age_sec > busy_max_lifetime_seconds():
            logger.info(
                "busy_expired_reclaimed sender_id_hash=%s busy_owner_id=%s age_sec=%.0f",
                hash_sender_id(sender_id),
                owner_id,
                age_sec,
            )
            return BusyStatus(
                is_busy=False,
                owner_id=owner_id,
                started_at=started_at,
                notice_sent=notice_sent,
                request_preview=request_preview,
                expired_reclaimed=True,
            )
    return BusyStatus(
        is_busy=True,
        owner_id=owner_id,
        started_at=started_at,
        notice_sent=notice_sent,
        request_preview=request_preview,
    )


def claim_busy_for_operation(
    sender_id: str,
    *,
    request_text: str | None = None,
) -> str | None:
    owner_id = str(uuid.uuid4())
    preview = format_request_preview(request_text)
    stored_preview = None
    if request_text and request_text.strip():
        stored_preview = _normalize_preview_text(request_text.strip())[
            :_BUSY_PREVIEW_STORE_MAX
        ]
    claimed = DbMessengerStateStore.claim_busy(
        sender_id,
        owner_id,
        max_lifetime_seconds=busy_max_lifetime_seconds(),
        request_preview=stored_preview,
    )
    if not claimed:
        return None
    logger.info(
        "busy_claimed sender_id_hash=%s busy_owner_id=%s has_preview=%s",
        hash_sender_id(sender_id),
        owner_id,
        bool(preview),
    )
    return owner_id


def release_busy(
    sender_id: str,
    owner_id: str | None,
    *,
    reason: str = "busy_cleared",
    request_id: str | None = None,
    job_id: int | None = None,
    generation_id: str | None = None,
) -> bool:
    if not owner_id:
        return False
    cleared = DbMessengerStateStore.clear_busy_if_owner_matches(sender_id, owner_id)
    if cleared:
        logger.info(
            "%s sender_id_hash=%s busy_owner_id=%s request_id=%s job_id=%s generation_id=%s",
            reason,
            hash_sender_id(sender_id),
            owner_id,
            request_id or "",
            job_id if job_id is not None else "",
            generation_id or "",
        )
        return True
    logger.info(
        "busy_clear_skipped_stale sender_id_hash=%s busy_owner_id=%s request_id=%s job_id=%s",
        hash_sender_id(sender_id),
        owner_id,
        request_id or "",
        job_id if job_id is not None else "",
    )
    return False


def release_busy_after_send_failure(
    sender_id: str,
    owner_id: str | None,
    *,
    request_id: str | None = None,
    job_id: int | None = None,
    generation_id: str | None = None,
) -> bool:
    return release_busy(
        sender_id,
        owner_id,
        reason="busy_cleared_after_send_failure",
        request_id=request_id,
        job_id=job_id,
        generation_id=generation_id,
    )


def force_clear_busy(sender_id: str) -> None:
    DbMessengerStateStore.force_clear_busy(sender_id)
    logger.info(
        "busy_cleared sender_id_hash=%s busy_owner_id=force reason=force_clear",
        hash_sender_id(sender_id),
    )


def mark_notice_sent(sender_id: str, owner_id: str) -> bool:
    return DbMessengerStateStore.mark_busy_notice_sent(sender_id, owner_id)


def verify_busy_owner_or_raise(
    sender_id: str,
    owner_id: str | None,
    *,
    job_id: int,
    job_kind: str,
) -> None:
    if not owner_id:
        return
    status = read_busy_status(sender_id)
    if not status.is_busy or status.owner_id != owner_id:
        logger.info(
            "busy_owner_mismatch sender_id_hash=%s expected_busy_owner_id=%s "
            "live_busy_owner_id=%s job_id=%s job_kind=%s",
            hash_sender_id(sender_id),
            owner_id,
            status.owner_id or "",
            job_id,
            job_kind,
        )
        raise CancelledStaleError(
            f"job {job_id} ({job_kind}) busy owner superseded"
        )
    logger.info(
        "busy_owner_verified sender_id_hash=%s busy_owner_id=%s job_id=%s job_kind=%s",
        hash_sender_id(sender_id),
        owner_id,
        job_id,
        job_kind,
    )


def reject_if_busy(sender_id: str, *, request_id: str) -> bool:
    """Return True when the message was rejected (caller should stop processing)."""
    status = read_busy_status(sender_id)
    if not status.is_busy or not status.owner_id:
        return False
    logger.info(
        "busy_rejected_message sender_id_hash=%s busy_owner_id=%s request_id=%s",
        hash_sender_id(sender_id),
        status.owner_id,
        request_id,
    )
    if status.notice_sent:
        return True
    from app.services.messenger_handler import send_outbound_user_text
    from app.services.outbound_validator import OUTBOUND_CLASS_SYSTEM_NOTICE

    preview = format_request_preview(status.request_preview)
    notice_text = format_busy_notice(preview)
    send_outbound_user_text(
        sender_id,
        notice_text,
        request_id=request_id,
        outbound_class=OUTBOUND_CLASS_SYSTEM_NOTICE,
    )
    if mark_notice_sent(sender_id, status.owner_id):
        logger.info(
            "busy_notice_sent sender_id_hash=%s busy_owner_id=%s request_id=%s",
            hash_sender_id(sender_id),
            status.owner_id,
            request_id,
        )
    return True
