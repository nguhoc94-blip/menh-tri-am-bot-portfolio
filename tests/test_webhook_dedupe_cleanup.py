"""webhook_dedupe retention cleanup."""

from __future__ import annotations

from unittest.mock import patch

from psycopg import errors

from app.services import webhook_dedupe_cleanup as mod


def test_retention_hours_locked_24() -> None:
    assert mod.RETENTION_HOURS == 24


def test_cleanup_returns_zero_when_table_missing() -> None:
    def raise_undefined() -> None:
        raise errors.UndefinedTable("webhook_dedupe")

    with patch("app.services.webhook_dedupe_cleanup.get_connection", side_effect=raise_undefined):
        assert mod.run_webhook_dedupe_retention_cleanup() == 0
