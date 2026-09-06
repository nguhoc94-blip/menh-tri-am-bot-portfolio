"""Wipe all customer/session data — preserves admin, app_config, schema_migrations."""

from __future__ import annotations

import logging
from typing import Any

from psycopg import errors

from app.db import get_connection

logger = logging.getLogger(__name__)

# Order: queue/assets first; sessions/profiles last among user tables.
CUSTOMER_TABLES: tuple[str, ...] = (
    "jobs",
    "assets",
    "readings",
    "user_memories",
    "messenger_sessions",
    "user_profiles",
    "profile_entities",
    "user_daily_counters",
    "message_burst_log",
    "webhook_dedupe",
    "funnel_events",
    "tracking_events",
    "bot_activity_log",
    "conversation_history",
    "donate_reports",
    "abuse_flags",
    "cost_ledger",
    "support_tickets",
    "orders",
)

VERIFY_TABLES: tuple[str, ...] = (
    "messenger_sessions",
    "readings",
    "user_profiles",
    "user_daily_counters",
    "funnel_events",
)


def reset_all_customer_data() -> dict[str, Any]:
    """
    TRUNCATE all customer-facing tables. Idempotent.
    Does NOT touch admin_users, app_config, campaigns, schema_migrations.
    """
    summary: dict[str, Any] = {"truncated": [], "skipped": [], "counts": {}}

    with get_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for table in CUSTOMER_TABLES:
                    try:
                        cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                        summary["truncated"].append(table)
                    except errors.UndefinedTable:
                        summary["skipped"].append(table)
                    except Exception:
                        logger.exception("customer_reset_failed table=%s", table)
                        raise

                for table in VERIFY_TABLES:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        row = cur.fetchone()
                        summary["counts"][table] = int(row[0]) if row else -1
                    except errors.UndefinedTable:
                        summary["counts"][table] = -1

    logger.info(
        "customer_data_reset_done truncated=%s counts=%s",
        len(summary["truncated"]),
        summary["counts"],
    )
    return summary
