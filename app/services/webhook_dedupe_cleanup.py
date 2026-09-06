"""Retention cleanup for webhook_dedupe — Nhịp 3, locked to 24 hours."""

from __future__ import annotations

import logging
from typing import Final

from psycopg import errors

from app.db import get_connection

logger = logging.getLogger(__name__)

RETENTION_HOURS: Final[int] = 24
_DEFAULT_BATCH: Final[int] = 500


def run_webhook_dedupe_retention_cleanup(*, batch_size: int = _DEFAULT_BATCH) -> int:
    """
    Delete rows older than 24 hours in batches. Returns total rows deleted.
    Semantics: rows with created_at < NOW() - interval '24 hours'.
    """
    if batch_size < 1:
        batch_size = _DEFAULT_BATCH
    total = 0
    try:
        while True:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        WITH cte AS (
                            SELECT dedupe_key FROM webhook_dedupe
                            WHERE created_at < NOW() - INTERVAL '24 hours'
                            LIMIT %s
                        )
                        DELETE FROM webhook_dedupe AS w
                        USING cte
                        WHERE w.dedupe_key = cte.dedupe_key
                        """,
                        (batch_size,),
                    )
                    n = cur.rowcount or 0
            total += n
            if n < batch_size:
                break
    except errors.UndefinedTable:
        logger.warning("webhook_dedupe_cleanup_skipped event=table_missing")
        return 0
    if total:
        logger.info(
            "webhook_dedupe_cleanup_done event=dedupe_retention rows=%s retention_h=%s",
            total,
            RETENTION_HOURS,
        )
    return total
