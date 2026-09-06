"""PostgreSQL integration test fixtures (PR-00h)."""

from __future__ import annotations

import os

import pytest

from postgres_harness import (
    assert_test_database_url_safe,
    get_test_database_url,
    truncate_application_tables,
)

_SKIP_MESSAGE = (
    "TEST_DATABASE_URL is not set; skipping PostgreSQL integration test. "
    "See backend/docs/test_infra.md for setup instructions."
)


@pytest.fixture(scope="session")
def postgres_test_database_url() -> str:
    """
    Session-scoped URL for PostgreSQL integration tests.

    Skips when TEST_DATABASE_URL is unset. Raises (never skips) when the URL
    fails the production-safety guard — before any connection is opened.
    """
    test_url = get_test_database_url()
    if test_url is None:
        pytest.skip(_SKIP_MESSAGE)
    assert_test_database_url_safe(test_url)
    return test_url


@pytest.fixture(scope="session")
def postgres_test_db(postgres_test_database_url: str):
    """
    Session-scoped harness: point DATABASE_URL at the test DB, apply migrations
    once, yield a psycopg connection factory, drop row data on session end.
    """
    from app.db import close_pool, get_connection
    from app.db_init import run_schema_bootstrap

    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = postgres_test_database_url
    close_pool()

    try:
        run_schema_bootstrap()
        yield get_connection
    finally:
        try:
            with get_connection() as conn:
                truncate_application_tables(conn)
        except Exception:
            pass
        close_pool()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


@pytest.fixture
def postgres_clean_db(postgres_test_db):
    """
    Per-test isolation: truncate application tables before and after each test,
    including when the test fails or raises.
    """
    get_connection = postgres_test_db
    with get_connection() as conn:
        truncate_application_tables(conn)
    yield get_connection
    with get_connection() as conn:
        truncate_application_tables(conn)
