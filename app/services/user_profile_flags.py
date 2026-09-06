from __future__ import annotations

import json
import logging
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row

from app.db import get_connection

logger = logging.getLogger(__name__)


def get_profile_metadata(sender_id: str) -> dict[str, Any]:
    meta, _available = get_profile_metadata_with_availability(sender_id)
    return meta


def get_profile_metadata_with_availability(sender_id: str) -> tuple[dict[str, Any], bool]:
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT metadata_json FROM user_profiles WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
        if not row:
            return {}, True
        raw = row["metadata_json"]
        if isinstance(raw, dict):
            return dict(raw), True
        if isinstance(raw, str):
            return json.loads(raw), True
        return {}, True
    except errors.UndefinedTable:
        return {}, True
    except Exception:
        logger.debug("profile_metadata_read_failed sender_id=%s", sender_id, exc_info=True)
        return {}, False


def paid_once_from_metadata(metadata: dict[str, Any]) -> bool:
    flags = metadata.get("flags")
    if not isinstance(flags, dict):
        return False
    return bool(flags.get("paid_once"))


def entitlement_paid_unlocked(sender_id: str, session_order_id: int | None) -> bool:
    """Paid entitlement: orders.status == paid_verified (authority). Not metadata alone."""
    from app.services.order_service import get_order_for_session

    row = get_order_for_session(sender_id, session_order_id)
    return bool(row and str(row.get("status")) == "paid_verified")


def mirror_paid_once_after_verification(sender_id: str) -> None:
    """Non-authoritative mirror for legacy flow keys; call only after paid_verified."""
    patch_profile_metadata(sender_id, {"flags": {"paid_once": True}})


def patch_profile_metadata(sender_id: str, patch: dict[str, Any]) -> None:
    if not patch:
        return
    try:
        _write_profile_metadata_patch(sender_id, patch)
    except errors.UndefinedTable:
        pass
    except Exception:
        logger.warning("profile_metadata_patch_failed sender_id=%s", sender_id, exc_info=True)


def patch_profile_metadata_strict(sender_id: str, patch: dict[str, Any]) -> None:
    """Like patch_profile_metadata but propagates write failures."""
    if not patch:
        return
    _write_profile_metadata_patch(sender_id, patch)


def _write_profile_metadata_patch(sender_id: str, patch: dict[str, Any]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_profiles (sender_id, metadata_json, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (sender_id) DO UPDATE SET
                    metadata_json = COALESCE(user_profiles.metadata_json, '{}'::jsonb) || EXCLUDED.metadata_json,
                    updated_at = NOW()
                """,
                (sender_id, json.dumps(patch)),
            )
