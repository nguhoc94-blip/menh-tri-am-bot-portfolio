"""Data subject operations — anonymize or fully delete a Messenger sender_id."""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg import errors

from app.db import get_connection
from app.utils.sender_hash import hash_sender_id

logger = logging.getLogger(__name__)

_ANON_SESSION_PAYLOAD = {
    "anonymized": True,
    "birth": {},
    "history": [],
    "routing": {},
    "chart": None,
    "reading_id": None,
}

# Child/audit tables first; core identity rows last.
_SENDER_DELETE_TABLES: tuple[str, ...] = (
    "cost_ledger",
    "premium_interpretations",
    "assets",
    "readings",
    "user_memories",
    "conversation_history",
    "support_tickets",
    "donate_reports",
    "abuse_flags",
    "bot_activity_log",
    "funnel_events",
    "webhook_dedupe",
    "message_burst_log",
    "user_daily_counters",
    "orders",
    "profile_entities",
    "messenger_sessions",
    "user_profiles",
)


def _delete_count(cur, sql: str, params: tuple[Any, ...]) -> int:
    cur.execute(sql, params)
    return cur.rowcount or 0


def delete_sender_all_data(sender_id: str) -> dict[str, Any]:
    """Hard-delete all persisted customer data for one sender_id.

    Unlike ``anonymize_sender_baseline``, rows are removed — the PSID becomes
    indistinguishable from a never-seen customer on the next message (except
    Messenger platform history outside this DB).
    """
    sid = (sender_id or "").strip()
    if not sid:
        raise ValueError("sender_id is required")

    sender_hash = hash_sender_id(sid)
    summary: dict[str, Any] = {"sender_id": sid, "deleted": {}}

    with get_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                summary["deleted"]["jobs"] = _delete_count(
                    cur,
                    "DELETE FROM jobs WHERE payload->>'sender_id' = %s",
                    (sid,),
                )
                for table in _SENDER_DELETE_TABLES:
                    try:
                        summary["deleted"][table] = _delete_count(
                            cur,
                            f"DELETE FROM {table} WHERE sender_id = %s",
                            (sid,),
                        )
                    except errors.UndefinedTable:
                        summary["deleted"][table] = 0

                try:
                    summary["deleted"]["tracking_events"] = _delete_count(
                        cur,
                        """
                        DELETE FROM tracking_events
                        WHERE user_id_hash = %s OR session_id = %s
                        """,
                        (sender_hash, sid),
                    )
                except errors.UndefinedTable:
                    summary["deleted"]["tracking_events"] = 0

    logger.info(
        "sender_data_deleted sender_id=%s deleted=%s",
        sid,
        summary["deleted"],
    )
    return summary


def anonymize_sender_baseline(sender_id: str) -> dict[str, Any]:
    """
    Scrub PII-heavy fields for sender_id. Keeps row keys where required (readings id)
    but replaces text/json content with redacted placeholders.
    """
    sid = (sender_id or "").strip()
    if not sid:
        raise ValueError("sender_id is required")

    summary: dict[str, Any] = {
        "sender_id": sid,
        "messenger_sessions_updated": 0,
        "readings_updated": 0,
        "conversation_history_deleted": 0,
        "funnel_events_deleted": 0,
        "webhook_dedupe_deleted": 0,
        "orders_updated": 0,
        "user_profiles_deleted": 0,
    }

    session_json = json.dumps(_ANON_SESSION_PAYLOAD)

    with get_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        UPDATE messenger_sessions
                        SET state = 'CHATTING', data_json = %s::jsonb, updated_at = NOW()
                        WHERE sender_id = %s
                        """,
                        (session_json, sid),
                    )
                    summary["messenger_sessions_updated"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

                try:
                    cur.execute(
                        """
                        UPDATE readings
                        SET
                            normalized_input_json = '{"anonymized":true}'::jsonb,
                            chart_json = '{"anonymized":true}'::jsonb,
                            free_teaser = '[REDACTED]',
                            full_reading = '[REDACTED]',
                            updated_at = NOW()
                        WHERE sender_id = %s
                        """,
                        (sid,),
                    )
                    summary["readings_updated"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

                try:
                    cur.execute(
                        "DELETE FROM conversation_history WHERE sender_id = %s",
                        (sid,),
                    )
                    summary["conversation_history_deleted"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

                try:
                    cur.execute(
                        "DELETE FROM funnel_events WHERE sender_id = %s",
                        (sid,),
                    )
                    summary["funnel_events_deleted"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

                try:
                    cur.execute(
                        "DELETE FROM webhook_dedupe WHERE sender_id = %s",
                        (sid,),
                    )
                    summary["webhook_dedupe_deleted"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

                try:
                    cur.execute(
                        """
                        UPDATE orders
                        SET metadata_json = '{"anonymized":true}'::jsonb, updated_at = NOW()
                        WHERE sender_id = %s
                        """,
                        (sid,),
                    )
                    summary["orders_updated"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

                try:
                    cur.execute(
                        "DELETE FROM user_profiles WHERE sender_id = %s",
                        (sid,),
                    )
                    summary["user_profiles_deleted"] = cur.rowcount or 0
                except errors.UndefinedTable:
                    pass

    logger.info(
        "data_subject_anonymized sender_id=%s sessions=%s readings=%s",
        sid,
        summary["messenger_sessions_updated"],
        summary["readings_updated"],
    )
    return summary
