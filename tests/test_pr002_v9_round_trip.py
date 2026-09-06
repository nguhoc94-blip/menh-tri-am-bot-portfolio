"""PR-002 — V9 session fields round-trip through PostgreSQL when flag is ON."""

from __future__ import annotations

import pytest

from app.services.messenger_state import ConversationState, MessengerSession
from app.services.messenger_state_db import DbMessengerStateStore

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

FIELD_VALUES = {
    "session_model_calls": 11,
    "cohort_label": "high_cost",
    "admin_granted_combined": True,
    "face_consent_given": True,
    "palm_asset_ids": ["palm-only-1", "palm-only-2"],
    "face_asset_ids": ["face-only-1"],
    "image_retry_count": 5,
    "abuse_flag_count": 9,
    "active_mode": "palm",
}


def _base_session(sender_id: str, **overrides) -> MessengerSession:
    kwargs = dict(V9_DEFAULTS)
    kwargs.update(overrides)
    return MessengerSession(sender_id=sender_id, state=ConversationState.CHATTING, **kwargs)


def _reload(sender_id: str) -> MessengerSession:
    session, _created = DbMessengerStateStore().get_or_create_ex(sender_id)
    return session


def _assert_field(session: MessengerSession, field: str, expected) -> None:
    assert getattr(session, field) == expected


def _assert_other_fields_default(session: MessengerSession, field: str) -> None:
    for name, default in V9_DEFAULTS.items():
        if name == field:
            continue
        assert getattr(session, name) == default, f"{name} should remain default"


@pytest.fixture
def v9_persist_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_V9_PERSIST_ENABLED", "1")


@pytest.mark.requires_postgres
@pytest.mark.parametrize("field", list(FIELD_VALUES.keys()))
def test_single_v9_field_round_trips(postgres_clean_db, v9_persist_on, field: str):
    sender_id = f"psid-pr002-rt-{field}"
    value = FIELD_VALUES[field]
    store = DbMessengerStateStore()
    store.save(_base_session(sender_id, **{field: value}))

    reloaded = _reload(sender_id)
    _assert_field(reloaded, field, value)
    _assert_other_fields_default(reloaded, field)


@pytest.mark.requires_postgres
def test_all_nine_v9_fields_round_trip_together(postgres_clean_db, v9_persist_on):
    sender_id = "psid-pr002-rt-all"
    store = DbMessengerStateStore()
    store.save(_base_session(sender_id, **FIELD_VALUES))

    reloaded = _reload(sender_id)
    for field, value in FIELD_VALUES.items():
        _assert_field(reloaded, field, value)


@pytest.mark.requires_postgres
def test_reset_restores_v9_defaults(postgres_clean_db, v9_persist_on):
    sender_id = "psid-pr002-rt-reset"
    store = DbMessengerStateStore()
    store.save(_base_session(sender_id, **FIELD_VALUES))

    reset_session = store.reset(sender_id)
    for field, default in V9_DEFAULTS.items():
        _assert_field(reset_session, field, default)

    reloaded = _reload(sender_id)
    for field, default in V9_DEFAULTS.items():
        _assert_field(reloaded, field, default)


@pytest.mark.requires_postgres
def test_v9_fields_do_not_round_trip_when_flag_off(postgres_clean_db, monkeypatch):
    monkeypatch.setenv("SESSION_V9_PERSIST_ENABLED", "0")
    sender_id = "psid-pr002-rt-flag-off"
    store = DbMessengerStateStore()
    store.save(_base_session(sender_id, **FIELD_VALUES))

    reloaded = _reload(sender_id)
    for field, default in V9_DEFAULTS.items():
        _assert_field(reloaded, field, default)


@pytest.mark.requires_postgres
def test_get_or_create_ex_new_session_v9_defaults(postgres_clean_db, v9_persist_on):
    sender_id = "psid-pr002-rt-new"
    session, created = DbMessengerStateStore().get_or_create_ex(sender_id)
    assert created is True
    for field, default in V9_DEFAULTS.items():
        _assert_field(session, field, default)
