"""Structured bot activity log — Nhịp 2 (Gate Output timeline)."""

from __future__ import annotations

import logging
from typing import Any

from psycopg import errors

from app.db import get_connection

logger = logging.getLogger(__name__)

_MAX_EXCERPT = 2000


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    t = text.strip()
    if len(t) <= _MAX_EXCERPT:
        return t
    return t[: _MAX_EXCERPT - 3] + "..."


def log_activity(
    *,
    sender_id: str,
    request_id: str | None,
    event_kind: str,
    state_before: str | None = None,
    state_after: str | None = None,
    outbound_class: str | None = None,
    validator_verdict: str | None = None,
    blocked_reason: str | None = None,
    order_status: str | None = None,
    ai_status: str | None = None,
    structured_payload: str | None = None,
    message_excerpt: str | None = None,
    send_outcome: str | None = None,
    provider_message_id: str | None = None,
) -> None:
    if not sender_id:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_activity_log (
                        sender_id, request_id, event_kind,
                        state_before, state_after, outbound_class,
                        validator_verdict, blocked_reason,
                        order_status, ai_status, structured_payload, message_excerpt,
                        send_outcome, provider_message_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sender_id,
                        request_id,
                        event_kind,
                        state_before,
                        state_after,
                        outbound_class,
                        validator_verdict,
                        blocked_reason,
                        order_status,
                        ai_status,
                        structured_payload,
                        _excerpt(message_excerpt),
                        send_outcome,
                        provider_message_id,
                    ),
                )
    except errors.UndefinedTable:
        logger.warning("bot_activity_log_table_missing event_kind=%s", event_kind)
    except Exception:
        logger.exception("bot_activity_log_write_failed event_kind=%s", event_kind)


def log_inbound_text(
    *,
    sender_id: str,
    request_id: str | None,
    state_before: str | None,
    text: str,
) -> None:
    log_activity(
        sender_id=sender_id,
        request_id=request_id,
        event_kind="inbound_text",
        state_before=state_before,
        message_excerpt=text,
    )


def log_state_transition(
    *,
    sender_id: str,
    request_id: str | None,
    state_before: str | None,
    state_after: str | None,
    order_status: str | None = None,
) -> None:
    log_activity(
        sender_id=sender_id,
        request_id=request_id,
        event_kind="state_transition",
        state_before=state_before,
        state_after=state_after,
        order_status=order_status,
    )


def log_outbound_send(
    *,
    sender_id: str,
    request_id: str | None,
    conversation_state: str | None,
    outbound_class: str | None,
    validator_verdict: str,
    blocked_reason: str | None,
    text: str,
    order_status: str | None = None,
    ai_status: str | None = None,
    structured_payload: str | None = None,
    send_outcome: str | None = None,
    provider_message_id: str | None = None,
) -> None:
    log_activity(
        sender_id=sender_id,
        request_id=request_id,
        event_kind="outbound_send",
        state_before=conversation_state,
        outbound_class=outbound_class,
        validator_verdict=validator_verdict,
        blocked_reason=blocked_reason,
        order_status=order_status,
        ai_status=ai_status,
        structured_payload=structured_payload,
        message_excerpt=text,
        send_outcome=send_outcome,
        provider_message_id=provider_message_id,
    )
