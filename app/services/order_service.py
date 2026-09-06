"""Payment order authority — Nhịp 1 (manual verify). Source of truth: orders.status."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from psycopg.rows import dict_row

from app.db import get_connection

logger = logging.getLogger(__name__)

OrderStatus = Literal[
    "draft",
    "verification_pending",
    "paid_verified",
    "verify_failed",
    "manual_review",
]

VerificationApplyResult = Literal["verified", "verify_failed", "manual_review"]


def get_order_by_id(order_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, sender_id, status, metadata_json, created_at, updated_at
                FROM orders
                WHERE id = %s
                """,
                (order_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def get_order_for_session(sender_id: str, session_order_id: int | None) -> dict[str, Any] | None:
    """Prefer session-bound order; else latest order for sender (payment pipeline)."""
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if session_order_id is not None:
                cur.execute(
                    """
                    SELECT id, sender_id, status, metadata_json, created_at, updated_at
                    FROM orders
                    WHERE id = %s AND sender_id = %s
                    """,
                    (session_order_id, sender_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None
            cur.execute(
                """
                SELECT id, sender_id, status, metadata_json, created_at, updated_at
                FROM orders
                WHERE sender_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (sender_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def create_draft_order(sender_id: str) -> int:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO orders (sender_id, status, metadata_json)
                VALUES (%s, 'draft', '{}'::jsonb)
                RETURNING id
                """,
                (sender_id,),
            )
            row = cur.fetchone()
    if not row:
        raise RuntimeError("create_draft_order failed")
    return int(row["id"])


def mark_verification_pending(order_id: int, sender_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders
                SET status = 'verification_pending',
                    updated_at = NOW()
                WHERE id = %s AND sender_id = %s
                  AND status IN ('draft', 'verify_failed')
                """,
                (order_id, sender_id),
            )


def apply_verification_result(
    order_id: int,
    result: VerificationApplyResult,
    *,
    verification_actor: str = "manual",
) -> dict[str, Any] | None:
    """Manual verify seam. Returns updated row or None if order missing."""
    row = get_order_by_id(order_id)
    if not row:
        return None
    st0 = str(row.get("status") or "")
    if st0 == "paid_verified" and result in ("verify_failed", "manual_review"):
        return row
    sender_id = str(row["sender_id"])
    meta = row.get("metadata_json")
    if isinstance(meta, str):
        meta_obj: dict[str, Any] = json.loads(meta) if meta else {}
    elif isinstance(meta, dict):
        meta_obj = dict(meta)
    else:
        meta_obj = {}
    meta_obj["verification_actor"] = verification_actor

    new_status: OrderStatus
    if result == "verified":
        new_status = "paid_verified"
    elif result == "verify_failed":
        new_status = "verify_failed"
    else:
        new_status = "manual_review"

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE orders
                SET status = %s,
                    metadata_json = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, sender_id, status, metadata_json, created_at, updated_at
                """,
                (new_status, json.dumps(meta_obj), order_id),
            )
            updated = cur.fetchone()

    out = dict(updated) if updated else None
    if result == "verified" and out:
        from app.services.user_profile_flags import mirror_paid_once_after_verification

        mirror_paid_once_after_verification(sender_id)
    logger.info(
        "order_verification_applied order_id=%s result=%s status=%s",
        order_id,
        result,
        new_status,
    )
    return out


def reconcile_session_state_from_order(
    *,
    order_status: str | None,
    current_state_value: str,
) -> str | None:
    """Derive suggested ConversationState value from order authority. None = no change."""
    from app.services.messenger_state import ConversationState

    if not order_status:
        return None
    if order_status == "verify_failed":
        return ConversationState.CHECKOUT.value
    if order_status == "manual_review":
        return ConversationState.SUPPORT_HANDOFF.value
    if order_status == "paid_verified":
        if current_state_value in (
            ConversationState.CHECKOUT.value,
            ConversationState.PAYMENT_VERIFYING.value,
        ):
            return ConversationState.PAID_GENERATING.value
    if order_status == "verification_pending":
        if current_state_value == ConversationState.CHECKOUT.value:
            return ConversationState.PAYMENT_VERIFYING.value
    return None


def can_enter_paid_generating(order_status: str | None) -> bool:
    return order_status == "paid_verified"
