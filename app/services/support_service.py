"""
Support handoff service — Slice 5.
V9 §9, V9.2 §3.4 / docs/ARCHITECTURE/11_support_sop.md
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandoffExecutionResult:
    success: bool
    ticket_id: int | None
    stage: str
    ticket_created: bool
    takeover_performed: bool
    ack_sent: bool


@dataclass(frozen=True)
class SupportTicketRow:
    id: int
    handoff_takeover_completed_at: Any
    handoff_ack_sent_at: Any


def _fetch_ticket_by_idempotency_key(idempotency_key: str) -> SupportTicketRow | None:
    from app.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, handoff_takeover_completed_at, handoff_ack_sent_at
                FROM support_tickets
                WHERE idempotency_key = %s
                LIMIT 1
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return SupportTicketRow(id=row[0], handoff_takeover_completed_at=row[1], handoff_ack_sent_at=row[2])


def ensure_support_ticket(
    sender_id: str,
    trigger_type: str,
    context: dict,
    *,
    idempotency_key: str | None = None,
) -> tuple[int | None, bool]:
    """
    Create or return existing support ticket keyed by idempotency_key.
    Returns (ticket_id, created_new).
    """
    from app.db import get_connection

    key = (idempotency_key or "").strip()
    payload = dict(context or {})
    if key:
        payload.setdefault("idempotency_key", key)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if key:
                    cur.execute(
                        """
                        INSERT INTO support_tickets (
                            sender_id, session_id, trigger_type, context_json, idempotency_key
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                        DO NOTHING
                        RETURNING id
                        """,
                        (sender_id, sender_id, trigger_type, json.dumps(payload), key),
                    )
                    row = cur.fetchone()
                    if row:
                        ticket_id = row[0]
                        logger.info(
                            "support_ticket_created sender=%s trigger=%s id=%s key=%s",
                            sender_id[:8],
                            trigger_type,
                            ticket_id,
                            key[:16],
                        )
                        return ticket_id, True

                    cur.execute(
                        """
                        SELECT id FROM support_tickets
                        WHERE idempotency_key = %s
                        LIMIT 1
                        """,
                        (key,),
                    )
                    existing = cur.fetchone()
                    if existing:
                        logger.info(
                            "support_ticket_idempotent_hit sender=%s id=%s key=%s",
                            sender_id[:8],
                            existing[0],
                            key[:16],
                        )
                        return existing[0], False
                    logger.error(
                        "support_ticket_idempotent_miss sender=%s key=%s",
                        sender_id[:8],
                        key[:16],
                    )
                    return None, False

                cur.execute(
                    """
                    INSERT INTO support_tickets (sender_id, session_id, trigger_type, context_json)
                    VALUES (%s, %s, %s, %s) RETURNING id
                    """,
                    (sender_id, sender_id, trigger_type, json.dumps(payload)),
                )
                row = cur.fetchone()
                ticket_id = row[0] if row else None
                logger.info(
                    "support_ticket_created sender=%s trigger=%s id=%s",
                    sender_id[:8],
                    trigger_type,
                    ticket_id,
                )
                return ticket_id, True
    except Exception:
        logger.exception("support_ticket_failed sender=%s trigger=%s", sender_id[:8], trigger_type)
        return None, False


def _mark_takeover_completed(idempotency_key: str) -> bool:
    from app.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE support_tickets
                SET handoff_takeover_completed_at = now(), updated_at = now()
                WHERE idempotency_key = %s
                  AND handoff_takeover_completed_at IS NULL
                """,
                (idempotency_key,),
            )
            return cur.rowcount > 0


