"""
Donate service — Slice 5.
V9 §6.3, V9.2 §8.2 / docs/ARCHITECTURE/10_cost_and_abuse.md

Policy (product-approved, 2026-06-01):
  - User self-claim → creates donate_report with status='reported' ONLY
  - Only admin manual verify can set status='verified' and unlock supporter entitlement
  - Final bank/QR info is config-driven; placeholders block production if unset
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Config-driven bank info (placeholders block prod if unset) ───────
def get_bank_info() -> dict[str, str]:
    """Return bank info from environment. Empty strings = not configured."""
    return {
        "bank_block": (os.environ.get("MESSENGER_PART_2_BANK_BLOCK") or "").strip(),
        "donate_text": (os.environ.get("DONATION_TEXT") or "").strip(),
        "donation_url": (os.environ.get("DONATION_URL") or "").strip(),
        "qr_configured": bool((os.environ.get("DONATION_URL") or "").strip()),
    }


def build_donate_prompt_message() -> str:
    """
    Build the donation CTA message (on-demand / admin flows).
    Delegates to support_cta_service for unified copy.
    """
    from app.services.support_cta_service import build_support_cta_message

    msg = build_support_cta_message()
    if msg.strip():
        return msg

    logger.warning("donate_bank_info_not_configured")
    return (
        "🙏 Cảm ơn bạn đã quan tâm đến việc ủng hộ!\n\n"
        "Thông tin chuyển khoản đang được cập nhật. "
        "Bạn quay lại sau nhé!"
    )


def create_donate_report(
    sender_id: str,
    amount_claimed: int | None,
    transfer_note: str | None,
) -> int:
    """
    Create a donate_report with status='reported'.
    NEVER sets status='verified' — admin-only.

    Returns: new report ID.
    """
    from app.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO donate_reports (sender_id, session_id, amount_claimed, transfer_note, status)
                VALUES (%s, %s, %s, %s, 'reported')
                RETURNING id
                """,
                (sender_id, sender_id, amount_claimed, (transfer_note or "")[:500]),
            )
            row = cur.fetchone()
            report_id = row[0] if row else None

    logger.info("donate_report_created sender=%s id=%s amount=%s", sender_id[:8], report_id, amount_claimed)

    # Update session state
    try:
        from app.services.messenger_state import ConversationState
        from app.services.messenger_state_db import DbMessengerStateStore
        store = DbMessengerStateStore()
        session = store.get_or_create(sender_id)
        session.state = ConversationState.DONATE_REPORTED
        store.save(session)
    except Exception:
        logger.exception("donate_state_update_failed sender=%s", sender_id[:8])

    return report_id


def is_verified_supporter(sender_id: str) -> bool:
    """
    Return True if sender has at least one verified donate_report.
    This is the single source of truth for supporter entitlement.
    """
    try:
        from app.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM donate_reports
                    WHERE sender_id=%s AND status='verified'
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
                return (row[0] if row else 0) > 0
    except Exception:
        logger.exception("supporter_check_failed sender=%s", sender_id[:8])
        return False


def admin_verify_donation(
    report_id: int,
    admin_email: str,
    note: str = "",
) -> bool:
    """
    Admin-only: set donate_report to 'verified' and update session cohort.
    Returns True on success.
    """
    from app.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE donate_reports
                    SET status='verified', verified_by=%s, admin_note=%s, verified_at=now(), updated_at=now()
                    WHERE id=%s AND status='reported'
                    RETURNING sender_id
                    """,
                    (admin_email, note[:500], report_id),
                )
                row = cur.fetchone()
                if not row:
                    return False
                sender_id = row[0]

        # Upgrade cohort to supporter
        _upgrade_to_supporter(sender_id)
        logger.info("donation_verified report_id=%d sender=%s by=%s", report_id, sender_id[:8], admin_email)
        return True
    except Exception:
        logger.exception("admin_verify_failed report_id=%d", report_id)
        return False


def _upgrade_to_supporter(sender_id: str) -> None:
    """Set cohort_label='supporter' and admin_granted_combined=True in session."""
    try:
        from app.services.messenger_state_db import DbMessengerStateStore
        from app.services.messenger_state import ConversationState
        store = DbMessengerStateStore()
        session = store.get_or_create(sender_id)
        session.cohort_label = "supporter"
        session.admin_granted_combined = True
        if session.state == ConversationState.DONATE_REPORTED:
            session.state = ConversationState.DONATE_VERIFIED
        store.save(session)
        logger.info("cohort_upgraded_to_supporter sender=%s", sender_id[:8])
    except Exception:
        logger.exception("supporter_upgrade_failed sender=%s", sender_id[:8])


def build_donate_reported_message() -> str:
    """User-facing message after donate report submitted."""
    return (
        "✅ Mình đã ghi nhận thông tin ủng hộ của bạn!\n\n"
        "Đội hỗ trợ sẽ xác nhận trong vòng 24h. Cảm ơn bạn rất nhiều! 🙏"
    )


def build_donate_verified_message() -> str:
    """User-facing message after admin verifies donation."""
    return (
        "🌟 Cảm ơn bạn rất nhiều! Đóng góp của bạn đã được xác nhận.\n\n"
        "Demo Bot trân trọng sự ủng hộ của bạn! 🙏"
    )
