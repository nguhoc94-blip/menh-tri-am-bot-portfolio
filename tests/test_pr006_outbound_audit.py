"""PR-006 AM-07 — outbound audit truth tests (one invariant per outcome)."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from app.services.outbound_audit import (
    OUTCOME_DEBUG_CAPTURE,
    OUTCOME_MISSING_TOKEN,
    OUTCOME_SEND_FAILED,
    OUTCOME_SENT_OK,
    OUTCOME_VALIDATOR_BLOCKED,
    MissingPageAccessTokenError,
)
from app.utils.sender_hash import hash_sender_id

SENDER = "psid-outbound-audit-test"
REQUEST_ID = "req-pr006-1"
FAKE_MESSAGE_ID = "mid.pr006.test.abc123"


def _outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records if "outbound_result outcome=" in r.message]


def _fake_urlopen_ok(*_args, **_kwargs):
    body = json.dumps({"message_id": FAKE_MESSAGE_ID, "recipient_id": SENDER}).encode()
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    resp.getcode.return_value = 200
    return resp


def test_send_ok_logs_sent_ok_with_message_id(monkeypatch, caplog):
    """Invariant: Graph API 200 với message_id → outcome=sent_ok, message_id logged."""
    import app.services.messenger_handler as mh

    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(
        "app.services.debug_outbound.should_capture",
        lambda _sid: False,
    )
    monkeypatch.setattr(
        "app.services.messenger_handler.urllib.request.urlopen",
        _fake_urlopen_ok,
    )

    with caplog.at_level("INFO"):
        result = mh.send_text_message(SENDER, "hello", request_id=REQUEST_ID)

    assert result.outcome == OUTCOME_SENT_OK
    assert result.message_id == FAKE_MESSAGE_ID
    lines = _outcome_lines(caplog)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_SENT_OK}" in lines[0]
    assert f"message_id={FAKE_MESSAGE_ID}" in lines[0]
    assert hash_sender_id(SENDER) in lines[0]


def test_send_failed_after_retries_logs_send_failed(monkeypatch, caplog):
    """Invariant: tất cả retry đều thất bại → outcome=send_failed."""
    from urllib.error import HTTPError

    import app.services.messenger_handler as mh

    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(
        "app.services.debug_outbound.should_capture",
        lambda _sid: False,
    )
    monkeypatch.setattr(mh, "_SEND_RETRIES", 2)
    monkeypatch.setattr(mh, "_SEND_BACKOFF_SEC", 0)

    def _always_fail(*_args, **_kwargs):
        raise HTTPError(
            url="https://graph.facebook.com/v21.0/me/messages",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=BytesIO(b'{"error":"server"}'),
        )

    monkeypatch.setattr(
        "app.services.messenger_handler.urllib.request.urlopen",
        _always_fail,
    )

    with caplog.at_level("INFO"):
        result = mh.send_text_message(SENDER, "hello", request_id=REQUEST_ID)

    assert result.outcome == OUTCOME_SEND_FAILED
    lines = _outcome_lines(caplog)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_SEND_FAILED}" in lines[0]
    assert "message_id=none" in lines[0]


def test_missing_token_logs_and_raises(monkeypatch, caplog):
    """Invariant: PAGE_ACCESS_TOKEN rỗng/thiếu → outcome=missing_token, không gửi request."""
    import app.services.messenger_handler as mh

    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        "app.services.debug_outbound.should_capture",
        lambda _sid: False,
    )

    urlopen = MagicMock()
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", urlopen)

    with caplog.at_level("INFO"):
        with pytest.raises(MissingPageAccessTokenError):
            mh.send_text_message(SENDER, "hello", request_id=REQUEST_ID)

    urlopen.assert_not_called()
    lines = _outcome_lines(caplog)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_MISSING_TOKEN}" in lines[0]


def test_debug_capture_logs_debug_capture(monkeypatch, caplog):
    """Invariant: debug session → không gửi thực, outcome=debug_capture."""
    import app.services.messenger_handler as mh

    monkeypatch.setenv("DEBUG_CHAT_ENABLED", "1")
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(
        "app.services.debug_outbound.capture_text",
        lambda _sid, _text: None,
    )

    urlopen = MagicMock()
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", urlopen)

    dbg_sender = "dbg_pr006_capture"
    with caplog.at_level("INFO"):
        result = mh.send_text_message(dbg_sender, "debug hello", request_id=REQUEST_ID)

    urlopen.assert_not_called()
    assert result.outcome == OUTCOME_DEBUG_CAPTURE
    lines = _outcome_lines(caplog)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_DEBUG_CAPTURE}" in lines[0]
    assert hash_sender_id(dbg_sender) in lines[0]


def test_validator_blocked_logs_validator_blocked(monkeypatch, caplog):
    """Invariant: guardrail hoặc validator chặn → outcome=validator_blocked trước khi gửi."""
    import app.services.messenger_handler as mh

    from app.services.outbound_audit import OutboundSendResult

    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(
        mh,
        "validate_outbound",
        lambda _ctx: (False, "blocked_by_validator"),
    )
    monkeypatch.setattr(mh, "fallback_message_blocked", lambda: "fallback text")
    monkeypatch.setattr(
        mh,
        "send_text_message",
        lambda *_a, **_k: OutboundSendResult(
            outcome=OUTCOME_SENT_OK, message_id=FAKE_MESSAGE_ID
        ),
    )
    monkeypatch.setattr(mh, "log_outbound_send", lambda **_k: None)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)

    with caplog.at_level("INFO"):
        mh.send_outbound_user_text(
            SENDER,
            "blocked original",
            request_id=REQUEST_ID,
        )

    lines = _outcome_lines(caplog)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_VALIDATOR_BLOCKED}" in lines[0]
    assert hash_sender_id(SENDER) in lines[0]
