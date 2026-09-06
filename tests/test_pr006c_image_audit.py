"""PR-006c AM-07 — persistent image outbound audit in bot_activity_log (PostgreSQL)."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from app.services.outbound_audit import OUTCOME_SENT_OK

SENDER = "psid-pr006c-image"
REQUEST_ID = "req-pr006c-1"
FAKE_MESSAGE_ID = "mid.pr006c.test.xyz789"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PAGE_NUM = 2
TOTAL_PAGES = 3
MODE = "palm"
JOB_ID = 99
PAYLOAD = {"sender_id": SENDER, "generation_id": "gen-pr006c"}


def _fetch_image_outbound_rows(get_connection, sender_id: str) -> list[tuple]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT message_excerpt, send_outcome, provider_message_id, validator_verdict, outbound_class
                FROM bot_activity_log
                WHERE sender_id = %s
                  AND event_kind = 'outbound_send'
                  AND outbound_class = 'asset_image'
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


def _setup_send_asset_intro_mocks(monkeypatch, *, page_num: int = PAGE_NUM) -> None:
    store = MagicMock()
    store.get_or_create.return_value = MagicMock()
    monkeypatch.setattr(
        "app.services.messenger_state_db.DbMessengerStateStore",
        lambda: store,
    )
    monkeypatch.setattr(
        "app.services.generation_guard.skip_if_stale_job",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("app.services.debug_outbound.should_capture", lambda _sid: False)
    monkeypatch.setattr("app.render.sender.send_text_fallback", lambda *args, **kwargs: None)


def _call_send_asset_intro_and_image(
    monkeypatch,
    *,
    page_num: int = PAGE_NUM,
    total_pages: int = TOTAL_PAGES,
    mode: str = MODE,
):
    from app.workers.handlers import _send_asset_intro_and_image

    _setup_send_asset_intro_mocks(monkeypatch, page_num=page_num)
    return _send_asset_intro_and_image(
        sender_id=SENDER,
        payload=PAYLOAD,
        job_id=JOB_ID,
        page_num=page_num,
        total_pages=total_pages,
        mode=mode,
        request_id=REQUEST_ID,
        jpeg_bytes=JPEG_BYTES,
    )


@pytest.mark.requires_postgres
def test_image_sent_with_message_id_persists_delivered(postgres_clean_db, monkeypatch):
    """Invariant: ảnh gửi thành công với message_id → bot_activity_log.send_outcome='delivered', provider_message_id=<id thật>."""
    get_connection = postgres_clean_db
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.render.sender.urllib.request.urlopen", _fake_urlopen_ok)

    result = _call_send_asset_intro_and_image(monkeypatch)

    assert bool(result) is True
    assert result.message_id == FAKE_MESSAGE_ID
    rows = _fetch_image_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    excerpt, send_outcome, provider_message_id, verdict, outbound_class = rows[0]
    assert excerpt == f"[image asset page {PAGE_NUM}/{TOTAL_PAGES} mode={MODE}]"
    assert send_outcome == "delivered"
    assert provider_message_id == FAKE_MESSAGE_ID
    assert verdict == "pass"
    assert outbound_class == "asset_image"


@pytest.mark.requires_postgres
def test_image_2xx_without_message_id_persists_delivered_null_one_request(postgres_clean_db, monkeypatch):
    """Invariant: HTTP 2xx không message_id → send_outcome='delivered', provider_message_id IS NULL, CHỈ một HTTP request (không retry)."""
    get_connection = postgres_clean_db
    call_count = {"n": 0}

    def _counting_urlopen(*_args, **_kwargs):
        call_count["n"] += 1
        return _fake_urlopen_2xx_no_mid()

    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.render.sender.urllib.request.urlopen", _counting_urlopen)

    result = _call_send_asset_intro_and_image(monkeypatch)

    assert bool(result) is True
    assert result.message_id is None
    assert call_count["n"] == 1
    rows = _fetch_image_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _, _ = rows[0]
    assert send_outcome == "delivered"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_image_missing_token_persists_skipped_no_token_no_http_request(postgres_clean_db, monkeypatch):
    """Invariant: thiếu FB_PAGE_ACCESS_TOKEN → send_outcome='skipped_no_token', urlopen không được gọi."""
    get_connection = postgres_clean_db
    urlopen = MagicMock()
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("app.render.sender.urllib.request.urlopen", urlopen)

    result = _call_send_asset_intro_and_image(monkeypatch)

    assert bool(result) is False
    urlopen.assert_not_called()
    rows = _fetch_image_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _, _ = rows[0]
    assert send_outcome == "skipped_no_token"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_image_retry_exhausted_persists_failed(postgres_clean_db, monkeypatch):
    """Invariant: mọi retry ảnh đều lỗi → send_outcome='failed', không phải 'delivered'."""
    from urllib.error import HTTPError

    get_connection = postgres_clean_db
    monkeypatch.delenv("DEBUG_CHAT_ENABLED", raising=False)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.render.sender._SEND_RETRIES", 2)
    monkeypatch.setattr("app.render.sender._SEND_BACKOFF", 0)

    def _always_fail(*_args, **_kwargs):
        raise HTTPError(
            url="https://graph.facebook.com/v21.0/me/messages",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=BytesIO(b'{"error":"server"}'),
        )

    monkeypatch.setattr("app.render.sender.urllib.request.urlopen", _always_fail)

    result = _call_send_asset_intro_and_image(monkeypatch)

    assert bool(result) is False
    rows = _fetch_image_outbound_rows(get_connection, SENDER)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _, _ = rows[0]
    assert send_outcome == "failed"
    assert send_outcome != "delivered"
    assert provider_message_id is None


@pytest.mark.requires_postgres
def test_image_debug_capture_persists_skipped_debug(postgres_clean_db, monkeypatch):
    """Invariant: debug session → send_outcome='skipped_debug', không gọi Graph API thật."""
    get_connection = postgres_clean_db
    dbg_sender = "dbg_pr006c_capture"
    urlopen = MagicMock()
    monkeypatch.setenv("DEBUG_CHAT_ENABLED", "1")
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("app.services.debug_outbound.capture_image", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.render.sender.urllib.request.urlopen", urlopen)

    store = MagicMock()
    store.get_or_create.return_value = MagicMock()
    monkeypatch.setattr(
        "app.services.messenger_state_db.DbMessengerStateStore",
        lambda: store,
    )
    monkeypatch.setattr(
        "app.services.generation_guard.skip_if_stale_job",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("app.render.sender.send_text_fallback", lambda *args, **kwargs: None)

    from app.workers.handlers import _send_asset_intro_and_image

    result = _send_asset_intro_and_image(
        sender_id=dbg_sender,
        payload={"sender_id": dbg_sender, "generation_id": "gen-dbg"},
        job_id=JOB_ID,
        page_num=PAGE_NUM,
        total_pages=TOTAL_PAGES,
        mode=MODE,
        request_id=REQUEST_ID,
        jpeg_bytes=JPEG_BYTES,
    )

    assert bool(result) is True
    urlopen.assert_not_called()
    rows = _fetch_image_outbound_rows(get_connection, dbg_sender)
    assert len(rows) == 1
    _, send_outcome, provider_message_id, _, _ = rows[0]
    assert send_outcome == "skipped_debug"
    assert provider_message_id is None
