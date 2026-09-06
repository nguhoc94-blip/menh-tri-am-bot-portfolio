"""PR-L2: user-visible progress messages for the chat_turn job path.

Scope: applies ONLY to the chat_turn job (app/workers/handlers.py::handle_chat_turn
and the conversation_bridge functions it calls into). The older image-analysis
worker pipeline (handle_analyze_image / handle_analyze_combined / handle_render_asset
/ handle_send_asset / handle_verify_donate) is intentionally untouched.

Two independent send paths exist because of a thread-safety constraint:

1. `send_progress_stage()` — for the charting/analyzing/rendering stage messages,
   called synchronously on the worker's main thread, deep inside
   `handle_incoming_text`'s call tree. It relies on the ambient `commit_guard`
   ContextVar (see app.services.generation_guard) being visible on that same
   thread, so it can fail fast via `check_fresh_or_raise` (propagates
   CancelledStaleError — no progress message, no further work, no final reply
   for a superseded turn).

2. `make_queued_ack_sender()` — for the universal "queued" grace-period ack,
   which `ProcessingFeedback` fires from a spawned `threading.Thread`. ContextVars
   set on the main worker thread are NOT visible from that spawned thread, so this
   path must NOT use `check_fresh_or_raise`/the ambient guard. Instead it captures
   `job_id`/`expected_generation_id` as plain closure values (safe: read-only,
   captured at thread-creation time) and does a direct live-DB comparison against
   `DbMessengerStateStore().get_current_generation_id()`, silently skipping (never
   raising) when stale — this callable runs on a background thread where an
   uncaught exception would just be swallowed/lost anyway.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from app.db import get_connection

logger = logging.getLogger(__name__)

STAGE_QUEUED = "queued"
STAGE_CHARTING = "charting"
STAGE_ANALYZING = "analyzing"
STAGE_RENDERING = "rendering"

QUEUED_ACK_MESSAGES: tuple[str, ...] = (
    "Mình đang xử lý thông tin bạn vừa gửi nhé. Bạn vẫn có thể nhắn bổ sung hoặc chỉnh sửa nếu cần.",
    "Mình đã nhận được thông tin và đang xử lý rồi nhé. Có gì muốn thêm hoặc sửa, bạn cứ nhắn tiếp nha.",
    "Mình đang xem nội dung bạn vừa gửi. Nếu cần bổ sung thêm thông tin, bạn cứ gửi tiếp nhé.",
    "Thông tin của bạn đang được mình xử lý nhé. Bạn có thể cập nhật thêm bất cứ lúc nào.",
    "Mình đang xử lý phần thông tin vừa nhận được. Nếu có gì cần thay đổi, bạn cứ nhắn thêm nha.",
    "Mình đã nhận được nội dung của bạn rồi nhé. Trong lúc xử lý, bạn vẫn có thể bổ sung thêm thông tin.",
    "Mình đang tiếp tục xử lý yêu cầu của bạn. Nếu cần sửa hoặc thêm chi tiết nào, cứ nhắn mình nhé.",
    "Nội dung bạn vừa gửi đang được xử lý rồi nha. Bạn vẫn có thể gửi thêm nếu còn thông tin cần bổ sung.",
    "Mình đang tổng hợp các thông tin bạn vừa cung cấp. Có gì cần cập nhật thêm thì cứ nhắn tiếp nhé.",
    "Mình đang xử lý nội dung vừa nhận được nhé. Nếu muốn chỉnh sửa gì, bạn cứ gửi thêm cho mình.",
    "Mình đã nhận thông tin rồi và đang xử lý nha. Bạn có thể nhắn thêm bất kỳ chi tiết nào nếu cần.",
    "Mình đang xem qua thông tin của bạn nhé. Trong lúc này, bạn vẫn có thể bổ sung hoặc sửa lại nội dung.",
    "Mình đang xử lý những gì bạn vừa gửi. Nếu còn thiếu chi tiết nào, bạn cứ gửi thêm nha.",
    "Thông tin đã được ghi nhận và đang được xử lý nhé. Bạn vẫn có thể cập nhật thêm nếu cần.",
    "Mình đang xử lý yêu cầu này rồi nha. Có gì muốn bổ sung hoặc điều chỉnh thì cứ nhắn tiếp nhé.",
    "Mình đã nhận được các thông tin bạn gửi. Nếu có thêm nội dung mới, bạn cứ gửi tiếp trong lúc mình xử lý nhé.",
    "Mình đang kiểm tra và xử lý thông tin của bạn. Bạn có thể bổ sung thêm chi tiết bất cứ lúc nào nha.",
    "Mình đang làm việc với nội dung bạn vừa gửi nhé. Nếu cần thay đổi gì, bạn cứ nhắn thêm.",
    "Yêu cầu của bạn đang được xử lý rồi nha. Trong lúc này, bạn vẫn có thể gửi thêm thông tin nếu muốn.",
    "Mình đang tiếp nhận và xử lý các thông tin vừa rồi. Có gì cần sửa hoặc bổ sung, bạn cứ nhắn nhé.",
    "Mình đang xử lý nội dung này cho bạn nhé. Nếu nhớ ra thêm chi tiết nào, bạn cứ gửi tiếp nha.",
    "Thông tin của bạn mình đã nhận được rồi. Mình đang xử lý và bạn vẫn có thể cập nhật thêm nếu cần.",
    "Mình đang xem xét các thông tin bạn vừa gửi nhé. Bạn cứ thoải mái bổ sung thêm nội dung nếu có.",
    "Mình đang xử lý phần này rồi nha. Nếu có thông tin mới hoặc cần chỉnh sửa, bạn cứ gửi thêm nhé.",
    "Mình đã ghi nhận nội dung bạn vừa gửi và đang xử lý. Trong lúc này, bạn vẫn có thể nhắn bổ sung nha.",
    "Mình đang xử lý thông tin cho bạn nhé. Có chi tiết nào cần thêm hoặc sửa thì cứ nhắn tiếp.",
    "Mình đang tiếp tục xử lý những thông tin vừa nhận được. Bạn có thể cập nhật thêm bất cứ khi nào cần nhé.",
    "Nội dung của bạn đang được mình xử lý rồi nha. Nếu muốn bổ sung gì thêm, cứ nhắn tiếp nhé.",
    "Mình đã nhận đủ phần thông tin hiện tại và đang xử lý. Nếu có thay đổi gì, bạn vẫn có thể gửi thêm nha.",
    "Mình đang xử lý yêu cầu của bạn nhé. Trong lúc chờ kết quả, bạn cứ gửi thêm hoặc sửa thông tin nếu cần.",
)

STAGE_QUEUED_MESSAGE = QUEUED_ACK_MESSAGES[0]


def queued_ack_message_for_job(job_id: int) -> str:
    """Deterministic queued ack copy — no session persistence (PR-B)."""
    return QUEUED_ACK_MESSAGES[job_id % len(QUEUED_ACK_MESSAGES)]

_STAGE_MESSAGES: dict[str, str] = {
    STAGE_QUEUED: STAGE_QUEUED_MESSAGE,
    STAGE_CHARTING: "Mình đang lập lá số…",
    STAGE_ANALYZING: "Mình đang phân tích nội dung…",
    STAGE_RENDERING: "Mình đang trình bày kết quả thành bản dễ đọc…",
}

_DEFAULT_QUEUED_GRACE_SEC = 2.5
_MIN_QUEUED_GRACE_SEC = 0.5


def queued_grace_sec() -> float:
    """Parse CHAT_TURN_QUEUED_GRACE_SEC; default 2.5, floor 0.5.

    Defensive style matches app.services.runtime_metrics.validate_metrics_interval_sec:
    a set-but-invalid or below-floor value logs a warning and falls back to the
    default, never raises. Unlike that function, an *unset* env var (the expected
    steady-state config for most deployments) does not log a warning — this is
    read on every chat_turn job, so warning on the common "not configured" case
    would just be noise.
    """
    fallback = _DEFAULT_QUEUED_GRACE_SEC
    raw = os.environ.get("CHAT_TURN_QUEUED_GRACE_SEC")

    if raw is None or not str(raw).strip():
        return fallback

    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "chat_turn_queued_grace_sec_invalid raw=%r fallback=%s", raw, fallback,
        )
        return fallback

    if value < _MIN_QUEUED_GRACE_SEC:
        logger.warning(
            "chat_turn_queued_grace_sec_below_floor raw=%r floor=%s fallback=%s",
            raw, _MIN_QUEUED_GRACE_SEC, fallback,
        )
        return fallback

    return value


def queued_ack_enabled() -> bool:
    """Kill switch for chat_turn grace-period queued ack (PR-B rotation)."""
    flag = (os.environ.get("QUEUED_ACK_ENABLED") or "1").strip().lower()
    return flag in ("1", "true", "yes")


def record_progress_sent(job_id: int, stage: str) -> bool:
    """Idempotency primitive used by every progress send path in this module.

    Returns True the first time this exact (job_id, stage) pair is recorded —
    the caller should proceed to send the message. Returns False when the pair
    already existed — the caller must not send again.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_progress_sent (job_id, stage)
                VALUES (%s, %s)
                ON CONFLICT (job_id, stage) DO NOTHING
                RETURNING job_id
                """,
                (job_id, stage),
            )
            row = cur.fetchone()
    return row is not None


_progress_ctx: ContextVar[tuple[int, str, str] | None] = ContextVar(
    "chat_turn_progress_ctx", default=None,
)


@contextmanager
def progress_scope(job_id: int, sender_id: str, request_id: str) -> Iterator[None]:
    """Ambient scope for send_progress_stage(). Orthogonal to commit_guard — this
    ContextVar only carries (job_id, sender_id, request_id) for progress sends;
    it does not itself gate writes (commit_guard/check_fresh_or_raise still do)."""
    token = _progress_ctx.set((job_id, sender_id, request_id))
    try:
        yield
    finally:
        _progress_ctx.reset(token)


def send_progress_stage(stage: str) -> None:
    """Send a stage progress message — MAIN WORKER THREAD ONLY.

    No-op (safe) if no progress_scope is active — this lets callers like
    conversation_bridge._mode_generate() call this unconditionally even when
    invoked outside a chat_turn job (e.g. from a unit test or another flow).

    Deliberately does NOT catch CancelledStaleError from check_fresh_or_raise —
    it must propagate so the calling function aborts immediately: no point
    building a chart or calling GPT for a superseded turn. This is only safe to
    call synchronously on the thread that entered progress_scope()/commit_guard()
    (see module docstring) — never from a spawned threading.Thread.
    """
    ctx = _progress_ctx.get()
    if ctx is None:
        return
    job_id, sender_id, request_id = ctx

    from app.services.generation_guard import check_fresh_or_raise

    check_fresh_or_raise(sender_id, job_id=job_id, job_kind="chat_turn")

    if not record_progress_sent(job_id, stage):
        return

    message = _STAGE_MESSAGES.get(stage)
    if not message:
        return

    from app.services.messenger_handler import send_outbound_user_text
    from app.services.outbound_validator import OUTBOUND_CLASS_SYSTEM_NOTICE

    send_outbound_user_text(
        sender_id,
        message,
        request_id=request_id,
        outbound_class=OUTBOUND_CLASS_SYSTEM_NOTICE,
    )


def make_queued_ack_sender(
    *, job_id: int, expected_generation_id: str,
) -> Callable[[str, str, str], None]:
    """Factory for a `send_ack` callable safe to run on ProcessingFeedback's
    background thread (see module docstring for why this can't use the ambient
    commit_guard ContextVar / check_fresh_or_raise)."""

    def _send(sender_id: str, request_id: str, message: str) -> None:
        from app.services.messenger_state_db import DbMessengerStateStore

        current = DbMessengerStateStore().get_current_generation_id(sender_id)
        if current is None or str(current) != str(expected_generation_id):
            return  # stale — skip silently, do not raise (background thread)
        if not record_progress_sent(job_id, STAGE_QUEUED):
            return
        from app.services.messenger_handler import send_outbound_user_text
        from app.services.outbound_validator import OUTBOUND_CLASS_SYSTEM_NOTICE

        send_outbound_user_text(
            sender_id,
            message,
            request_id=request_id,
            outbound_class=OUTBOUND_CLASS_SYSTEM_NOTICE,
        )

    return _send
