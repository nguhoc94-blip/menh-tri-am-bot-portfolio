"""
Structured event emitter — fire-and-forget, never raises.
Slice 1 · V9.1 §9 / docs/ARCHITECTURE/04_event_schema.md

Events are hashed with USER_HASH_HMAC_SECRET before storage (U-11).
Log redaction: no raw content, no signed URLs, no PII.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.db import get_connection
from app.utils.trace_context import get_trace_id

logger = logging.getLogger(__name__)

_HMAC_SECRET_ENV = "USER_HASH_HMAC_SECRET"


def _hash_sender_id(sender_id: str) -> str:
    """
    HMAC-SHA256 hash of sender_id using USER_HASH_HMAC_SECRET.
    Falls back to plain SHA-256 if secret not set (local/dev only — logs warning).
    """
    secret = (os.environ.get(_HMAC_SECRET_ENV) or "").strip().encode()
    if not secret:
        logger.warning(
            "event_hash_degraded reason=%s_not_set "
            "hint=set USER_HASH_HMAC_SECRET in env",
            _HMAC_SECRET_ENV,
        )
        # Fallback: plain SHA-256 (NOT production-safe — U-11 must be set)
        return hashlib.sha256(sender_id.encode()).hexdigest()[:32]
    return hmac.new(secret, sender_id.encode(), hashlib.sha256).hexdigest()[:32]


def emit_event(
    *,
    event_name: str,
    sender_id: str,
    session_id: str,
    mode: str,
    step: str,
    status: str,
    source: str = "system",
    cost_estimate: float | None = None,
    model_call_count: int | None = None,
    asset_id: str | None = None,
    error_code: str | None = None,
    cohort_label: str = "new",
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Emit a tracking event to the database.
    Non-blocking (synchronous but quick INSERT).
    Never raises — a tracking failure must not interrupt the main flow.

    Metadata must NOT contain: raw image bytes, signed URLs, bank info,
    raw prompt text with PII (V9.2 §7.2 / P.1 rule 5).
    """
    user_id_hash = _hash_sender_id(sender_id)
    trace_id = get_trace_id()
    safe_metadata = {**(metadata or {}), "trace_id": trace_id}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tracking_events (
                        event_name, user_id_hash, session_id, mode, step,
                        status, event_timestamp, source, cost_estimate,
                        model_call_count, asset_id, error_code, cohort_label,
                        metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s
                    )
                    """,
                    (
                        event_name,
                        user_id_hash,
                        session_id,
                        mode,
                        step,
                        status,
                        datetime.now(timezone.utc),
                        source,
                        cost_estimate,
                        model_call_count,
                        asset_id,
                        error_code,
                        cohort_label,
                        json.dumps(safe_metadata),
                    ),
                )
        logger.debug(
            "event_emitted name=%s session=%s mode=%s status=%s trace=%s",
            event_name, session_id, mode, status, trace_id,
        )
    except Exception as exc:
        # Tracking failure must NOT interrupt main flow
        logger.warning(
            "event_emit_failed name=%s session=%s err=%s",
            event_name, session_id, str(exc)[:200],
        )


def emit_cost_event(
    *,
    sender_id: str,
    session_id: str,
    mode: str,
    model_name: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: float,
    is_retry: bool = False,
    job_id: int | None = None,
) -> None:
    """
    Record a cost entry in cost_ledger.
    Also emits cost_bucket_updated tracking event.
    Never raises.
    """
    exchange_rate = int(os.environ.get("VND_USD_EXCHANGE_RATE", "25000") or "25000")
    cost_vnd = round(cost_usd * exchange_rate)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cost_ledger (
                        sender_id, session_id, mode, model_name,
                        prompt_tokens, completion_tokens,
                        cost_usd, cost_vnd_estimate, is_retry, job_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sender_id, session_id, mode, model_name,
                        prompt_tokens, completion_tokens,
                        cost_usd, cost_vnd, is_retry, job_id,
                    ),
                )
        emit_event(
            event_name="cost_bucket_updated",
            sender_id=sender_id,
            session_id=session_id,
            mode=mode,
            step="billing",
            status="updated",
            cost_estimate=cost_usd,
            model_call_count=1,
            metadata={
                "model": model_name,
                "cost_vnd": cost_vnd,
                "is_retry": is_retry,
            },
        )
    except Exception as exc:
        logger.warning(
            "emit_cost_event_failed session=%s model=%s err=%s",
            session_id, model_name, str(exc)[:200],
        )
