"""PostgreSQL storage breakdown for admin maintenance."""

from __future__ import annotations

import logging
from typing import Any

from psycopg.rows import dict_row

from app.db import get_connection

logger = logging.getLogger(__name__)


def get_db_storage_stats(*, table_limit: int = 15) -> dict[str, Any]:
    """Return database size, per-table sizes, and key row/asset metrics."""
    limit = max(5, min(int(table_limit), 50))
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size,"
                " pg_database_size(current_database()) AS db_size_bytes"
            )
            db_row = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT
                    relname AS table_name,
                    pg_total_relation_size(relid) AS total_bytes,
                    pg_size_pretty(pg_total_relation_size(relid)) AS total_pretty,
                    pg_relation_size(relid) AS table_bytes,
                    pg_size_pretty(pg_relation_size(relid)) AS table_pretty
                FROM pg_catalog.pg_statio_user_tables
                ORDER BY pg_total_relation_size(relid) DESC
                LIMIT %s
                """,
                (limit,),
            )
            tables = [dict(r) for r in cur.fetchall()]

            metrics: dict[str, Any] = {}
            metric_queries: tuple[tuple[str, str], ...] = (
                ("messenger_sessions", "SELECT COUNT(*) AS n FROM messenger_sessions"),
                ("readings", "SELECT COUNT(*) AS n FROM readings"),
                ("assets", "SELECT COUNT(*) AS n FROM assets"),
                (
                    "assets_with_bytes",
                    "SELECT COUNT(*) AS n FROM assets WHERE data_bytes IS NOT NULL",
                ),
                (
                    "assets_bytes_total",
                    "SELECT COALESCE(SUM(octet_length(data_bytes)), 0) AS n FROM assets",
                ),
                ("jobs", "SELECT COUNT(*) AS n FROM jobs"),
                (
                    "jobs_by_status",
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY n DESC",
                ),
                ("bot_activity_log", "SELECT COUNT(*) AS n FROM bot_activity_log"),
                ("funnel_events", "SELECT COUNT(*) AS n FROM funnel_events"),
            )
            for key, sql in metric_queries:
                try:
                    cur.execute(sql)
                    if key == "jobs_by_status":
                        metrics[key] = [dict(r) for r in cur.fetchall()]
                    else:
                        row = cur.fetchone()
                        metrics[key] = int(row["n"]) if row else 0
                except Exception:
                    logger.exception("db_storage_metric_failed key=%s", key)
                    metrics[key] = None

    assets_bytes = metrics.get("assets_bytes_total")
    if isinstance(assets_bytes, int):
        metrics["assets_bytes_pretty"] = _pretty_bytes(assets_bytes)

    db_bytes = int(db_row.get("db_size_bytes") or 0)
    return {
        "db_size": db_row.get("db_size"),
        "db_size_bytes": db_bytes,
        "db_size_mb": round(db_bytes / (1024 * 1024), 2) if db_bytes else 0,
        "tables": tables,
        "metrics": metrics,
    }


def _pretty_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"
