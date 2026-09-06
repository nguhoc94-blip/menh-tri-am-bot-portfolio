"""Shared eval instrumentation for provider usage metadata (opt-in via MTA_EVAL_MODE=1)."""

from __future__ import annotations

import contextvars
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Any, Iterator

_eval_operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "eval_operation_id", default=None
)
_eval_attempt: contextvars.ContextVar[int] = contextvars.ContextVar("eval_attempt", default=1)


def is_eval_mode() -> bool:
    return (os.environ.get("MTA_EVAL_MODE") or "").strip().lower() in ("1", "true", "yes")


def _usage_get(usage: Any, key: str) -> Any:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _sum_optional(values: list[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def count_words(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return len(stripped.split())


def configured_output_token_limit_from_kwargs(kwargs: dict[str, Any]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
        val = _coerce_int(kwargs.get(key))
        if val is not None:
            return val
    return None


def extract_response_text(completion: Any) -> str | None:
    choices = getattr(completion, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            content = getattr(message, "content", None)
            if content is not None:
                return str(content)
    output_text = getattr(completion, "output_text", None)
    if output_text is not None:
        return str(output_text)
    return None


def extract_finish_reason(completion: Any) -> str | None:
    choices = getattr(completion, "choices", None)
    if choices:
        reason = getattr(choices[0], "finish_reason", None)
        if reason is not None:
            return str(reason)
    status = getattr(completion, "status", None)
    if status is not None:
        return str(status)
    return None


def extract_incomplete(completion: Any, finish_reason: str | None) -> bool | None:
    if finish_reason == "length":
        return True
    if finish_reason in ("stop", "end_turn"):
        return False
    incomplete_details = getattr(completion, "incomplete_details", None)
    if incomplete_details is not None:
        return True
    return None


def compute_premium_complete(
    *,
    status: str,
    postcheck_kind: str,
    api_calls: list["ModelCallRecord"],
) -> bool:
    """Premium is fully complete only when sections pass AND output was not truncated."""
    sections_ok = postcheck_kind == "ok" or status == "premium_ok"
    if not sections_ok or not api_calls:
        return False
    last = api_calls[-1]
    if last.finish_reason != "stop":
        return False
    if last.incomplete is True:
        return False
    return True


def normalize_usage(usage: Any) -> dict[str, int | None]:
    """Normalize Chat Completions or Responses API usage into eval artifact fields."""
    empty: dict[str, int | None] = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    if usage is None:
        return empty

    input_tokens = _coerce_int(_usage_get(usage, "input_tokens"))
    if input_tokens is None:
        input_tokens = _coerce_int(_usage_get(usage, "prompt_tokens"))

    output_tokens = _coerce_int(_usage_get(usage, "output_tokens"))
    if output_tokens is None:
        output_tokens = _coerce_int(_usage_get(usage, "completion_tokens"))

    cached_input_tokens: int | None = None
    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        details = _usage_get(usage, details_key)
        if details is not None:
            cached_input_tokens = _coerce_int(_usage_get(details, "cached_tokens"))
            if cached_input_tokens is not None:
                break

    reasoning_tokens: int | None = None
    for details_key in ("output_tokens_details", "completion_tokens_details"):
        details = _usage_get(usage, details_key)
        if details is not None:
            reasoning_tokens = _coerce_int(_usage_get(details, "reasoning_tokens"))
            if reasoning_tokens is not None:
                break

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": _coerce_int(_usage_get(usage, "total_tokens")),
    }


@dataclass
class ModelCallRecord:
    source: str
    model: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    latency_ms: float
    attempt: int = 1
    operation_id: str | None = None
    success: bool = True
    error: str | None = None
    configured_output_token_limit: int | None = None
    finish_reason: str | None = None
    incomplete: bool | None = None
    generated_chars: int | None = None
    generated_words: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OperationSummary:
    operation_id: str
    source: str
    api_calls: list[ModelCallRecord] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.api_calls)

    @property
    def input_tokens(self) -> int | None:
        return _sum_optional([r.input_tokens for r in self.api_calls])

    @property
    def cached_input_tokens(self) -> int | None:
        return _sum_optional([r.cached_input_tokens for r in self.api_calls])

    @property
    def output_tokens(self) -> int | None:
        return _sum_optional([r.output_tokens for r in self.api_calls])

    @property
    def reasoning_tokens(self) -> int | None:
        return _sum_optional([r.reasoning_tokens for r in self.api_calls])

    @property
    def total_tokens(self) -> int | None:
        return _sum_optional([r.total_tokens for r in self.api_calls])

    @property
    def retry_count(self) -> int:
        if not self.api_calls:
            return 0
        return max((r.attempt or 1) - 1 for r in self.api_calls)

    @property
    def operation_completed(self) -> bool:
        return bool(self.api_calls) and all(r.success for r in self.api_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "source": self.source,
            "api_call_count": self.call_count,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "retry_count": self.retry_count,
            "operation_completed": self.operation_completed,
            "api_calls": [r.to_dict() for r in self.api_calls],
        }


class EvalUsageRecorder:
    _lock = Lock()
    _records: list[ModelCallRecord] = []
    _operations: dict[str, OperationSummary] = {}

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._records.clear()
            cls._operations.clear()

    @classmethod
    def begin_operation(cls, operation_id: str, source: str) -> None:
        with cls._lock:
            cls._operations[operation_id] = OperationSummary(
                operation_id=operation_id,
                source=source,
            )

    @classmethod
    def end_operation(cls, operation_id: str) -> OperationSummary | None:
        with cls._lock:
            return cls._operations.get(operation_id)

    @classmethod
    def all_records(cls) -> list[ModelCallRecord]:
        with cls._lock:
            return list(cls._records)

    @classmethod
    def _append(cls, record: ModelCallRecord) -> None:
        with cls._lock:
            cls._records.append(record)
            if record.operation_id and record.operation_id in cls._operations:
                cls._operations[record.operation_id].api_calls.append(record)

    @classmethod
    def record_completion(
        cls,
        completion: Any,
        *,
        model: str,
        source: str | None = None,
        latency_ms: float,
        attempt: int | None = None,
        operation_id: str | None = None,
        configured_output_token_limit: int | None = None,
        finish_reason: str | None = None,
        incomplete: bool | None = None,
        generated_chars: int | None = None,
        generated_words: int | None = None,
    ) -> None:
        if not is_eval_mode():
            return
        normalized = normalize_usage(getattr(completion, "usage", None))
        if finish_reason is None:
            finish_reason = extract_finish_reason(completion)
        if incomplete is None:
            incomplete = extract_incomplete(completion, finish_reason)
        if generated_chars is None or generated_words is None:
            response_text = extract_response_text(completion)
            if response_text is not None:
                if generated_chars is None:
                    generated_chars = len(response_text)
                if generated_words is None:
                    generated_words = count_words(response_text)
        record = ModelCallRecord(
            source=source or "unknown",
            model=model,
            input_tokens=normalized["input_tokens"],
            cached_input_tokens=normalized["cached_input_tokens"],
            output_tokens=normalized["output_tokens"],
            reasoning_tokens=normalized["reasoning_tokens"],
            total_tokens=normalized["total_tokens"],
            latency_ms=latency_ms,
            attempt=attempt if attempt is not None else _eval_attempt.get(),
            operation_id=operation_id or _eval_operation_id.get(),
            success=True,
            error=None,
            configured_output_token_limit=configured_output_token_limit,
            finish_reason=finish_reason,
            incomplete=incomplete,
            generated_chars=generated_chars,
            generated_words=generated_words,
        )
        cls._append(record)

    @classmethod
    def record_failure(
        cls,
        *,
        model: str,
        source: str | None = None,
        latency_ms: float,
        attempt: int | None = None,
        operation_id: str | None = None,
        error: str,
        configured_output_token_limit: int | None = None,
    ) -> None:
        if not is_eval_mode():
            return
        record = ModelCallRecord(
            source=source or "unknown",
            model=model,
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            total_tokens=None,
            latency_ms=latency_ms,
            attempt=attempt if attempt is not None else _eval_attempt.get(),
            operation_id=operation_id or _eval_operation_id.get(),
            success=False,
            error=error[:400],
            configured_output_token_limit=configured_output_token_limit,
            finish_reason=None,
            incomplete=None,
            generated_chars=None,
            generated_words=None,
        )
        cls._append(record)


def usage_from_completion(completion: Any) -> dict[str, Any] | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    return normalize_usage(usage)


@contextmanager
def eval_operation(operation_id: str, source: str) -> Iterator[None]:
    token_op = _eval_operation_id.set(operation_id)
    EvalUsageRecorder.begin_operation(operation_id, source)
    try:
        yield
    finally:
        _eval_operation_id.reset(token_op)


@contextmanager
def eval_attempt(attempt: int) -> Iterator[None]:
    token = _eval_attempt.set(attempt)
    try:
        yield
    finally:
        _eval_attempt.reset(token)


def maybe_wrap_openai_client(client: Any, *, source: str | None = None) -> Any:
    """Wrap chat.completions.create to capture provider usage when eval mode is on."""
    if not is_eval_mode():
        return client

    original_create = client.chat.completions.create
    client_source = source

    def instrumented_create(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        configured_limit = configured_output_token_limit_from_kwargs(kwargs)
        started = time.perf_counter()
        try:
            completion = original_create(*args, **kwargs)
        except Exception as exc:
            EvalUsageRecorder.record_failure(
                model=str(model),
                source=client_source,
                latency_ms=(time.perf_counter() - started) * 1000,
                configured_output_token_limit=configured_limit,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        EvalUsageRecorder.record_completion(
            completion,
            model=str(model),
            source=client_source,
            latency_ms=(time.perf_counter() - started) * 1000,
            configured_output_token_limit=configured_limit,
        )
        return completion

    client.chat.completions.create = instrumented_create  # type: ignore[method-assign]
    return client
