"""PR-004 (AM-04, AM-05) — backoff and outcome semantics tests."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        self.calls.append((sql, params))

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


def test_fail_job_sets_run_at_for_retryable_failure():
    # Invariant: a retryable failure defers re-claiming by setting run_at in the future
    from app.workers.queue import fail_job

    cursor = _FakeCursor()
    with _patch_queue_connection(cursor):
        fail_job(42, "transient", attempt=1, max_attempts=3)

    sql, params = cursor.calls[-1]
    assert "run_at" in sql
    assert "interval '1 second'" in sql
    assert params[2] > 0  # backoff seconds


def test_fail_job_does_not_set_run_at_for_dead_job():
    # Invariant: a terminal queue failure (attempt >= max_attempts) does not set run_at
    # because the job will never be claimed again
    from app.workers.queue import fail_job

    cursor = _FakeCursor()
    with _patch_queue_connection(cursor):
        fail_job(42, "exhausted", attempt=3, max_attempts=3)

    sql, params = cursor.calls[-1]
    assert "run_at" not in sql
    assert params[0] == "dead"


def test_backoff_seconds_increases_with_attempt():
    # Invariant: successive failures produce longer backoff delays (exponential)
    from app.workers.queue import _backoff_seconds

    d1 = _backoff_seconds(1, job_id=10)
    d2 = _backoff_seconds(2, job_id=10)
    d3 = _backoff_seconds(3, job_id=10)
    assert d2 > d1
    assert d3 > d2


def test_backoff_seconds_is_deterministic_for_same_inputs():
    # Invariant: backoff is deterministic (no randomness), same inputs → same delay
    from app.workers.queue import _backoff_seconds

    assert _backoff_seconds(2, job_id=99) == _backoff_seconds(2, job_id=99)
    assert _backoff_seconds(1, job_id=7) == _backoff_seconds(1, job_id=7)


def test_backoff_seconds_caps_at_configured_maximum():
    # Invariant: backoff never exceeds WORKER_BACKOFF_CAP_SEC
    from app.workers.queue import _backoff_seconds

    with patch.dict(os.environ, {"WORKER_BACKOFF_BASE_SEC": "60", "WORKER_BACKOFF_CAP_SEC": "120"}):
        delay = _backoff_seconds(10, job_id=999)
    assert delay <= 120


def test_fail_job_terminal_sets_dead_and_failed_terminal_outcome():
    # Invariant: fail_job_terminal marks the job dead with outcome=failed_terminal,
    # never retried
    from app.workers.queue import fail_job_terminal

    cursor = _FakeCursor()
    with _patch_queue_connection(cursor):
        fail_job_terminal(55, "permanent error")

    sql, params = cursor.calls[-1]
    assert "dead" in sql.lower()
    assert "failed_terminal" in sql
    assert params[0] == "permanent error"


def test_cancel_job_sets_cancelled_and_cancelled_stale_outcome():
    # Invariant: cancel_job marks the job cancelled with outcome=cancelled_stale,
    # never counted as a business success
    from app.workers.queue import cancel_job

    cursor = _FakeCursor()
    with _patch_queue_connection(cursor):
        cancel_job(77, reason="superseded")

    sql, params = cursor.calls[-1]
    assert "cancelled" in sql.lower()
    assert "cancelled_stale" in sql
    assert "superseded" in params[0]


def test_retry_budget_queue_max_times_service_max_does_not_exceed_total():
    # Invariant: for every declared job kind, queue_max × service_max ≤ total budget
    from app.workers.retry_budget import RETRY_BUDGETS

    for kind, budget in RETRY_BUDGETS.items():
        assert budget["queue_max"] * budget["service_max"] <= budget["total"], \
            f"{kind}: {budget['queue_max']} × {budget['service_max']} > {budget['total']}"
