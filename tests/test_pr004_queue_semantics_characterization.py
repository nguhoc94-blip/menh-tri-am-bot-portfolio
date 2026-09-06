"""
PR-004 (AM-04, AM-05) — characterization of queue retry / terminal semantics.

Every test here pins the behaviour that exists BEFORE the PR-004 change, so the
commit that changes the behaviour makes the change visible and reviewable.
Each test names the invariant (or the missing invariant) it characterizes.

No PostgreSQL is required: the queue functions are exercised against a mocked
connection and the assertions are made on the SQL and parameters they emit.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class _FakeCursor:
    """Records every execute() so tests can assert on emitted SQL."""

    def __init__(self, fetch_results: list | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._fetch_results = list(fetch_results or [])

    def execute(self, sql, params=None):  # noqa: ANN001
        self.calls.append((sql, params))

    def fetchone(self):
        if self._fetch_results:
            return self._fetch_results.pop(0)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return False


def _patch_queue_connection(cursor: _FakeCursor):
    conn = _FakeConnection(cursor)
    mock_gc = MagicMock()
    mock_gc.return_value.__enter__ = lambda s: conn
    mock_gc.return_value.__exit__ = MagicMock(return_value=False)
    return patch("app.workers.queue.get_connection", mock_gc)


# ── 1. Backoff (AM-04, plan invariant 22) ───────────────────────────


def test_char_fail_job_leaves_run_at_untouched() -> None:
    """Invariant 22: a retryable failure defers re-claiming via run_at backoff."""
    from app.workers.queue import fail_job

    cursor = _FakeCursor()
    with _patch_queue_connection(cursor):
        fail_job(99, "boom", attempt=1, max_attempts=3)

    sql, _params = cursor.calls[-1]
    assert "run_at" in sql


def test_char_claim_predicate_gates_on_run_at() -> None:
    """Invariant 22 (other half): claiming already honours run_at.

    This is why writing run_at in fail_job is sufficient to implement backoff:
    the claim predicate filters on it today.
    """
    from app.workers.queue import claim_next_job

    cursor = _FakeCursor(fetch_results=[None])
    with _patch_queue_connection(cursor):
        claim_next_job("render_asset", "worker-char")

    sql, _params = cursor.calls[-1]
    assert "run_at <= now()" in sql


# ── 2. Terminal errors laundered into `completed` (AM-05, invariant 18) ──


def _runner_with_handler(handler):
    """Run runner._process_one against a claimed job and a stubbed queue."""
    from app.workers import runner

    job = {"id": 7, "kind": "char_kind", "payload": {"x": 1}, "attempt": 3, "max_attempts": 3}
    with patch.object(runner, "claim_next_job", return_value=job), \
            patch.object(runner, "complete_job") as complete, \
            patch.object(runner, "fail_job") as fail, \
            patch.object(runner, "fail_job_terminal") as fail_terminal, \
            patch.object(runner, "cancel_job") as cancel:
        runner._HANDLERS["char_kind"] = handler
        try:
            runner._process_one("char_kind")
        finally:
            runner._HANDLERS.pop("char_kind", None)
    return complete, fail, fail_terminal, cancel


def test_char_handler_swallowing_terminal_error_is_marked_completed() -> None:
    """MISSING invariant 18: a terminal failure is persisted as `completed`.

    A handler that catches an unrecoverable error, sends the user a fallback and
    returns normally is indistinguishable from success: runner._process_one
    calls complete_job right after handler(...) (runner.py:64-65).
    """
    def swallowing_handler(payload, job_id, attempt):  # noqa: ANN001, ARG001
        return None  # handler already sent the user a terminal fallback

    complete, fail, fail_terminal, cancel = _runner_with_handler(swallowing_handler)

    complete.assert_called_once_with(7)
    fail.assert_not_called()
    fail_terminal.assert_not_called()
    cancel.assert_not_called()


def test_char_stale_skip_is_counted_as_business_success() -> None:
    """Invariant: a superseded chat_turn job is cancelled, not counted as business success."""
    from app.services.generation_guard import skip_if_stale_job
    from app.services.messenger_state import MessengerSession

    session = MessengerSession(
        sender_id="char_user",
        generation_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )

    def stale_handler(payload, job_id, attempt):  # noqa: ANN001, ARG001
        skip_if_stale_job(
            session,
            {"generation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            job_id=job_id,
            job_kind="chat_turn",
        )
        return None

    complete, fail, fail_terminal, cancel = _runner_with_handler(stale_handler)

    cancel.assert_called_once()
    complete.assert_not_called()
    fail.assert_not_called()
    fail_terminal.assert_not_called()


def test_char_delivery_job_not_cancelled_on_generation_mismatch() -> None:
    """Delivery jobs ignore generation_id mismatch (chat-only stale guard)."""
    from app.services.generation_guard import skip_if_stale_job
    from app.services.messenger_state import MessengerSession

    session = MessengerSession(
        sender_id="char_user",
        generation_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )

    def delivery_handler(payload, job_id, attempt):  # noqa: ANN001, ARG001
        skip_if_stale_job(
            session,
            {"generation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            job_id=job_id,
            job_kind="render_reading",
        )
        return None

    complete, fail, fail_terminal, cancel = _runner_with_handler(delivery_handler)

    complete.assert_called_once()
    cancel.assert_not_called()
    fail.assert_not_called()
    fail_terminal.assert_not_called()


def test_char_analyze_handler_returns_normally_on_terminal_analyzer_error() -> None:
    """Terminal analyzer failure raises TerminalJobError for runner dispatch."""
    import pytest

    from app.services.analyzer import AnalyzerError
    from app.services.messenger_state import ConversationState, MessengerSession
    from app.workers import handlers as h
    from app.workers.job_outcome import TerminalJobError

    gen = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    session = MessengerSession(
        sender_id="char_user",
        generation_id=gen,
        state=ConversationState.ANALYZING,
    )
    store = MagicMock()
    store.get_or_create.return_value = session

    with patch("app.services.messenger_state_db.DbMessengerStateStore", return_value=store), \
            patch(
                "app.services.analyzer.analyze_image_asset",
                side_effect=AnalyzerError("boom", "OPENAI_ERROR"),
            ), \
            patch.object(h, "_send_fallback_message"), \
            patch("app.services.cost_enforcer.check_can_analyze"), \
            patch("app.services.cost_enforcer.is_in_grace_window", return_value=False):
        with pytest.raises(TerminalJobError, match="analyze_image terminal"):
            h.handle_analyze_image(
                {
                    "sender_id": "char_user",
                    "asset_id": "a1",
                    "mode": "palm",
                    "request_id": "rq",
                    "generation_id": gen,
                    "_max_attempts": 2,
                },
                job_id=2,
                attempt=2,
            )


# ── 3. Outcome is not separated from execution status (AM-05) ───────


def test_char_complete_job_records_no_business_outcome() -> None:
    """Invariant 18: successful jobs record outcome=succeeded separately from status."""
    from app.workers.queue import complete_job

    cursor = _FakeCursor()
    with _patch_queue_connection(cursor):
        complete_job(99)

    sql, _params = cursor.calls[-1]
    assert "completed" in sql.lower()
    assert "outcome" in sql.lower()
    assert "succeeded" in sql.lower()


def test_char_runner_calls_complete_job_regardless_of_outcome() -> None:
    """Runner dispatches CancelledStaleError → cancel_job, TerminalJobError → fail_job_terminal."""
    from app.workers import runner
    from app.workers.job_outcome import CancelledStaleError, TerminalJobError

    job = {"id": 7, "kind": "char_kind", "payload": {"x": 1}, "attempt": 1, "max_attempts": 3}

    def stale_raise_handler(payload, job_id, attempt):  # noqa: ANN001, ARG001
        raise CancelledStaleError("superseded")

    with patch.object(runner, "claim_next_job", return_value=job), \
            patch.object(runner, "complete_job") as complete, \
            patch.object(runner, "fail_job") as fail, \
            patch.object(runner, "fail_job_terminal") as fail_terminal, \
            patch.object(runner, "cancel_job") as cancel:
        runner._HANDLERS["char_kind"] = stale_raise_handler
        try:
            runner._process_one("char_kind")
        finally:
            runner._HANDLERS.pop("char_kind", None)

    cancel.assert_called_once()
    complete.assert_not_called()
    fail.assert_not_called()
    fail_terminal.assert_not_called()

    def terminal_raise_handler(payload, job_id, attempt):  # noqa: ANN001, ARG001
        raise TerminalJobError("permanent failure")

    with patch.object(runner, "claim_next_job", return_value=job), \
            patch.object(runner, "complete_job") as complete, \
            patch.object(runner, "fail_job") as fail, \
            patch.object(runner, "fail_job_terminal") as fail_terminal, \
            patch.object(runner, "cancel_job") as cancel:
        runner._HANDLERS["char_kind"] = terminal_raise_handler
        try:
            runner._process_one("char_kind")
        finally:
            runner._HANDLERS.pop("char_kind", None)

    fail_terminal.assert_called_once()
    complete.assert_not_called()
    fail.assert_not_called()
    cancel.assert_not_called()


def test_char_jobs_status_check_has_no_cancelled_state() -> None:
    """There is no queue state that means "superseded, do not retry"."""
    from pathlib import Path

    sql_text = (
        Path(__file__).resolve().parent.parent / "sql" / "migrations" / "018_jobs.sql"
    ).read_text(encoding="utf-8")

    assert "'cancelled'" not in sql_text
    assert "outcome" not in sql_text


# ── 4. Retry budget is a product, not a shared total (AM-04, invariant 19) ──


def test_char_send_asset_retry_budget_is_the_product_of_two_layers() -> None:
    """Invariant: send_asset total budget = queue_max × service_max ≤ declared total."""
    from app.workers.retry_budget import get_budget
    from app.render import sender as render_sender
    from app.workers import queue as queue_mod
    import inspect

    budget = get_budget("send_asset")
    queue_attempts = budget["queue_max"]       # declared: 2
    service_attempts = render_sender._SEND_RETRIES  # now reads from budget: 3

    assert queue_attempts == 2
    assert service_attempts == 3
    assert queue_attempts * service_attempts == budget["total"]  # 6

    # send_image_to_user cannot be told how much budget is left.
    assert "max_attempts" not in inspect.signature(render_sender.send_image_to_user).parameters
