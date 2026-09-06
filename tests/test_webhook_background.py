"""Webhook: 200 nhanh, background task, dedupe DB (mock)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.messenger_handler import process_messaging_pipeline
from app.services.payload_handler import PayloadApplyResult
from chat_turn_test_helpers import sync_chat_turn_enqueue_with_status

client = TestClient(app)

_SENDER = "sender_test_001"
_MID = "m_test_mid_unique_abc123"

_TEXT_EVENT_PAYLOAD = {
    "entry": [
        {
            "messaging": [
                {
                    "sender": {"id": _SENDER},
                    "message": {"text": "start", "mid": _MID},
                }
            ]
        }
    ]
}


def test_case1_webhook_returns_200_immediately() -> None:
    call_log: list[str] = []

    def _slow(*args: object, **kwargs: object) -> None:
        call_log.append("pipeline")

    with patch("app.api.messenger.process_messaging_pipeline", _slow):
        with patch("app.api.messenger.try_claim_webhook_delivery", return_value=True):
            response = client.post("/webhook", json=_TEXT_EVENT_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_case2_background_task_scheduled_with_correct_args() -> None:
    scheduled: list[dict] = []

    def _capture_add_task(fn, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        scheduled.append({"fn": fn, "args": args, "kwargs": kwargs})

    with patch("app.api.messenger.process_messaging_pipeline") as mock_pipeline:
        with patch("app.api.messenger.try_claim_webhook_delivery", return_value=True):
            with patch("fastapi.BackgroundTasks.add_task", side_effect=_capture_add_task):
                response = client.post("/webhook", json=_TEXT_EVENT_PAYLOAD)

    assert response.status_code == 200
    assert len(scheduled) >= 1
    first = scheduled[0]
    assert first["args"][0] == _SENDER
    assert first["args"][1]["message"]["text"] == "start"
    assert first["args"][2] == "start"
    assert mock_pipeline is not None


def test_case3_duplicate_mid_is_skipped() -> None:
    task_count = 0

    def _count_task(*args: object, **kwargs: object) -> None:
        nonlocal task_count
        task_count += 1

    claim_calls = iter([True, False])

    def _claim(*args: object, **kwargs: object) -> bool:
        return next(claim_calls)

    with patch("app.api.messenger.process_messaging_pipeline", _count_task):
        with patch("app.api.messenger.try_claim_webhook_delivery", side_effect=_claim):
            client.post("/webhook", json=_TEXT_EVENT_PAYLOAD)
            client.post("/webhook", json=_TEXT_EVENT_PAYLOAD)

    assert task_count == 1


def test_case4_pipeline_calls_handle_then_send() -> None:
    call_order: list[str] = []

    def _fake_handle(
        sender_id: str,
        text: str,
        *,
        request_id: str,
        messaging_event: object | None = None,
    ) -> str:
        call_order.append("handle")
        return "reply text"

    def _fake_send(sender_id: str, text: str, **kwargs: object) -> None:
        call_order.append("send")
        assert text == "reply text"

    event = {
        "sender": {"id": _SENDER},
        "message": {"text": "hi", "mid": "m1"},
    }
    with (
        patch("app.services.messenger_handler.handle_incoming_text", _fake_handle),
        patch("app.services.messenger_handler.send_outbound_user_text", _fake_send),
        patch("app.services.messenger_handler.on_inbound_messaging_event", MagicMock()),
        patch(
            "app.services.messenger_handler.apply_structured_payload",
            return_value=PayloadApplyResult(reply_immediate=None, continue_with_text=True),
        ),
        patch("app.services.messenger_handler._store.save", MagicMock()),
        patch(
            "app.services.messenger_handler._store.get_or_create_ex",
            return_value=(MagicMock(generation_id="gen-test"), False),
        ),
        patch(
            "app.services.messenger_handler._store.get_or_create",
            return_value=MagicMock(generation_id="gen-test"),
        ),
        patch("app.workers.queue.enqueue_with_status", sync_chat_turn_enqueue_with_status),
        patch("app.services.messenger_processing_feedback._feedback_enabled", return_value=False),
        patch("app.services.busy_gate.reject_if_busy", return_value=False),
        patch("app.services.busy_gate.claim_busy_for_operation", return_value="test-busy-owner"),
    ):
        process_messaging_pipeline(_SENDER, event, "hi", "req-1")

    assert call_order == ["handle", "send"]


def test_case5_pipeline_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("simulated background failure")

    event = {
        "sender": {"id": _SENDER},
        "message": {"text": "hi", "mid": "m2"},
    }
    with patch("app.services.messenger_handler.handle_incoming_text", _raise):
        with patch("app.services.messenger_handler.on_inbound_messaging_event", MagicMock()):
                with patch(
                    "app.services.messenger_handler.apply_structured_payload",
                    return_value=PayloadApplyResult(reply_immediate=None, continue_with_text=True),
                ):
                    with patch("app.services.messenger_handler._store.save", MagicMock()):
                        with patch(
                            "app.services.messenger_handler._store.get_or_create_ex",
                            return_value=(MagicMock(generation_id="gen-test"), False),
                        ):
                            with patch(
                                "app.services.messenger_handler._store.get_or_create",
                                return_value=MagicMock(generation_id="gen-test"),
                            ):
                                with patch(
                                    "app.workers.queue.enqueue_with_status",
                                    sync_chat_turn_enqueue_with_status,
                                ):
                                    with patch(
                                        "app.services.messenger_processing_feedback._feedback_enabled",
                                        return_value=False,
                                    ):
                                        with patch(
                                            "app.services.busy_gate.reject_if_busy",
                                            return_value=False,
                                        ):
                                            with patch(
                                                "app.services.busy_gate.claim_busy_for_operation",
                                                return_value="test-busy-owner",
                                            ):
                                                with caplog.at_level(logging.ERROR, logger="app.services.messenger_handler"):
                                                    process_messaging_pipeline(_SENDER, event, "hi", "req-2")

    assert any(
        "background_failed" in record.message for record in caplog.records
    ), "Expected background_failed in logs"
