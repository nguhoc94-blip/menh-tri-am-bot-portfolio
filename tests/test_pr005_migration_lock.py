"""PR-005 AM-06: PostgreSQL advisory lock for migration runner."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.db_init import _MIGRATION_LOCK_KEY


def test_advisory_lock_key_is_stable():
    """Invariant: lock key không bao giờ thay đổi — migration pairs must not diverge."""
    assert _MIGRATION_LOCK_KEY == 5_800_001


def test_run_migrations_calls_advisory_lock_and_unlock(monkeypatch):
    """Invariant: pg_advisory_lock và pg_advisory_unlock đều được gọi kể cả khi có exception."""
    lock_calls: list[tuple] = []
    unlock_calls: list[tuple] = []
    commit_calls: list[str] = []

    class MockCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            if "pg_advisory_lock" in sql:
                lock_calls.append(params)
            elif "pg_advisory_unlock" in sql:
                unlock_calls.append(params)

    class MockConn:
        def cursor(self):
            return MockCursor()

        def execute(self, sql):
            pass

        def commit(self):
            commit_calls.append("commit")

        def rollback(self):
            pass

    @contextmanager
    def mock_get_connection():
        yield MockConn()

    monkeypatch.setattr("app.db_init.get_connection", mock_get_connection)
    monkeypatch.setattr("app.db_init._run_migrations_locked", lambda lock_conn: [])

    from app.db_init import run_migrations

    run_migrations()

    assert lock_calls == [(_MIGRATION_LOCK_KEY,)]
    assert unlock_calls == [(_MIGRATION_LOCK_KEY,)]
    assert commit_calls == ["commit", "commit"]


def test_advisory_unlock_called_on_migration_exception(monkeypatch):
    """Invariant: exception trong migration không để lock bị giữ vĩnh viễn (finally block)."""
    unlock_calls: list[tuple] = []
    commit_calls: list[str] = []

    class MockCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            if "pg_advisory_unlock" in sql:
                unlock_calls.append(params)

    class MockConn:
        def cursor(self):
            return MockCursor()

        def execute(self, sql):
            pass

        def commit(self):
            commit_calls.append("commit")

        def rollback(self):
            pass

    @contextmanager
    def mock_get_connection():
        yield MockConn()

    def boom(lock_conn):
        raise ValueError("simulated migration failure")

    monkeypatch.setattr("app.db_init.get_connection", mock_get_connection)
    monkeypatch.setattr("app.db_init._run_migrations_locked", boom)

    from app.db_init import run_migrations

    with pytest.raises(ValueError, match="simulated migration failure"):
        run_migrations()

    assert unlock_calls == [(_MIGRATION_LOCK_KEY,)]
    assert commit_calls == ["commit", "commit"]


def _concurrent_migration_worker(result_queue, barrier, db_url):
    """Subprocess worker for concurrent migration startup test (must be module-level for Windows spawn)."""
    import os

    os.environ["DATABASE_URL"] = db_url
    from app.db import close_pool

    close_pool()
    from app.db_init import run_migrations

    barrier.wait()
    result = run_migrations()
    result_queue.put(result)


def _advisory_lock_count(cur, lock_key: int) -> int:
    cur.execute(
        """
        SELECT COUNT(*) FROM pg_locks
        WHERE locktype = 'advisory'
          AND classid = 0
          AND objid = %s
          AND granted = true
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
        """,
        (lock_key,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


@pytest.mark.requires_postgres
def test_concurrent_migrations_two_independent_startups(postgres_clean_db):
    """
    Invariant: hai process độc lập chạy run_migrations() đồng thời
    không tạo duplicate schema_migrations rows.
    Một runner apply versions, runner còn lại trả [].
    """
    import multiprocessing
    import os

    get_connection = postgres_clean_db

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM schema_migrations")
        conn.commit()

    db_url = os.environ["DATABASE_URL"]

    manager = multiprocessing.Manager()
    result_queue = manager.Queue()
    barrier = manager.Barrier(2)

    worker_args = (result_queue, barrier, db_url)
    p1 = multiprocessing.Process(target=_concurrent_migration_worker, args=worker_args)
    p2 = multiprocessing.Process(target=_concurrent_migration_worker, args=worker_args)
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)

    assert p1.exitcode == 0, f"Process 1 crashed: {p1.exitcode}"
    assert p2.exitcode == 0, f"Process 2 crashed: {p2.exitcode}"

    results = sorted([result_queue.get(), result_queue.get()], key=len)
    assert results[0] == [], f"Expected one runner to return []; got {results[0]}"
    assert len(results[1]) > 0, f"Expected one runner to apply versions; got empty"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cur.fetchall()]
    assert len(versions) == len(set(versions)), f"Duplicate versions: {versions}"


@pytest.mark.requires_postgres
def test_second_run_is_idempotent(postgres_clean_db):
    """Invariant: chạy run_migrations() lần hai không apply lại bất kỳ version nào đã có."""
    from app.db_init import run_migrations

    get_connection = postgres_clean_db

    first = run_migrations()
    second = run_migrations()

    assert second == []

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions_after = [row[0] for row in cur.fetchall()]

    if first:
        for version in first:
            assert version in versions_after


@pytest.mark.requires_postgres
def test_lock_released_after_normal_completion(postgres_clean_db):
    """Invariant: sau khi run_migrations() hoàn thành, pg_locks không còn giữ migration lock."""
    from app.db_init import run_migrations

    get_connection = postgres_clean_db

    run_migrations()

    with get_connection() as conn:
        with conn.cursor() as cur:
            assert _advisory_lock_count(cur, _MIGRATION_LOCK_KEY) == 0


@pytest.mark.requires_postgres
def test_lock_released_after_exception(postgres_clean_db, monkeypatch):
    """Invariant: exception trong migration không làm lock bị giữ — connection close releases it."""
    from app.db_init import run_migrations

    get_connection = postgres_clean_db

    def boom(lock_conn):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr("app.db_init._run_migrations_locked", boom)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        run_migrations()

    with get_connection() as conn:
        with conn.cursor() as cur:
            assert _advisory_lock_count(cur, _MIGRATION_LOCK_KEY) == 0


@pytest.mark.requires_postgres
def test_apply_legacy_schema_uses_lock_conn(postgres_clean_db):
    """
    Invariant: apply_legacy_schema_if_needed() giữ advisory lock trên lock_conn
    trong suốt quá trình tạo legacy tables.
    """
    from app.db_init import apply_legacy_schema_if_needed

    get_connection = postgres_clean_db

    apply_legacy_schema_if_needed()

    with get_connection() as conn:
        with conn.cursor() as cur:
            assert _advisory_lock_count(cur, _MIGRATION_LOCK_KEY) == 0
