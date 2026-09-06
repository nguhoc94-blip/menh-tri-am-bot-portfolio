"""PostgreSQL integration test harness — guard unit tests and smoke test (PR-00h)."""

from __future__ import annotations

import pytest

from postgres_harness import (
    UnsafeTestDatabaseError,
    assert_test_database_url_safe,
)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "postgresql://u:p@dpg-example-host.oregon-postgres.render.com/DEMO_test",
        "postgresql://u:p@localhost:5432/DEMO",
        "postgresql://u:p@localhost:5432/DEMO_prod",
        "postgresql://u:p@localhost:5432/myapp",
    ],
)
def test_guard_rejects_unsafe_test_database_urls(unsafe_url: str) -> None:
    # Invariant: production-safety guard rejects blocklisted hosts, forbidden db
    # names, and databases whose names do not contain 'test' — before connecting.
    with pytest.raises(UnsafeTestDatabaseError):
        assert_test_database_url_safe(unsafe_url)


def test_guard_rejects_when_test_url_equals_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invariant: TEST_DATABASE_URL must not alias DATABASE_URL (same target as prod/dev).
    url = "postgresql://u:p@localhost:5432/DEMO_test"
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(UnsafeTestDatabaseError, match="must not equal DATABASE_URL"):
        assert_test_database_url_safe(url)


def test_guard_accepts_safe_test_database_url() -> None:
    # Invariant: a clearly named local test database passes the guard without connecting.
    assert_test_database_url_safe("postgresql://u:p@localhost:5432/DEMO_test")


@pytest.mark.requires_postgres
def test_postgres_migrations_apply_and_schema_exists(postgres_clean_db) -> None:
    # Invariant: harness applies project SQL migrations against real PostgreSQL and
    # the resulting schema includes schema_migrations plus core application tables.
    get_connection = postgres_clean_db
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cur.fetchall()]
            assert versions, "schema_migrations must list at least one applied version"
            assert "001" in versions

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'jobs'
                )
                """
            )
            assert cur.fetchone()[0] is True

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'messenger_sessions'
                )
                """
            )
            assert cur.fetchone()[0] is True
