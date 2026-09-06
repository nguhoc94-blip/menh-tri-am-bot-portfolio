from __future__ import annotations

import logging
from typing import Any

from psycopg import errors

from app.db import get_connection

logger = logging.getLogger(__name__)


def try_claim_webhook_delivery(
    *,
    dedupe_key: str,
    sender_id: str,
    payload_excerpt: str | None = None,
) -> bool:
    """
    Returns True if this delivery should be processed (first insert).
    Returns False if duplicate.
    """
    if not dedupe_key:
        return True
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO webhook_dedupe (dedupe_key, sender_id, payload_excerpt)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    """,
                    (dedupe_key, sender_id, payload_excerpt),
                )
                return cur.rowcount is not None and cur.rowcount > 0
    except errors.UndefinedTable:
        logger.warning("webhook_dedupe_table_missing event=webhook_dedupe_table_missing")
        return True
    except Exception:
        logger.exception("webhook_dedupe_failed event=webhook_dedupe_failed")
        return True


def build_dedupe_key_from_messaging_event(event: dict[str, Any]) -> str | None:
    msg = event.get("message") or {}
    mid = msg.get("mid")
    if isinstance(mid, str) and mid.strip():
        return mid.strip()
    postback = event.get("postback") or {}
    payload = postback.get("payload")
    ts = event.get("timestamp")
    sender_id = str((event.get("sender") or {}).get("id") or "")
    if isinstance(payload, str) and payload and sender_id and ts is not None:
        return f"pb:{sender_id}:{ts}:{payload[:120]}"
    if sender_id and ts is not None:
        return f"evt:{sender_id}:{ts}"
    return None
