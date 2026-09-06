"""Tests for customer_data_reset service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_reset_all_customer_data_truncates_and_counts() -> None:
    from app.services.customer_data_reset import CUSTOMER_TABLES, reset_all_customer_data

    execute_calls: list[str] = []

    class FakeCursor:
        def execute(self, sql: str, params=None) -> None:
            execute_calls.append(sql.strip().split()[0:3])

        def fetchone(self):
            return (0,)

    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=FakeCursor())
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    fake_conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
    fake_conn.transaction.return_value.__exit__ = MagicMock(return_value=False)

    with patch("app.services.customer_data_reset.get_connection") as mock_gc:
        mock_gc.return_value.__enter__ = MagicMock(return_value=fake_conn)
        mock_gc.return_value.__exit__ = MagicMock(return_value=False)
        summary = reset_all_customer_data()

    assert len(summary["truncated"]) == len(CUSTOMER_TABLES)
    assert summary["counts"]["messenger_sessions"] == 0
    truncate_sql = [c for c in execute_calls if c and c[0] == "TRUNCATE"]
    assert len(truncate_sql) == len(CUSTOMER_TABLES)
