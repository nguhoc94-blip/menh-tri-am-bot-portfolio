"""PR-002 — characterization: V9 fields do not round-trip when flag is OFF (baseline bug)."""

from __future__ import annotations

import pytest

from app.services.messenger_state import ConversationState, MessengerSession
from app.services.messenger_state_db import DbMessengerStateStore

SENDER = "psid-pr002-v9-char"

# Dataclass defaults — what reload must show when persistence is disabled.
V9_DEFAULTS = {
    "session_model_calls": 0,
    "cohort_label": "new",
    "admin_granted_combined": False,
    "face_consent_given": False,
    "palm_asset_ids": [],
    "face_asset_ids": [],
    "image_retry_count": 0,
    "abuse_flag_count": 0,
    "active_mode": None,
}


def _session_with_non_default_v9(sender_id: str) -> MessengerSession:
    return MessengerSession(
        sender_id=sender_id,
        state=ConversationState.CHATTING,
        session_model_calls=7,
        cohort_label="supporter",
        admin_granted_combined=True,
        face_consent_given=True,
        palm_asset_ids=["palm-abc", "palm-def"],
        face_asset_ids=["face-xyz"],
        image_retry_count=3,
        abuse_flag_count=2,
        active_mode="combined",
    )


def _assert_v9_defaults(session: MessengerSession) -> None:
    assert session.session_model_calls == V9_DEFAULTS["session_model_calls"]
    assert session.cohort_label == V9_DEFAULTS["cohort_label"]
    assert session.admin_granted_combined is V9_DEFAULTS["admin_granted_combined"]
    assert session.face_consent_given is V9_DEFAULTS["face_consent_given"]
    assert session.palm_asset_ids == V9_DEFAULTS["palm_asset_ids"]
    assert session.face_asset_ids == V9_DEFAULTS["face_asset_ids"]
    assert session.image_retry_count == V9_DEFAULTS["image_retry_count"]
    assert session.abuse_flag_count == V9_DEFAULTS["abuse_flag_count"]
    assert session.active_mode is V9_DEFAULTS["active_mode"]


@pytest.mark.requires_postgres
def test_v9_fields_lost_on_reload_when_persist_flag_off(postgres_clean_db, monkeypatch):
    """Baseline bug: save non-default V9 values, reload → all fields revert to defaults."""
    monkeypatch.delenv("SESSION_V9_PERSIST_ENABLED", raising=False)

    store = DbMessengerStateStore()
    session = _session_with_non_default_v9(SENDER)
    store.save(session)

    reloaded, _created = DbMessengerStateStore().get_or_create_ex(SENDER)
    assert _created is False
    _assert_v9_defaults(reloaded)
