"""
Request-scoped trace_id context for structured logging.
Slice 1 · V9.1 §4.5

Usage:
    # In middleware: set_trace_id(rid)
    # In any log call: get_trace_id() → include in log kwargs
    # In job payloads: propagate trace_id so worker logs are correlated
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def set_trace_id(trace_id: str) -> Token:
    """Set the current request's trace_id (call from middleware).

    Returns the contextvars.Token so callers that need to nest scopes safely
    (PR-010 · app.utils.correlation.correlation_scope) can restore the prior
    value via reset_trace_id() instead of clobbering it with "".
    """
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: Token) -> None:
    """Restore trace_id to the value captured by an earlier set_trace_id() token."""
    _trace_id_var.reset(token)


def get_trace_id() -> str:
    """Return current trace_id, or generate a fallback if not set."""
    tid = _trace_id_var.get()
    if not tid:
        tid = str(uuid.uuid4())
        _trace_id_var.set(tid)
    return tid


def peek_trace_id() -> str:
    """Return current trace_id without generating one if unset (empty string if unset).

    Safe for use in logging filters that run on every log record — unlike
    get_trace_id(), this never has the side effect of minting a fresh id.
    """
    return _trace_id_var.get()


def new_trace_id() -> str:
    """Generate a fresh trace_id and set it as the current context."""
    tid = str(uuid.uuid4())
    set_trace_id(tid)
    return tid
