"""Test helpers for PR-L1 chat_turn deferred pipeline."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from app.workers.queue import EnqueueResult


@contextmanager
def _noop_commit_guard(*args: Any, **kwargs: Any):
    yield


def sync_chat_turn_enqueue_with_status(
    kind: str,
    payload: dict[str, Any],
    **kwargs: Any,
) -> EnqueueResult:
    """Run chat_turn jobs inline so legacy pipeline tests keep end-to-end semantics."""
    if kind != "chat_turn":
        raise AssertionError(f"unexpected job kind in test shim: {kind}")
    from app.workers.handlers import handle_chat_turn

    job_id = 910000 + hash(kwargs.get("idempotency_key") or payload.get("request_id") or "") % 10000
    session = MagicMock()
    session.sender_id = payload.get("sender_id", "")
    session.generation_id = payload.get("generation_id", "")

    with (
        patch("app.services.messenger_state_db.DbMessengerStateStore") as store_cls,
        patch("app.services.generation_guard.skip_if_stale_job"),
        patch("app.services.generation_guard.check_fresh_or_raise"),
        patch("app.services.generation_guard.commit_guard", _noop_commit_guard),
        patch("app.services.busy_gate.verify_busy_owner_or_raise"),
        patch("app.services.busy_gate.release_busy"),
    ):
        store_cls.return_value.get_or_create.return_value = session
        payload.setdefault("busy_owner_id", "test-busy-owner")
        handle_chat_turn(payload, job_id=job_id, attempt=1)

    return EnqueueResult(
        job_id=job_id,
        created=True,
        existing_status="",
        existing_attempt=0,
        user_status="queued",
    )