def _mark_ack_sent(idempotency_key: str) -> bool:
    from app.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE support_tickets
                SET handoff_ack_sent_at = now(), updated_at = now()
                WHERE idempotency_key = %s
                  AND handoff_ack_sent_at IS NULL
                """,
                (idempotency_key,),
            )
            return cur.rowcount > 0


def _transition_support_handoff_state(sender_id: str) -> None:
    from app.services.messenger_state import ConversationState
    from app.services.messenger_state_db import DbMessengerStateStore

    store = DbMessengerStateStore()
    session = store.get_or_create(sender_id)
    session.state = ConversationState.SUPPORT_HANDOFF
    store.save(session)


def execute_support_handoff(
    sender_id: str,
    *,
    request_id: str,
    trigger_type: str,
    context: dict,
    confirmation_text: str,
    session: Any = None,
    source: str = "premium",
    error_fallback_text: str | None = None,
) -> HandoffExecutionResult:
    """
    Durable ticket → ASSISTANT takeover → one transition ack.
    Retry-safe via idempotency_key=request_id and ticket stage timestamps.
    """
    idempotency_key = (request_id or "").strip()
    if not idempotency_key:
        logger.error("support_handoff_missing_request_id sender=%s", sender_id[:8])
        return HandoffExecutionResult(
            success=False,
            ticket_id=None,
            stage="ticket_failed",
            ticket_created=False,
            takeover_performed=False,
            ack_sent=False,
        )

    ticket_id, ticket_created = ensure_support_ticket(
        sender_id,
        trigger_type,
        {**context, "handoff_source": source, "request_id": request_id},
        idempotency_key=idempotency_key,
    )
    if ticket_id is None:
        return HandoffExecutionResult(
            success=False,
            ticket_id=None,
            stage="ticket_failed",
            ticket_created=False,
            takeover_performed=False,
            ack_sent=False,
        )

    row = _fetch_ticket_by_idempotency_key(idempotency_key)
    if row is None:
        return HandoffExecutionResult(
            success=False,
            ticket_id=ticket_id,
            stage="ticket_failed",
            ticket_created=ticket_created,
            takeover_performed=False,
            ack_sent=False,
        )

    if row.handoff_ack_sent_at is not None:
        return HandoffExecutionResult(
            success=True,
            ticket_id=row.id,
            stage="already_completed",
            ticket_created=ticket_created,
            takeover_performed=False,
            ack_sent=False,
        )

    _transition_support_handoff_state(sender_id)

    takeover_performed = False
    takeover_already_done = row.handoff_takeover_completed_at is not None
    if not takeover_already_done:
        from app.services.assistant_takeover import enter_assistant_takeover

        try:
            enter_assistant_takeover(
                sender_id,
                request_id=request_id,
                session=session,
            )
            _mark_takeover_completed(idempotency_key)
            takeover_performed = True
            takeover_already_done = True
        except Exception:
            logger.exception(
                "support_handoff_takeover_failed sender=%s request_id=%s ticket_id=%s",
                sender_id[:8],
                request_id,
                ticket_id,
            )
            fallback = error_fallback_text or ""
            if fallback:
                try:
                    from app.services.assistant_takeover import deliver_assistant_handoff_confirmation

                    deliver_assistant_handoff_confirmation(
                        sender_id,
                        fallback,
                        request_id=request_id,
                    )
                except Exception:
                    logger.exception(
                        "support_handoff_takeover_error_reply_failed sender=%s",
                        sender_id[:8],
                    )
            return HandoffExecutionResult(
                success=False,
                ticket_id=row.id,
                stage="takeover_failed",
                ticket_created=ticket_created,
                takeover_performed=False,
                ack_sent=False,
            )
    else:
        logger.info(
            "support_handoff_takeover_skip_idempotent sender=%s request_id=%s",
            sender_id[:8],
            request_id,
        )

    confirmation_required = bool((confirmation_text or "").strip())
    if not confirmation_required:
        return HandoffExecutionResult(
            success=True,
            ticket_id=row.id,
            stage="completed",
            ticket_created=ticket_created,
            takeover_performed=takeover_performed,
            ack_sent=False,
        )

    ack_sent = False
    if row.handoff_ack_sent_at is None:
        from app.services.assistant_takeover import deliver_assistant_handoff_confirmation

        try:
            deliver_assistant_handoff_confirmation(
                sender_id,
                confirmation_text.strip(),
                request_id=request_id,
            )
            _mark_ack_sent(idempotency_key)
            ack_sent = True
        except Exception:
            logger.exception(
                "support_handoff_ack_failed sender=%s request_id=%s ticket_id=%s",
                sender_id[:8],
                request_id,
                row.id,
            )
            fallback = error_fallback_text or ""
            if fallback:
                try:
                    deliver_assistant_handoff_confirmation(
                        sender_id,
                        fallback,
                        request_id=request_id,
                    )
                except Exception:
                    logger.exception(
                        "support_handoff_ack_error_reply_failed sender=%s",
                        sender_id[:8],
                    )
            return HandoffExecutionResult(
                success=False,
                ticket_id=row.id,
                stage="ack_failed",
                ticket_created=ticket_created,
                takeover_performed=takeover_performed,
                ack_sent=False,
            )

    return HandoffExecutionResult(
        success=True,
        ticket_id=row.id,
        stage="completed",
        ticket_created=ticket_created,
        takeover_performed=takeover_performed,
        ack_sent=ack_sent,
    )


def create_support_ticket(
    sender_id: str,
    trigger_type: str,
    context: dict,
    *,
    idempotency_key: str | None = None,
) -> int | None:
    """
    Backward-compatible ticket creation with optional idempotency.
    Transitions session to SUPPORT_HANDOFF on success.
    """
    ticket_id, _created = ensure_support_ticket(
        sender_id,
        trigger_type,
        context,
        idempotency_key=idempotency_key,
    )
    if ticket_id is None:
        return None
    _transition_support_handoff_state(sender_id)
    return ticket_id


def build_support_handoff_message(trigger_type: str = "user_request") -> str:
    msgs = {
        "render_fail": (
            "😔 Mình gặp sự cố khi tạo kết quả cho bạn. "
            "Đội hỗ trợ đã được thông báo và sẽ liên hệ bạn trong 24h."
        ),
        "abuse": (
            "Mình đã chuyển yêu cầu của bạn để đội hỗ trợ xem xét. "
            "Bạn chờ được liên hệ nhé."
        ),
        "cost": (
            "Bạn đã đạt giới hạn sử dụng hôm nay. "
            "Nếu cần hỗ trợ thêm, mình đã chuyển yêu cầu của bạn tới đội hỗ trợ."
        ),
        "user_request": (
            "Mình đã chuyển yêu cầu của bạn tới đội hỗ trợ. "
            "Bạn sẽ được liên hệ sớm nhé. Cảm ơn bạn đã kiên nhẫn! 🙏"
        ),
    }
    return msgs.get(trigger_type, msgs["user_request"])
