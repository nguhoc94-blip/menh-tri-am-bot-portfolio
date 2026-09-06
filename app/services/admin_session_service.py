from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row

from app.db import get_connection

logger = logging.getLogger(__name__)

SESSION_TTL_DAYS = 7


def create_session(*, admin_user_id: int) -> str:
    sid = str(uuid.uuid4())
    exp = datetime.now(tz=timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_sessions (id, admin_user_id, expires_at)
                VALUES (%s::uuid, %s, %s)
                """,
                (sid, admin_user_id, exp),
            )
    return sid


def delete_session(session_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_sessions WHERE id = %s::uuid", (session_id,))


def get_session_user(session_id: str | None) -> dict[str, Any] | None:
    if not session_id or not session_id.strip():
        return None
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT u.id AS user_id, u.email, u.role, u.is_active
                    FROM admin_sessions s
                    JOIN admin_users u ON u.id = s.admin_user_id
                    WHERE s.id = %s::uuid AND s.expires_at > NOW() AND u.is_active = TRUE
                    """,
                    (session_id.strip(),),
                )
                row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    except Exception:
        logger.debug("admin_session_lookup_failed", exc_info=True)
        return None


def purge_expired_sessions() -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admin_sessions WHERE expires_at <= NOW()")
    except Exception:
        logger.debug("admin_session_purge_failed", exc_info=True)


def sessions_summary_json() -> str:
    """For export (admin); bounded JSON from messenger_sessions."""
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT sender_id, state, updated_at, data_json
                    FROM messenger_sessions
                    ORDER BY updated_at DESC
                    LIMIT 500
                    """
                )
                rows = cur.fetchall()
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)
    except Exception:
        logger.exception("sessions_export_failed")
        return "[]"
