"""PR-004b — unified retry budget wiring and analyze_combined guardrail terminal."""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from app.services.analyzer import GuardrailBlockedError
from app.services.messenger_state import ConversationState, MessengerSession
from app.workers.job_outcome import TerminalJobError
from app.workers.retry_budget import get_budget

GEN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SENDER = "pr004b_budget_user"


def _session(**kwargs) -> MessengerSession:
    defaults: dict = {
        "sender_id": SENDER,
        "generation_id": GEN,
        "state": ConversationState.ANALYZING,
        "routing": {},
    }
    defaults.update(kwargs)
    return MessengerSession(**defaults)


def test_send_retries_reads_from_retry_budget() -> None:
    """Invariant: _SEND_RETRIES reads from RETRY_BUDGETS['send_asset']['service_max']."""
    from app.render import sender

    assert sender._SEND_RETRIES == get_budget("send_asset")["service_max"]


def test_send_asset_enqueue_max_attempts_matches_queue_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant: send_asset job is enqueued with max_attempts = queue_max from budget."""
    from app.workers import handlers as h

    monkeypatch.setenv("MESSENGER_MAX_RENDER_CARDS", "1")
    session = _session(state=ConversationState.HAS_CHART)
    store = MagicMock()
    store.get_or_create.return_value = session
    store.save.return_value = None

    mock_enqueue = MagicMock(return_value=99)
    expected_queue_max = get_budget("send_asset")["queue_max"]

    with ExitStack() as stack:
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ({"sections": {"intro": "x"}}, "done")
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        stack.enter_context(patch("app.db.get_connection", return_value=mock_conn))
        stack.enter_context(patch.object(h, "_safe_emit_event"))
        stack.enter_context(
            patch("app.render.renderer.render_all_pages", return_value=[b"jpeg" * 100])
        )
        stack.enter_context(patch("app.render.renderer.render_cta_card", return_value=None))
        stack.enter_context(
            patch("app.media.storage.get_storage_backend", return_value=MagicMock())
        )
        stack.enter_context(
            patch("app.media.storage.make_storage_key", return_value="key/asset.jpg")
        )
        stack.enter_context(
            patch("app.services.messenger_state_db.DbMessengerStateStore", return_value=store)
        )
        stack.enter_context(patch("app.workers.queue.enqueue", mock_enqueue))

        h.handle_render_asset(
            payload={
                "sender_id": SENDER,
                "asset_id": "input_asset_123",
                "mode": "palm",
                "request_id": "rq_budget",
                "generation_id": GEN,
            },
            job_id=50,
            attempt=1,
        )

    assert mock_enqueue.call_count >= 1
    for call in mock_enqueue.call_args_list:
        if call.kwargs.get("kind") == "send_asset":
            assert call.kwargs["max_attempts"] == expected_queue_max
            break
    else:
        pytest.fail("enqueue was not called with kind='send_asset'")


def test_analyze_combined_guardrail_raises_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant: GuardrailBlockedError in analyze_combined raises TerminalJobError."""
    from app.workers import handlers as h

    session = _session(state=ConversationState.ANALYZING)
    store = MagicMock()
    store.get_or_create.return_value = session
    store.save.return_value = None

    mock_fallback = MagicMock()
    mock_synth = MagicMock(
        side_effect=GuardrailBlockedError("policy_violation")
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.messenger_state_db.DbMessengerStateStore", return_value=store)
        )
        stack.enter_context(
            patch(
                "app.services.combined_pipeline.validate_asset_for_combined",
                return_value=(True, None, {"raw_text": "palm analysis"}),
            )
        )
        stack.enter_context(
            patch("app.services.analyzer.analyze_combined_synthesis", mock_synth)
        )
        stack.enter_context(patch("app.services.cost_enforcer.check_token_day_cap"))
        stack.enter_context(patch.object(h, "_send_fallback_message", mock_fallback))

        with pytest.raises(TerminalJobError, match="analyze_combined guardrail blocked job_id=77"):
            h.handle_analyze_combined(
                {
                    "sender_id": SENDER,
                    "palm_asset_id": "palm-1",
                    "chart_summary": "chart text",
                    "request_id": "rq_guardrail",
                    "generation_id": GEN,
                },
                job_id=77,
                attempt=1,
            )

    mock_fallback.assert_called_once()
    mock_synth.assert_called_once()
