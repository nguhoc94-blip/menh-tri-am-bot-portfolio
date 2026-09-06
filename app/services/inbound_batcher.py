"""Quiet-window inbound message batching for Messenger text."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

_DEFAULT_QUIET_SECONDS = 8.0
_DEFAULT_MAX_WAIT_SECONDS = 30.0
_DEFAULT_MAX_MESSAGES = 10
_DEFAULT_MERGE_SEPARATOR = "\n"

FlushCallback = Callable[[str, list["PendingInbound"]], None]
FirstMessageCallback = Callable[[str, "PendingInbound"], None]


@dataclass(frozen=True)
class PendingInbound:
    request_id: str
    text: str
    event: dict[str, Any]
    received_at: float


@dataclass
class _SenderBatch:
    sender_id: str
    messages: list[PendingInbound] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    timer: threading.Timer | None = None


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def get_batch_quiet_seconds() -> float:
    return _env_float("BATCH_QUIET_SECONDS", _DEFAULT_QUIET_SECONDS)


def get_batch_max_wait_seconds() -> float:
    return _env_float("BATCH_MAX_WAIT_SECONDS", _DEFAULT_MAX_WAIT_SECONDS)


def get_batch_max_messages() -> int:
    return _env_int("BATCH_MAX_MESSAGES", _DEFAULT_MAX_MESSAGES)


def get_batch_merge_separator() -> str:
    raw = (os.environ.get("BATCH_MERGE_SEPARATOR") or "").strip()
    if raw == "\\n":
        return "\n"
    return raw if raw else _DEFAULT_MERGE_SEPARATOR


def merge_batch_text(entries: list[PendingInbound]) -> str:
    sep = get_batch_merge_separator()
    parts = [entry.text.strip() for entry in entries if entry.text.strip()]
    return sep.join(parts)


class InboundMessageBatcher:
    """Per-sender quiet-window batching: merge rapid text messages into one GPT turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._batches: dict[str, _SenderBatch] = {}

    def enqueue(
        self,
        sender_id: str,
        text: str,
        event: dict[str, Any],
        request_id: str,
        *,
        on_flush: FlushCallback,
        on_first_message: FirstMessageCallback | None = None,
    ) -> None:
        quiet = get_batch_quiet_seconds()
        now = time.monotonic()
        flush_now = False

        with self._lock:
            batch = self._batches.get(sender_id)
            first_in_batch = batch is None
            if batch is None:
                batch = _SenderBatch(sender_id=sender_id)
                self._batches[sender_id] = batch

            entry = PendingInbound(
                request_id=request_id,
                text=text,
                event=event,
                received_at=now,
            )
            batch.messages.append(entry)

            if first_in_batch and on_first_message is not None:
                on_first_message(sender_id, entry)

            max_messages = get_batch_max_messages()
            max_wait = get_batch_max_wait_seconds()
            elapsed = now - batch.started_at
            if quiet <= 0 or len(batch.messages) >= max_messages or elapsed >= max_wait:
                flush_now = True
            else:
                self._schedule_timer_locked(batch, on_flush, quiet_seconds=quiet)

        if flush_now:
            self.flush_now(sender_id, on_flush=on_flush)

    def flush_now(
        self,
        sender_id: str,
        *,
        on_flush: FlushCallback | None = None,
    ) -> None:
        with self._lock:
            entries = self._pop_batch_locked(sender_id)
        if not entries or on_flush is None:
            return
        self._invoke_flush(sender_id, entries, on_flush)

    def cancel_pending(self, sender_id: str) -> int:
        """Drop queued messages for sender without flushing (e.g. session reset)."""
        with self._lock:
            entries = self._pop_batch_locked(sender_id)
        if entries:
            logger.info(
                "inbound_batch_cancelled sender_id=%s count=%s request_ids=%s event=batch_cancelled",
                sender_id,
                len(entries),
                [entry.request_id for entry in entries],
            )
        return len(entries)

    def _schedule_timer_locked(
        self,
        batch: _SenderBatch,
        on_flush: FlushCallback,
        *,
        quiet_seconds: float,
    ) -> None:
        if batch.timer is not None:
            batch.timer.cancel()
            batch.timer = None

        max_wait = get_batch_max_wait_seconds()
        elapsed = time.monotonic() - batch.started_at
        remaining_max = max(0.0, max_wait - elapsed)
        delay = min(quiet_seconds, remaining_max)
        if delay <= 0:
            entries = self._pop_batch_locked(batch.sender_id)
            if entries:
                threading.Thread(
                    target=self._invoke_flush,
                    args=(batch.sender_id, entries, on_flush),
                    daemon=True,
                ).start()
            return

        batch.timer = threading.Timer(
            delay,
            self._timer_fire,
            args=(batch.sender_id, on_flush),
        )
        batch.timer.daemon = True
        batch.timer.start()

    def _timer_fire(self, sender_id: str, on_flush: FlushCallback) -> None:
        with self._lock:
            batch = self._batches.get(sender_id)
            if batch is None or not batch.messages:
                return
            batch.timer = None
            quiet = get_batch_quiet_seconds()
            max_wait = get_batch_max_wait_seconds()
            elapsed = time.monotonic() - batch.started_at
            last = batch.messages[-1]
            quiet_elapsed = time.monotonic() - last.received_at
            if quiet_elapsed + 1e-6 < quiet and elapsed < max_wait:
                self._schedule_timer_locked(
                    batch,
                    on_flush,
                    quiet_seconds=max(0.0, quiet - quiet_elapsed),
                )
                return
            entries = self._pop_batch_locked(sender_id)

        if entries:
            self._invoke_flush(sender_id, entries, on_flush)

    def _pop_batch_locked(self, sender_id: str) -> list[PendingInbound]:
        batch = self._batches.pop(sender_id, None)
        if batch is None:
            return []
        if batch.timer is not None:
            batch.timer.cancel()
            batch.timer = None
        return list(batch.messages)

    @staticmethod
    def _invoke_flush(
        sender_id: str,
        entries: list[PendingInbound],
        on_flush: FlushCallback,
    ) -> None:
        merged_len = len(merge_batch_text(entries))
        logger.info(
            "inbound_batch_flushed sender_id=%s count=%s merged_chars=%s "
            "request_ids=%s event=batch_flushed",
            sender_id,
            len(entries),
            merged_len,
            [entry.request_id for entry in entries],
        )
        try:
            on_flush(sender_id, entries)
        except Exception:
            logger.exception(
                "inbound_batch_flush_failed sender_id=%s count=%s event=batch_flush_failed",
                sender_id,
                len(entries),
            )


_batcher = InboundMessageBatcher()


def get_inbound_batcher() -> InboundMessageBatcher:
    return _batcher
