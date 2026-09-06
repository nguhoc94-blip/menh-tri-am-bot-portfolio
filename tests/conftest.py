"""Shared pytest fixtures — autouse env setup for tests requiring Messenger env vars."""

from __future__ import annotations

import pytest

pytest_plugins = ["conftest_postgres"]


@pytest.fixture(autouse=True)
def messenger_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimum env vars needed by final_message_builder and related tests."""
    monkeypatch.setenv("MESSENGER_PART_2_BANK_BLOCK", "VPBank123-test")
    monkeypatch.setenv("MESSENGER_PART_3_CEO_NOTE", "CEO-note-test")
    monkeypatch.setenv("BATCH_QUIET_SECONDS", "0")
    # Legacy image-flow tests expect multimodal on; production default is OFF.
    monkeypatch.setenv("MULTIMODAL_ENABLED", "1")


@pytest.fixture(autouse=True)
def clear_webhook_dedupe_for_tests() -> None:
    """Clear webhook_dedupe records with test MIDs before each test.

    Prevents dedup false-positives when tests use fixed MID strings and
    the real PostgreSQL DB persists records between test runs.
    """
    try:
        from app.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM webhook_dedupe WHERE dedupe_key LIKE 'mid_test_%' OR dedupe_key LIKE 'mid_sched_%' OR dedupe_key LIKE 'mid_dup_%'"
                )
    except Exception:
        pass
