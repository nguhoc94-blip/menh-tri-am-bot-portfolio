"""
PR-006 AM-07 — Outbound send audit truth.

Prior logging (pre-PR-006 characterization):
- ``send_text_message`` (messenger_handler.py): logs ``debug_text_captured`` on debug
  capture; ``messenger_send_skipped_missing_token`` when token absent (silent return);
  ``messenger_send_ok`` / ``messenger_send_http_error`` / ``messenger_send_failed`` on
  Graph API attempts — never records ``message_id`` or a single outcome label.
- ``send_image_to_user`` (render/sender.py): logs ``debug_image_captured`` on debug
  capture; ``send_image_skipped reason=no_page_token`` when token absent (returns
  False); ``image_sent`` on HTTP 200 without parsing ``message_id``; retry warnings then
  silent False — no unified outcome.
- ``send_outbound_user_text``: always calls ``log_outbound_send`` with
  ``validator_verdict=pass|blocked`` regardless of whether Graph API succeeded; DB row
  reads as "outbound_send" even when delivery failed or was skipped.
- ``bot_activity_log.log_outbound_send``: records validator verdict and excerpt only;
  no delivery outcome or Meta ``message_id``.

PR-006 adds structured ``outbound_result`` log lines with one of five outcomes per
send attempt (see outcome constants below).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from app.utils.sender_hash import hash_sender_id

logger = logging.getLogger(__name__)

OUTCOME_SENT_OK: Literal["sent_ok"] = "sent_ok"
OUTCOME_SEND_FAILED: Literal["send_failed"] = "send_failed"
OUTCOME_VALIDATOR_BLOCKED: Literal["validator_blocked"] = "validator_blocked"
OUTCOME_MISSING_TOKEN: Literal["missing_token"] = "missing_token"
OUTCOME_DEBUG_CAPTURE: Literal["debug_capture"] = "debug_capture"

OutboundOutcome = Literal[
    "sent_ok",
    "send_failed",
    "validator_blocked",
    "missing_token",
    "debug_capture",
]

KIND_TEXT = "text"
KIND_IMAGE = "image"


class MissingPageAccessTokenError(RuntimeError):
    """Raised when FB_PAGE_ACCESS_TOKEN is missing or empty."""


@dataclass(frozen=True)
class OutboundSendResult:
    outcome: OutboundOutcome
    message_id: str | None = None
    kind: str = KIND_TEXT

    def __bool__(self) -> bool:
        return self.outcome in (OUTCOME_SENT_OK, OUTCOME_DEBUG_CAPTURE)


def parse_graph_message_id(resp_body: bytes) -> str | None:
    """Extract message_id from Graph API send response JSON."""
    try:
        data = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    mid = data.get("message_id")
    if isinstance(mid, str) and mid.strip():
        return mid.strip()
    return None


def log_outbound_result(
    *,
    outcome: OutboundOutcome,
    kind: str,
    sender_id: str,
    message_id: str | None = None,
    request_id: str | None = None,
) -> None:
    logger.info(
        "outbound_result outcome=%s kind=%s sender_id_hash=%s message_id=%s request_id=%s",
        outcome,
        kind,
        hash_sender_id(sender_id),
        message_id or "none",
        request_id or "none",
    )
