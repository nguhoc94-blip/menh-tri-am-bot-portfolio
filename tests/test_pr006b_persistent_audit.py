"""PR-006b AM-07 — persistent outbound audit in bot_activity_log (PostgreSQL)."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.outbound_audit import OUTCOME_SENT_OK
from app.utils.sender_hash import hash_sender_id

SENDER = "psid-pr006b-persist"
REQUEST_ID = "req-pr006b-1"
FAKE_MESSAGE_ID = "mid.pr006b.test.abc123"
MIGRATION_036 = (
    Path(__file__).resolve().parents[1] / "sql" / "migrations" / "036_bot_activity_log_outcome.sql"
)


def _fetch_outbound_rows(get_connection, sender_id: str) -> list[tuple]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT message_excerpt, send_outcome, provider_message_id, validator_verdict
                FROM bot_activity_log
                WHERE sender_id = %s AND event_kind = 'outbound_send'
                ORDER BY id
                """,
                (sender_id,),
            )
            return cur.fetchall()


def _fake_urlopen_ok(*_args, **_kwargs):
    body = json.dumps({"message_id": FAKE_MESSAGE_ID, "recipient_id": SENDER}).encode()
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    resp.getcode.return_value = 200
    return resp


def _fake_urlopen_2xx_no_mid(*_args, **_kwargs):
    body = json.dumps({"recipient_id": SENDER}).encode()
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    resp.getcode.return_value = 200
    return resp


def _outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records if "outbound_result outcome=" in r.message]


@pytest.mark.requires_postgres
def test_sent_ok_persists_outcome_and_message_id(postgres_clean_db, monkeypatch):
    """Invariant: gửi thành công với message_id → bot_activity_log.send_outcome='delivered', provider_message_id=<id thật>."""
    import app.services.messenger_handler as mh

    get_connection = postgres_clean_db
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", _fake_urlopen_ok)

    mh.send_outbound_user_text(SENDER, "hello delivered", request_id=REQUEST_ID)

    rows = _fetch_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    excerpt, send_outcome, provider_message_id, verdict = rows[0]
    assert excerpt == "hello delivered"
    assert send_outcome == "delivered"
    assert provider_message_id == FAKE_MESSAGE_ID
    assert verdict == "pass"


