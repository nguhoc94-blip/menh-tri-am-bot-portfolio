"""Combined multimodal pipeline — source validation, enqueue, idempotency."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.services.conversation_bridge import _chart_summary
from app.services.feature_flags import MULTIMODAL_DISABLED_REPLY, is_multimodal_enabled
from app.services.messenger_state import ConversationState, MessengerSession
from app.services.async_job_metadata import JOB_PENDING, clear_pending_job_only, set_pending_job
from app.utils.sender_hash import hash_sender_id
from app.workers.queue import enqueue, enqueue_with_status, make_idempotency_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CombinedSources:
    palm_asset_id: str | None
    face_asset_id: str | None
    chart_summary: str | None


@dataclass(frozen=True)
class CombinedValidation:
    ok: bool
    reply: str | None = None
    sources: CombinedSources | None = None
    new_state: ConversationState | None = None


def _latest_asset_id(asset_ids: list[str] | None) -> str | None:
    if not asset_ids:
        return None
    return asset_ids[-1]


def _load_asset_row(asset_id: str) -> dict[str, Any] | None:
    from app.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sender_id, analysis_status, analysis_result, status, expires_at, mode
                FROM assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "sender_id": row[0],
        "analysis_status": row[1],
        "analysis_result": row[2],
        "status": row[3],
        "expires_at": row[4],
        "mode": row[5],
    }


def validate_asset_for_combined(
    asset_id: str,
    *,
    sender_id: str,
    expected_mode: str,
) -> tuple[bool, str | None, dict | None]:
    """Return (ok, error_reply, analysis_result)."""
    row = _load_asset_row(asset_id)
    if not row:
        return False, "Không tìm thấy ảnh nguồn. Bạn thử lại sau nhé.", None
    if row["sender_id"] != sender_id:
        return False, "Ảnh nguồn không thuộc phiên hiện tại.", None
    if row["status"] != "active":
        return False, "Ảnh nguồn đã hết hạn hoặc không còn hợp lệ.", None
    expires = row["expires_at"]
    if expires is not None and expires < datetime.now(timezone.utc):
        return False, "Ảnh nguồn đã hết hạn. Bạn gửi lại ảnh mới nhé.", None
    if row["mode"] and row["mode"] != expected_mode:
        return False, "Ảnh nguồn không đúng loại.", None
    status = row["analysis_status"]
    if status != "done":
        if status in ("running", "pending", None):
            return False, None, None  # pending — caller handles wait message
        return False, "Ảnh nguồn chưa phân tích xong. Bạn thử lại sau nhé.", None
    result = row["analysis_result"]
    if not result:
        return False, "Ảnh nguồn chưa có kết quả phân tích.", None
    if isinstance(result, str):
        import json

        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            result = {"raw_text": result}
    return True, None, result


def validate_combined_sources(
    session: MessengerSession,
    sender_id: str,
) -> CombinedValidation:
    """Validate chart + at least one analyzed palm/face source."""
    if not session.chart_json:
        return CombinedValidation(
            ok=False,
            reply=(
                "Để xem bản tổng hợp, mình cần có lá số tử vi trước. "
                "Bạn nhập ngày sinh để mình lập lá số nhé?"
            ),
            new_state=ConversationState.CHATTING,
        )

    palm_id = _latest_asset_id(session.palm_asset_ids)
    face_id = _latest_asset_id(session.face_asset_ids)

    if not palm_id and not face_id:
        return CombinedValidation(
            ok=False,
            reply=(
                "Để xem bản tổng hợp, mình cần thêm ảnh tay và/hoặc ảnh mặt. "
                "Bạn bắt đầu bằng ảnh bàn tay nhé?"
            ),
            new_state=ConversationState.INTAKE_PALM,
        )

    chart_summary = _chart_summary(session.chart_json)
    resolved_palm: str | None = None
    resolved_face: str | None = None

    if palm_id:
        ok, err, _ = validate_asset_for_combined(palm_id, sender_id=sender_id, expected_mode="palm")
        if err is None and not ok:
            return CombinedValidation(
                ok=False,
                reply="Mình đang phân tích ảnh tay trước đó. Bạn chờ thêm chút nhé.",
            )
        if not ok:
            return CombinedValidation(ok=False, reply=err or "Ảnh tay chưa sẵn sàng.")
        resolved_palm = palm_id

    if face_id:
        ok, err, _ = validate_asset_for_combined(face_id, sender_id=sender_id, expected_mode="face")
        if err is None and not ok:
            return CombinedValidation(
                ok=False,
                reply="Mình đang phân tích ảnh mặt trước đó. Bạn chờ thêm chút nhé.",
            )
        if not ok:
            return CombinedValidation(ok=False, reply=err or "Ảnh mặt chưa sẵn sàng.")
        resolved_face = face_id

    if not resolved_palm and not resolved_face:
        return CombinedValidation(
            ok=False,
            reply="Mình đang chờ kết quả phân tích ảnh. Bạn thử lại sau nhé.",
        )

    return CombinedValidation(
        ok=True,
        sources=CombinedSources(
            palm_asset_id=resolved_palm,
            face_asset_id=resolved_face,
            chart_summary=chart_summary,
        ),
    )


def combined_idempotency_key(
    sender_id: str,
    generation_id: str,
    palm_asset_id: str | None,
    face_asset_id: str | None,
) -> str:
    return make_idempotency_key(
        "analyze_combined",
        sender_id,
        generation_id,
        palm_asset_id or "",
        face_asset_id or "",
    )


def build_combined_job_payload(
    *,
    sender_id: str,
    generation_id: str,
    sources: CombinedSources,
    request_id: str,
) -> dict:
    return {
        "sender_id": sender_id,
        "generation_id": generation_id,
        "palm_asset_id": sources.palm_asset_id,
        "face_asset_id": sources.face_asset_id,
        "chart_summary": sources.chart_summary,
        "request_id": request_id,
    }


def try_enqueue_combined_analysis(
    session: MessengerSession,
    sender_id: str,
    request_id: str,
) -> tuple[bool, str]:
    """
    Validate sources, enqueue analyze_combined, set session metadata.

    Returns (success, user_message). On failure session state is not set to ANALYZING.
    """
    if not is_multimodal_enabled():
        return False, MULTIMODAL_DISABLED_REPLY

    validation = validate_combined_sources(session, sender_id)
    if not validation.ok or not validation.sources:
        if validation.new_state is not None:
            session.state = validation.new_state
        return False, validation.reply or "Chưa đủ dữ liệu để tổng hợp."

    sources = validation.sources
    gen = str(session.generation_id)
    payload = build_combined_job_payload(
        sender_id=sender_id,
        generation_id=gen,
        sources=sources,
        request_id=request_id,
    )
    ikey = combined_idempotency_key(
        sender_id, gen, sources.palm_asset_id, sources.face_asset_id,
    )

    result = enqueue_with_status(
        kind="analyze_combined",
        payload=payload,
        idempotency_key=ikey,
        max_attempts=3,
    )

    if result.user_status == "enqueue_error":
        clear_pending_job_only(session)
        return False, "Hệ thống đang bận, chưa thể tổng hợp. Bạn thử lại sau nhé."

    if result.user_status == "dead":
        return False, "Mình chưa tổng hợp được lần trước. Bạn thử lại sau nhé."

    if result.user_status == "completed":
        return False, "Bản tổng hợp đã được xử lý rồi. Bạn hỏi tiếp về lá số nhé."

    if result.user_status in ("already_running", "retry_pending", "new"):
        if not result.job_id:
            return False, "Hệ thống đang bận, chưa thể tổng hợp. Bạn thử lại sau nhé."
        set_pending_job(
            session,
            kind="analyze_combined",
            job_id=result.job_id,
            generation_id=gen,
            status=JOB_PENDING,
        )
        session.active_mode = "combined"
        session.state = ConversationState.ANALYZING
        return True, "Mình đang tổng hợp..."

    clear_pending_job_only(session)
    return False, "Chưa thể bắt đầu tổng hợp. Bạn thử lại sau nhé."
