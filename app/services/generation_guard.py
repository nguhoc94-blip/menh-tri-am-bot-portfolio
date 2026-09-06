"""Stale async job detection via session generation_id."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.services.messenger_state import MessengerSession
from app.utils.sender_hash import hash_sender_id

logger = logging.getLogger(__name__)

_active_commit_guard: ContextVar[tuple[str, str] | None] = ContextVar(
    "active_commit_guard", default=None,
)

# generation_id staleness only applies to interactive chat turns.
# Background delivery jobs (compile/bundle/render/send) must finish even when
# the user keeps chatting or session.generation_id moves forward.
_STALE_CHECK_JOB_KINDS = frozenset({"chat_turn"})


def is_stale_job(session: MessengerSession, payload: dict[str, Any]) -> bool:
    """Return True when job generation does not match the live session."""
    job_generation = payload.get("generation_id")
    if not job_generation:
        return True
    return str(job_generation) != str(session.generation_id)


def is_stale_generation(session: MessengerSession, generation_id: str | None) -> bool:
    """Return True when generation_id does not match the live session."""
    if not generation_id:
        return True
    return str(generation_id) != str(session.generation_id)


def log_stale_job_skipped(
    *,
    job_id: int,
    job_kind: str,
    sender_id: str,
    job_generation_id: str | None,
    current_generation_id: str,
) -> None:
    logger.info(
        "stale_job_skipped event=stale_job_skipped job_id=%s job_kind=%s "
        "sender_id_hash=%s job_generation_id=%s current_generation_id=%s",
        job_id,
        job_kind,
        hash_sender_id(sender_id),
        job_generation_id or "",
        current_generation_id,
    )


def skip_if_stale_job(
    session: MessengerSession,
    payload: dict[str, Any],
    *,
    job_id: int,
    job_kind: str,
) -> None:
    """Raise CancelledStaleError when a chat_turn job was superseded by a newer turn.

    Delivery / render / analyze jobs ignore generation_id mismatches — use
    domain-specific guards (e.g. _is_bundle_still_desired) for those instead.
    """
    if job_kind not in _STALE_CHECK_JOB_KINDS:
        return
    if not is_stale_job(session, payload):
        return
    log_stale_job_skipped(
        job_id=job_id,
        job_kind=job_kind,
        sender_id=session.sender_id,
        job_generation_id=payload.get("generation_id"),
        current_generation_id=str(session.generation_id),
    )
    from app.workers.job_outcome import CancelledStaleError
    raise CancelledStaleError(
        f"job {job_id} ({job_kind}) generation superseded"
    )


def skip_if_stale_generation(
    session: MessengerSession,
    generation_id: str | None,
    *,
    job_id: int = 0,
    job_kind: str = "",
) -> bool:
    """Return True when a side-effect helper must not run."""
    if not is_stale_generation(session, generation_id):
        return False
    if job_kind:
        log_stale_job_skipped(
            job_id=job_id,
            job_kind=job_kind,
            sender_id=session.sender_id,
            job_generation_id=generation_id,
            current_generation_id=str(session.generation_id),
        )
    return True


def payload_generation_id(payload: dict[str, Any]) -> str | None:
    raw = payload.get("generation_id")
    return str(raw) if raw else None


def with_generation_id(payload: dict[str, Any], generation_id: str) -> dict[str, Any]:
    """Return payload copy carrying the job's generation (not session reload)."""
    out = dict(payload)
    out["generation_id"] = generation_id
    return out


@contextmanager
def commit_guard(sender_id: str, expected_generation_id: str) -> Iterator[None]:
    """Ambient scope: while active, DbMessengerStateStore.save() for this exact sender_id
    uses CAS (save_if_current_generation) instead of the unconditional upsert, and
    check_fresh_or_raise() below becomes a real live-DB check instead of a no-op."""
    token = _active_commit_guard.set((sender_id, expected_generation_id))
    try:
        yield
    finally:
        _active_commit_guard.reset(token)


def active_expected_generation(sender_id: str) -> str | None:
    """Return the expected_generation_id if commit_guard() is active for exactly this sender_id, else None."""
    ctx = _active_commit_guard.get()
    if ctx is None or ctx[0] != sender_id:
        return None
    return ctx[1]


def reaffirm_commit_guard(sender_id: str, new_generation_id: str) -> None:
    """Re-arm the active commit_guard's expected generation after a terminal,
    self-contained state transition (session reset / cancel / support-handoff
    / switch-mode — see messenger_handler.py's `_handle_reset` and the
    `gate.state_changed` branch of `handle_incoming_text`) that intentionally
    bumps generation_id AS ITS OWN outcome, with no further generation-
    dependent work left to run in this turn.

    Bug fixed (2026): without this, a plain-text "reset"/"huỷ"/"hỗ trợ"/
    "chuyển mode" command — deferred to a chat_turn job since PR-L1 like any
    other text — would bump the session's generation_id as part of doing
    exactly what it was asked to do, and then `check_fresh_or_raise()`
    (called once in handlers.py::handle_chat_turn right after
    handle_incoming_text() returns, guarding the final send) would compare
    the live (just-bumped) generation against the job's ORIGINAL
    expected_generation_id captured at enqueue time, see a mismatch, and
    raise CancelledStaleError — silently swallowing the turn's own
    confirmation reply even though nothing external ever superseded it; the
    turn superseded itself, on purpose, as its one and only outcome. The
    backend-side reset/cancel did happen, but the user never got told.

    Safe because: this only widens what the CURRENT turn's OWN later
    check_fresh_or_raise() calls accept as fresh — it does not touch
    save_if_current_generation()'s CAS predicate (each `store.save()` call
    reads `active_expected_generation()` fresh, so a genuinely stale
    SUBSEQUENT write under this same guard, using an object that still
    carries the pre-bump generation_id, is unaffected and still fails its
    CAS check against whatever the DB row's generation now is). Callers
    whose reply depends on data that a concurrent/interleaved external write
    could invalidate (e.g. conversation_bridge._mode_generate mid-GPT) must
    keep relying on plain check_fresh_or_raise()/send_progress_stage() —
    never call this from there.
    """
    ctx = _active_commit_guard.get()
    if ctx is None or ctx[0] != sender_id:
        return
    _active_commit_guard.set((sender_id, new_generation_id))


def check_fresh_or_raise(
    sender_id: str,
    *,
    job_id: int | str = "",
    job_kind: str = "chat_turn",
) -> None:
    """Ambient freshness check via a LIVE DB read (not an in-memory session snapshot).

    No-op if no commit_guard is active for this sender_id. When a guard IS active,
    does a live DbMessengerStateStore.get_current_generation_id(sender_id) read and
    raises CancelledStaleError if it no longer matches the guard's expected_generation_id.
    """
    expected = active_expected_generation(sender_id)
    if expected is None:
        return
    from app.services.messenger_state_db import DbMessengerStateStore

    store = DbMessengerStateStore()
    current = store.get_current_generation_id(sender_id)
    if current is not None and str(current) == str(expected):
        return
    log_stale_job_skipped(
        job_id=int(job_id) if job_id else 0,
        job_kind=job_kind,
        sender_id=sender_id,
        job_generation_id=expected,
        current_generation_id=str(current or ""),
    )
    from app.workers.job_outcome import CancelledStaleError

    raise CancelledStaleError(
        f"stale commit for sender (expected_generation_id={expected})"
    )
