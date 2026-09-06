"""
Correlation context for structured logging — sender_id, job_id, generation_id.
PR-010 · Plan v1 Phase 0 (additive observability, no behavior change).

`request_id` already has a home in app.utils.trace_context (trace_id contextvar,
set by main.py's middleware for every HTTP request). This module does not
duplicate it — it re-exports get/set for request_id so callers only need one
import, and adds the three correlation fields that trace_context does not
cover: sender_id, job_id, generation_id.

Usage:
    from app.utils import correlation

    with correlation.correlation_scope(sender_id=sender_id, request_id=rid):
        ...  # any log emitted in here can read correlation.get_sender_id()
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from app.utils.trace_context import (
    get_trace_id as get_request_id,
    peek_trace_id,
    reset_trace_id,
    set_trace_id as set_request_id,
)

_sender_id_var: ContextVar[str] = ContextVar("correlation_sender_id", default="")
_job_id_var: ContextVar[str] = ContextVar("correlation_job_id", default="")
_generation_id_var: ContextVar[str] = ContextVar("correlation_generation_id", default="")

__all__ = [
    "get_request_id",
    "set_request_id",
    "get_sender_id",
    "set_sender_id",
    "get_job_id",
    "set_job_id",
    "get_generation_id",
    "set_generation_id",
    "correlation_scope",
    "CorrelationLogFilter",
    "attach_correlation_filter_to_root",
]


def get_sender_id() -> str:
    return _sender_id_var.get()


def set_sender_id(sender_id: str) -> Token:
    return _sender_id_var.set(sender_id or "")


def get_job_id() -> str:
    return _job_id_var.get()


def set_job_id(job_id: str) -> Token:
    return _job_id_var.set(job_id or "")


def get_generation_id() -> str:
    return _generation_id_var.get()


def set_generation_id(generation_id: str) -> Token:
    return _generation_id_var.set(generation_id or "")


@contextmanager
def correlation_scope(
    *,
    sender_id: str | None = None,
    job_id: str | None = None,
    generation_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """Set the given correlation fields for the duration of the block, then restore.

    Only fields explicitly passed (not None) are touched. Restoration uses
    contextvars.Token, so nested scopes compose safely — exiting an inner
    scope restores the *outer* scope's value rather than resetting to "".
    """
    resets: list[tuple[str, object]] = []
    try:
        if sender_id is not None:
            resets.append(("sender_id", _sender_id_var.set(sender_id)))
        if job_id is not None:
            resets.append(("job_id", _job_id_var.set(job_id)))
        if generation_id is not None:
            resets.append(("generation_id", _generation_id_var.set(generation_id)))
        if request_id is not None:
            resets.append(("request_id", set_request_id(request_id)))
        yield
    finally:
        for field, token in reversed(resets):
            if field == "sender_id":
                _sender_id_var.reset(token)
            elif field == "job_id":
                _job_id_var.reset(token)
            elif field == "generation_id":
                _generation_id_var.reset(token)
            elif field == "request_id":
                reset_trace_id(token)


class CorrelationLogFilter(logging.Filter):
    """Attach request_id/sender_id/job_id/generation_id to every LogRecord.

    Values default to "" when not set in the current context — this never
    raises, even if none of the correlation contextvars were ever touched
    (e.g. a log line emitted at import time, before any request/job).

    Attaching this filter does NOT change any existing log format string; it
    only makes the fields available on the record for formatters that choose
    to reference them. See backend/docs/baseline_v1.md for the design
    decision to keep the global LOG_FORMAT unchanged in PR-010.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = peek_trace_id()
        record.sender_id = get_sender_id()
        record.job_id = get_job_id()
        record.generation_id = get_generation_id()
        return True


_correlation_filter = CorrelationLogFilter()


def attach_correlation_filter_to_root() -> None:
    """Attach the shared CorrelationLogFilter to every handler on the root logger.

    Idempotent — safe to call more than once (e.g. re-configuring logging in
    tests): skips handlers that already carry this exact filter instance.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if _correlation_filter not in handler.filters:
            handler.addFilter(_correlation_filter)
