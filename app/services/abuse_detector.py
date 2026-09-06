"""
Abuse detector — Slice 5.
V9.2 §8.2 / docs/ARCHITECTURE/10_cost_and_abuse.md

Policies (product-approved, 2026-06-01):
  1. Spam: >20 messages/10 minutes → soft rate limit + abuse_flag
  2. Prompt attack: ≥3/session or ≥5/24h → safe mode + log
  3. Image abuse/consent issue → reject and guide

Detection is synchronous (call before processing a message).
Does NOT block the bot — returns AbuseDecision with action.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Literal

logger = logging.getLogger(__name__)


SPAM_WINDOW_SECONDS = 600      # 10 minutes
SPAM_MSG_THRESHOLD = 20        # >20 messages in window
PROMPT_ATTACK_SESSION_THRESHOLD = 3
PROMPT_ATTACK_DAY_THRESHOLD = 5


# ── Prompt attack patterns ───────────────────────────────────────────
# Patterns indicating jailbreak / prompt injection attempts
_ATTACK_PATTERNS = [
    re.compile(p, re.I | re.U)
    for p in [
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompt)",
        r"you\s+are\s+(now\s+)?(a\s+)?[\w\s]{0,30}(without\s+restriction|jailbreak)",
        r"pretend\s+(you\s+)?(are|have no)\s+(rules?|restriction|guardrail)",
        r"disregard\s+(your\s+)?(guideline|instruction|training)",
        r"system\s*prompt\s*(is|was|=|:)\s*[\"']",
        r"bỏ\s+qua\s+(tất\s+cả\s+)?(hướng\s+dẫn|quy\s+tắc|giới\s+hạn)",
        r"hãy\s+đóng\s+vai\s+(ai|người|bot|hệ\s+thống)\s+(không\s+có|bỏ|không\s+cần)\s+(giới\s+hạn|quy\s+tắc)",
        r"(reveal|show|print|output|repeat)\s+(your\s+)?(system\s+)?prompt",
    ]
]


@dataclass
class AbuseDecision:
    action: Literal["allow", "soft_limit", "safe_mode", "reject"]
    flag_type: str | None = None
    severity: str = "soft"
    detail: str = ""
    user_message: str | None = None  # message to send user


def check_spam(sender_id: str) -> AbuseDecision:
    """
    Check if sender is sending too many messages in the spam window.
    Uses message_burst_log table.
    """
    from app.db import get_connection
    window_start = datetime.now(timezone.utc) - timedelta(seconds=SPAM_WINDOW_SECONDS)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Insert this message
                cur.execute(
                    "INSERT INTO message_burst_log (sender_id) VALUES (%s)",
                    (sender_id,),
                )
                # Count messages in window
                cur.execute(
                    "SELECT COUNT(*) FROM message_burst_log WHERE sender_id=%s AND ts >= %s",
                    (sender_id, window_start),
                )
                row = cur.fetchone()
                count = row[0] if row else 0
    except Exception:
        logger.exception("spam_check_db_error sender=%s", sender_id[:8])
        return AbuseDecision(action="allow")

    if count > SPAM_MSG_THRESHOLD:
        logger.warning("spam_detected sender=%s count=%d", sender_id[:8], count)
        _record_abuse_flag(sender_id, "spam", "soft", f"burst_count={count}")
        return AbuseDecision(
            action="soft_limit",
            flag_type="spam",
            severity="soft",
            detail=f"burst_count={count}",
            user_message=(
                "Bạn đang gửi quá nhanh! Mình cần nghỉ một chút. "
                "Bạn thử lại sau 10 phút nhé."
            ),
        )

    return AbuseDecision(action="allow")


def check_prompt_attack(text: str, sender_id: str, session_attack_count: int) -> AbuseDecision:
    """
    Detect prompt injection / jailbreak attempts.
    Returns safe_mode decision if threshold exceeded.
    """
    text_lower = text.lower() if text else ""
    is_attack = any(p.search(text) for p in _ATTACK_PATTERNS)

    if not is_attack:
        return AbuseDecision(action="allow")

    logger.warning("prompt_attack_detected sender=%s session_count=%d", sender_id[:8], session_attack_count + 1)
    _record_abuse_flag(sender_id, "prompt_attack", "soft", f"text_prefix={text[:80]}")

    # Check 24h total
    day_count = _count_abuse_flags_today(sender_id, "prompt_attack")

    if session_attack_count + 1 >= PROMPT_ATTACK_SESSION_THRESHOLD or day_count >= PROMPT_ATTACK_DAY_THRESHOLD:
        return AbuseDecision(
            action="safe_mode",
            flag_type="prompt_attack",
            severity="hard" if day_count >= PROMPT_ATTACK_DAY_THRESHOLD else "soft",
            detail=f"session={session_attack_count + 1} day={day_count}",
            user_message=(
                "Mình không thể xử lý yêu cầu này. "
                "Nếu bạn cần hỗ trợ, hãy gõ 'hỗ trợ' để liên hệ team nhé."
            ),
        )

    return AbuseDecision(
        action="safe_mode",
        flag_type="prompt_attack",
        severity="soft",
        detail=f"attempt={session_attack_count + 1}",
        user_message=(
            "Câu hỏi này mình chưa xử lý được. "
            "Bạn thử hỏi theo cách khác nhé!"
        ),
    )


def check_image_consent_abuse(
    sender_id: str,
    mode: str,
    face_consent_given: bool,
) -> AbuseDecision:
    """Check consent state before accepting face image."""
    if mode == "face" and not face_consent_given:
        _record_abuse_flag(sender_id, "consent_bypass", "soft", "face_image_without_consent")
        return AbuseDecision(
            action="reject",
            flag_type="consent_bypass",
            severity="soft",
            detail="face_without_consent",
            user_message=(
                "Để xem tướng mặt, bạn cần xác nhận đồng ý trước. "
                "Bạn gõ 'xem tướng mặt' để bắt đầu lại nhé!"
            ),
        )
    return AbuseDecision(action="allow")


def _record_abuse_flag(
    sender_id: str,
    flag_type: str,
    severity: str = "soft",
    detail: str = "",
) -> None:
    """Insert an abuse_flag record and increment daily counter. Fire-and-forget."""
    try:
        from app.db import get_connection
        from app.services.cost_enforcer import increment_counter
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO abuse_flags (sender_id, session_id, flag_type, severity, detail)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (sender_id, sender_id, flag_type, severity, detail[:500]),
                )
        increment_counter(sender_id, "abuse_flags")
    except Exception:
        logger.exception("abuse_flag_record_failed sender=%s type=%s", sender_id[:8], flag_type)


def _count_abuse_flags_today(sender_id: str, flag_type: str) -> int:
    """Count abuse flags of given type for sender in last 24h."""
    try:
        from app.db import get_connection
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM abuse_flags
                    WHERE sender_id=%s AND flag_type=%s AND created_at >= %s
                    """,
                    (sender_id, flag_type, cutoff),
                )
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return 0


def update_session_attack_count(session, attack_delta: int = 1) -> None:
    """Increment prompt_attack count stored in session.routing."""
    if not isinstance(session.routing, dict):
        session.routing = {}
    current = session.routing.get("prompt_attack_count", 0)
    session.routing["prompt_attack_count"] = current + attack_delta


def get_session_attack_count(session) -> int:
    """Return current session prompt_attack count."""
    if isinstance(session.routing, dict):
        return int(session.routing.get("prompt_attack_count", 0))
    return 0