@pytest.mark.requires_postgres
def test_http_2xx_without_message_id_persists_success_null_one_request(postgres_clean_db, monkeypatch):
    """Invariant: HTTP 2xx không message_id → send_outcome='delivered', provider_message_id IS NULL, CHỈ một HTTP request (không retry)."""
    import app.services.messenger_handler as mh

    get_connection = postgres_clean_db
    call_count = {"n": 0}

    def _counting_urlopen(*_args, **_kwargs):
        call_count["n"] += 1
        return _fake_urlopen_2xx_no_mid()

    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", _counting_urlopen)

    mh.send_outbound_user_text(SENDER, "no mid ok", request_id=REQUEST_ID)

    assert call_count["n"] == 1
    rows = _fetch_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _ = rows[0]
    assert send_outcome == "delivered"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_missing_token_persists_skipped_no_token_no_http_request(postgres_clean_db, monkeypatch):
    """Invariant: thiếu FB_PAGE_ACCESS_TOKEN → send_outcome='skipped_no_token', urlopen không được gọi."""
    import app.services.messenger_handler as mh

    from app.services.outbound_audit import MissingPageAccessTokenError

    get_connection = postgres_clean_db
    urlopen = MagicMock()
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", urlopen)

    with pytest.raises(MissingPageAccessTokenError):
        mh.send_outbound_user_text(SENDER, "no token", request_id=REQUEST_ID)

    urlopen.assert_not_called()
    rows = _fetch_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _ = rows[0]
    assert send_outcome == "skipped_no_token"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_retry_exhausted_persists_failed_not_delivered(postgres_clean_db, monkeypatch):
    """Invariant: mọi retry đều lỗi → send_outcome='failed', không phải 'delivered'."""
    from urllib.error import HTTPError

    import app.services.messenger_handler as mh

    get_connection = postgres_clean_db
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)
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

    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", _always_fail)

    mh.send_outbound_user_text(SENDER, "will fail", request_id=REQUEST_ID)

    rows = _fetch_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _ = rows[0]
    assert send_outcome == "failed"
    assert send_outcome != "delivered"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_debug_capture_persists_skipped_debug(postgres_clean_db, monkeypatch):
    """Invariant: debug session → send_outcome='skipped_debug', không gọi Graph API thật."""
    import app.services.messenger_handler as mh

    get_connection = postgres_clean_db
    dbg_sender = "dbg_pr006b_capture"
    urlopen = MagicMock()
    monkeypatch.setenv("DEBUG_CHAT_ENABLED", "1")
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.capture_text", lambda _sid, _text: None)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", urlopen)

    mh.send_outbound_user_text(dbg_sender, "debug hello", request_id=REQUEST_ID)

    urlopen.assert_not_called()
    rows = _fetch_outbound_rows(get_connection, dbg_sender)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _ = rows[0]
    assert send_outcome == "skipped_debug"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_validator_blocked_persists_blocked(postgres_clean_db, monkeypatch):
    """Invariant: nội dung gốc bị validator chặn → hàng audit của nội dung gốc có send_outcome='blocked'; không báo delivered cho nội dung bị chặn."""
    import app.services.messenger_handler as mh

    get_connection = postgres_clean_db
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr(mh, "_conversation_state_value", lambda _sid: "CHATTING")
    monkeypatch.setattr(mh, "_order_status_for_log", lambda _sid: None)
    monkeypatch.setattr(mh, "validate_outbound", lambda _ctx: (False, "blocked_by_validator"))
    monkeypatch.setattr(mh, "fallback_message_blocked", lambda: "fallback text")
    monkeypatch.setattr("app.services.messenger_handler.urllib.request.urlopen", _fake_urlopen_ok)

    mh.send_outbound_user_text(SENDER, "blocked original", request_id=REQUEST_ID)

    rows = _fetch_outbound_rows(get_connection, SENDER)
    assert len(rows) == 2
    original_excerpt, original_outcome, original_mid, original_verdict = rows[0]
    fallback_excerpt, fallback_outcome, fallback_mid, fallback_verdict = rows[1]
    assert original_excerpt == "blocked original"
    assert original_outcome == "blocked"
    assert original_mid is None
    assert original_verdict == "blocked"
    assert fallback_excerpt == "fallback text"
    assert fallback_outcome == "delivered"
    assert fallback_mid == FAKE_MESSAGE_ID
    assert fallback_verdict == "pass"


@pytest.mark.requires_postgres
def test_image_path_persists_outcome_correctly(postgres_clean_db, monkeypatch, caplog):
    """Invariant: send_image_to_user trả OutboundSendResult; bot_activity_log chỉ persist tại call site (_send_asset_intro_and_image, xem test_pr006c)."""
    from app.render.sender import send_image_to_user
    from app.services.outbound_audit import OutboundSendResult

    get_connection = postgres_clean_db
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr("app.render.sender.urllib.request.urlopen", _fake_urlopen_ok)

    with caplog.at_level("INFO"):
        result = send_image_to_user(
            SENDER,
            b"\xff\xd8\xff\xe0\x00\x10JFIF",
            request_id=REQUEST_ID,
        )

    assert isinstance(result, OutboundSendResult)
    assert result.outcome == OUTCOME_SENT_OK
    assert result.message_id == FAKE_MESSAGE_ID
    assert bool(result) is True
    rows = _fetch_outbound_rows(get_connection, SENDER)
    assert rows == []
    lines = _outcome_lines(caplog)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_SENT_OK}" in lines[0]
    assert f"message_id={FAKE_MESSAGE_ID}" in lines[0]
    assert hash_sender_id(SENDER) in lines[0]


def test_migration_036_idempotent(postgres_clean_db):
    """Invariant: chạy migration 036 hai lần liên tiếp không lỗi, cột đã tồn tại thì bỏ qua."""
    get_connection = postgres_clean_db
    sql = MIGRATION_036.read_text(encoding="utf-8")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(sql)
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'bot_activity_log'
                  AND column_name IN ('send_outcome', 'provider_message_id')
                ORDER BY column_name
                """
            )
            cols = [row[0] for row in cur.fetchall()]
        conn.commit()

    assert cols == ["provider_message_id", "send_outcome"]
