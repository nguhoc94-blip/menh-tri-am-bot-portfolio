"""Test-only helpers for the PostgreSQL integration test harness (PR-00h)."""

from __future__ import annotations

import os
from urllib.parse import urlparse

_DOCS_HINT = "See backend/docs/test_infra.md for setup instructions."

# Production blocklist: known Render host suffix and database names for DEMO-db-v2.
_FORBIDDEN_HOST_SUBSTRINGS = ("render.com",)
_FORBIDDEN_DB_NAMES = frozenset({"DEMO", "DEMO_prod"})


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when TEST_DATABASE_URL fails the production-safety guard."""


def get_test_database_url() -> str | None:
    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    return url or None


def assert_test_database_url_safe(test_url: str) -> None:
    """
    Reject URLs that could target production. Runs before any DB connection.

    Both a blocklist and an opt-in rule are enforced:
    - Blocklist catches known production host/db names (Render, DEMO, DEMO_prod).
    - Opt-in requires the database name to contain 'test', so an unlisted
      production hostname cannot be used accidentally — production DBs are never
      named *test*.
    """
    prod_url = (os.environ.get("DATABASE_URL") or "").strip()
    if prod_url and test_url == prod_url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL must not equal DATABASE_URL (would run tests against "
            f"the same database as production/dev). {_DOCS_HINT}"
        )

    parsed = urlparse(test_url)
    host = (parsed.hostname or "").lower()
    db_name = (parsed.path or "").lstrip("/").split("?")[0].lower()

    if not db_name:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL has no database name in the path. {_DOCS_HINT}"
        )

    for forbidden in _FORBIDDEN_HOST_SUBSTRINGS:
        if forbidden in host:
            raise UnsafeTestDatabaseError(
                f"TEST_DATABASE_URL host {host!r} matches forbidden production "
                f"pattern {forbidden!r}. {_DOCS_HINT}"
            )

    if db_name in _FORBIDDEN_DB_NAMES:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL database name {db_name!r} is forbidden (production). "
            f"{_DOCS_HINT}"
        )

    if "test" not in db_name:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL database name {db_name!r} must contain 'test' to "
            f"opt in to being a test database. {_DOCS_HINT}"
        )


def truncate_application_tables(conn) -> None:
    """Remove all row data from public application tables; keep schema_migrations."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename <> 'schema_migrations'
            ORDER BY tablename
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        if not tables:
            return
        quoted = ", ".join(f'"{name}"' for name in tables)
        cur.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
