"""
Long-running Messenger turns: send a one-time ack if processing is slow.

Started when the first batched text arrives (covers quiet-window wait + GPT).
Non-batched paths start at pipeline entry. Stopped when the pipeline finishes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

_DEFAULT_ACK_DELAY_SEC = 20.0
_DEFAULT_ACK_MESSAGE = (
    "Mình đang xử lý, và sẽ phản hồi ngay khi xong — "
    "bạn đừng đợi, tránh mất thời gian của bạn."
)

_registry_lock = threading.Lock()
_active: dict[str, "ProcessingFeedback"] = {}


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


def processing_ack_delay_sec() -> float:
    return _env_float("PROCESSING_ACK_DELAY_SEC", _DEFAULT_ACK_DELAY_SEC)


def processing_ack_message() -> str:
    return (os.environ.get("PROCESSING_ACK_MESSAGE") or _DEFAULT_ACK_MESSAGE).strip()


def _feedback_enabled(sender_id: str) -> bool:
    from app.services.debug_outbound import should_capture

    if should_capture(sender_id):
        return False
    if not (os.environ.get("FB_PAGE_ACCESS_TOKEN") or "").strip():
        return False
    flag = (os.environ.get("PROCESSING_FEEDBACK_ENABLED") or "1").strip().lower()
    return flag in ("1", "true", "yes")


class ProcessingFeedback:
    def __init__(
        self,
        sender_id: str,
        request_id: str,
        *,
        send_ack: Callable[[str, str, str], None] | None = None,
        ack_delay_sec: float | None = None,
        ack_message: str | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        self.sender_id = sender_id
        self.request_id = request_id
        self._send_ack = send_ack or _default_send_ack
        self._ack_delay_sec = (
            ack_delay_sec if ack_delay_sec is not None else processing_ack_delay_sec()
        )
        self._ack_message = ack_message if ack_message is not None else processing_ack_message()
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ack_sent = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"messenger-feedback-{self.sender_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        wait = self._sleep or (lambda ev, sec: ev.wait(sec))
        if wait(self._stop_event, self._ack_delay_sec):
            return
        if self._stop_event.is_set() or self._ack_sent:
            return
        self._ack_sent = True
        self._send_ack(self.sender_id, self.request_id, self._ack_message)
        logger.info(
            "processing_ack_sent request_id=%s sender_id=%s delay_sec=%.1f",
            self.request_id,
            self.sender_id,
            self._ack_delay_sec,
        )


def _default_send_ack(sender_id: str, request_id: str, message: str) -> None:
    from app.services.messenger_handler import send_outbound_user_text
    from app.services.outbound_validator import OUTBOUND_CLASS_SYSTEM_NOTICE

    send_outbound_user_text(
        sender_id,
        message,
        request_id=request_id,
        outbound_class=OUTBOUND_CLASS_SYSTEM_NOTICE,
    )


def stop_processing_feedback(sender_id: str) -> None:
    with _registry_lock:
        old = _active.pop(sender_id, None)
    if old is not None:
        old.stop()


def begin_processing_feedback(sender_id: str, request_id: str) -> ProcessingFeedback | None:
    if not _feedback_enabled(sender_id):
        return None
    fb = ProcessingFeedback(sender_id, request_id)
    with _registry_lock:
        old = _active.pop(sender_id, None)
        if old is not None:
            old.stop()
        _active[sender_id] = fb
    fb.start()
    return fb


def take_processing_feedback(sender_id: str) -> ProcessingFeedback | None:
    with _registry_lock:
        return _active.pop(sender_id, None)


def ensure_processing_feedback(sender_id: str, request_id: str) -> ProcessingFeedback | None:
    fb = take_processing_feedback(sender_id)
    if fb is not None:
        return fb
    return begin_processing_feedback(sender_id, request_id)
