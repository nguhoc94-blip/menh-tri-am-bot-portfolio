"""
Worker runner — polls Postgres queue and dispatches jobs.
Slice 1 · V9.3 §4.3 / docs/ARCHITECTURE/06_queue_worker.md

Run modes:
  - Separate process (production Render): python -m app.workers.runner
  - Co-process with FastAPI (local/dev): RUN_WORKER=1 in env
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.services import runtime_metrics
from app.utils import correlation
from app.workers.job_outcome import TerminalJobError, CancelledStaleError
from app.workers.queue import (
    cancel_job,
    claim_next_job,
    complete_job,
    fail_job,
    fail_job_terminal,
    reclaim_stale_jobs,
)

logger = logging.getLogger(__name__)

WORKER_ID = f"worker-{str(uuid.uuid4())[:8]}"
POLL_INTERVAL_SEC = float(os.environ.get("WORKER_POLL_INTERVAL_SEC", "2.0"))
LOCK_TIMEOUT_SEC = 180
_REAPER_INTERVAL_SEC = 60.0
_WORKER_CONCURRENCY_MAX = 64

_active_handler_count = 0
_active_handler_lock = threading.Lock()

# Interactive turns must win over long-running render jobs when the worker is idle.
_KIND_POLL_PRIORITY = (
    "chat_turn",
    "send_asset",
    "premium_v2_after_return",
    "generate_overview_bundle",
    "verify_donate",
    "analyze_image",
    "analyze_palm",
    "analyze_face",
    "analyze_combined",
    "render_reading",
    "render_asset",
    "cleanup",
    "cleanup_assets",
)

# Registry: kind → handler callable
# Each handler receives (payload: dict, job_id: int, attempt: int) → None
# Raise any exception to fail the job.
_HANDLERS: dict[str, Callable[[dict, int, int], None]] = {}


def register_handler(kind: str, fn: Callable[[dict, int, int], None]) -> None:
    """Register a job handler. Called at module import time."""
    _HANDLERS[kind] = fn
    logger.debug("job_handler_registered kind=%s", kind)


def _reaper_enabled() -> bool:
    return os.environ.get("REAPER_ENABLED", "1").strip() not in ("0", "false", "False")


def _get_worker_concurrency() -> int:
    """Parse WORKER_CONCURRENCY env: default 1, clamp [1, 64], invalid → 1."""
    raw = (os.environ.get("WORKER_CONCURRENCY") or "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        logger.warning("worker_concurrency_invalid raw=%r fallback=1", raw)
        return 1
    if value < 1:
        logger.warning("worker_concurrency_below_min raw=%r clamped=1", raw)
        return 1
    if value > _WORKER_CONCURRENCY_MAX:
        logger.warning(
            "worker_concurrency_above_max raw=%r clamped=%d", raw, _WORKER_CONCURRENCY_MAX,
        )
        return _WORKER_CONCURRENCY_MAX
    return value


def get_active_worker_slots() -> int:
    """Number of slots currently inside a handler (for heartbeat observability)."""
    with _active_handler_lock:
        return _active_handler_count


def _process_one(kind: str) -> bool:
    """Claim and process one job. Returns True if a job was processed."""
    job = claim_next_job(kind, WORKER_ID, LOCK_TIMEOUT_SEC)
    if job is None:
        return False

    job_id: int = job["id"]
    payload: dict = job["payload"]
    attempt: int = job["attempt"]
    max_attempts: int = job["max_attempts"]

    # PR-010: correlation context + duration timing. Does not change any
    # complete/fail/cancel semantics, call order, or return value below —
    # only wraps the existing block and adds `job_duration_ms` metric lines.
    corr_sender_id = payload.get("sender_id") or None
    corr_generation_id = payload.get("generation_id") or None

    with correlation.correlation_scope(
        job_id=str(job_id), sender_id=corr_sender_id, generation_id=corr_generation_id,
    ):
        t0 = time.monotonic()

        def _log_job_duration(outcome: str) -> None:
            duration_ms = (time.monotonic() - t0) * 1000
            runtime_metrics.log_metric(
                "job_duration_ms", round(duration_ms, 2),
                kind=kind, job_id=job_id, outcome=outcome,
            )

        handler = _HANDLERS.get(kind)
        if handler is None:
            fail_job(job_id, f"no_handler_for_kind:{kind}", attempt, max_attempts)
            logger.error("no_handler_for_kind kind=%s job_id=%s", kind, job_id)
            _log_job_duration("no_handler")
            return True

        try:
            logger.info(
                "job_processing kind=%s job_id=%s attempt=%d worker=%s",
                kind, job_id, attempt, WORKER_ID,
            )
            payload = {**payload, "_max_attempts": max_attempts}
            global _active_handler_count
            with _active_handler_lock:
                _active_handler_count += 1
            try:
                handler(payload, job_id, attempt)
            finally:
                with _active_handler_lock:
                    _active_handler_count -= 1
            complete_job(job_id)
            _log_job_duration("succeeded")
        except CancelledStaleError as exc:
            cancel_job(job_id, reason=str(exc) or "stale")
            logger.info(
                "job_cancelled_stale kind=%s job_id=%s attempt=%d",
                kind, job_id, attempt,
            )
            _log_job_duration("cancelled_stale")
        except TerminalJobError as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            fail_job_terminal(job_id, error_msg)
            logger.error(
                "job_terminal kind=%s job_id=%s attempt=%d err=%s",
                kind, job_id, attempt, str(exc)[:200],
            )
            _log_job_duration("failed_terminal")
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            fail_job(job_id, error_msg, attempt, max_attempts)
            logger.error(
                "job_handler_error kind=%s job_id=%s attempt=%d err=%s",
                kind, job_id, attempt, str(exc)[:200],
            )
            _log_job_duration("failed")
    return True


def _get_enabled_kinds() -> list[str]:
    """Resolve which job kinds this worker should poll.

    Order of precedence:
      1. WORKER_KINDS env (comma-separated) — explicit override.
      2. ALL kinds registered via register_handler() at import time —
         default; eliminates the bug class where adding a new handler in
         handlers.py silently doesn't get polled (hit by render_reading
         in V9.2 — jobs were enqueued but never claimed by the worker).

    Callers must ensure `app.workers.handlers` has been imported before
    invoking this helper so `_HANDLERS` is fully populated.
    """
    raw = os.environ.get("WORKER_KINDS", "")
    kinds = [k.strip() for k in raw.split(",") if k.strip()]
    if kinds:
        return kinds
    if not _HANDLERS:
        # Safety net: if handlers module wasn't imported (shouldn't happen
        # in production paths but keeps tests defensive), fall back to the
        # historical list rather than disabling the worker entirely.
        return [
            "render_asset",
            "render_reading",
            "send_asset",
            "analyze_image",
            "analyze_palm",
            "analyze_face",
            "analyze_combined",
            "cleanup",
            "cleanup_assets",
            "verify_donate",
        ]
    return list(_HANDLERS.keys())


def _order_kinds_for_poll(kinds: list[str]) -> list[str]:
    """Prefer chat_turn over render_reading so one render does not starve others."""
    rank = {kind: index for index, kind in enumerate(_KIND_POLL_PRIORITY)}
    return sorted(kinds, key=lambda kind: (rank.get(kind, len(_KIND_POLL_PRIORITY)), kind))


_stop_flag = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _stop_flag
    _stop_flag = True
    logger.info("worker_shutdown_signal received signal=%s", signum)


_HEARTBEAT_INTERVAL_SEC = 30.0


def _emit_heartbeat(
    *,
    kinds: list[str],
    jobs_processed: int,
    last_heartbeat: float,
) -> float:
    now = time.monotonic()
    if now - last_heartbeat >= _HEARTBEAT_INTERVAL_SEC:
        logger.info(
            "WORKER_HEARTBEAT worker_id=%s kinds=%s jobs_processed=%d "
            "active_worker_slots=%d uptime_sec=%.0f",
            WORKER_ID, kinds, jobs_processed, get_active_worker_slots(),
            now - last_heartbeat + _HEARTBEAT_INTERVAL_SEC,
        )
        return now
    return last_heartbeat


def _run_serial_worker_loop(kinds: list[str]) -> None:
    """Original single-thread poll loop — preserved exactly when concurrency=1."""
    last_heartbeat = time.monotonic()
    last_reap = time.monotonic()
    jobs_processed = 0

    while not _stop_flag:
        did_work = False
        for kind in kinds:
            if _stop_flag:
                break
            try:
                processed = _process_one(kind)
                did_work |= processed
                if processed:
                    jobs_processed += 1
            except Exception:
                logger.exception("worker_loop_unexpected kind=%s", kind)

        last_heartbeat = _emit_heartbeat(
            kinds=kinds, jobs_processed=jobs_processed, last_heartbeat=last_heartbeat,
        )

        if _reaper_enabled():
            now = time.monotonic()
            if now - last_reap >= _REAPER_INTERVAL_SEC:
                reclaim_stale_jobs(WORKER_ID)
                last_reap = now

        if not did_work:
            time.sleep(POLL_INTERVAL_SEC)


def _run_concurrent_worker_loop(kinds: list[str], concurrency: int) -> None:
    """Run N fixed long-lived slot loops via ThreadPoolExecutor."""
    jobs_processed = [0]
    jobs_lock = threading.Lock()

    def _slot_loop_wrapped(slot_id: int) -> None:
        while not _stop_flag:
            did_work = False
            for kind in kinds:
                if _stop_flag:
                    break
                try:
                    processed = _process_one(kind)
                    did_work |= processed
                    if processed:
                        with jobs_lock:
                            jobs_processed[0] += 1
                except Exception:
                    logger.exception("worker_loop_unexpected kind=%s", kind)

            if not did_work:
                time.sleep(POLL_INTERVAL_SEC)

    last_heartbeat = time.monotonic()
    last_reap = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="worker-slot") as executor:
        futures = [executor.submit(_slot_loop_wrapped, i) for i in range(concurrency)]

        while not _stop_flag:
            last_heartbeat = _emit_heartbeat(
                kinds=kinds,
                jobs_processed=jobs_processed[0],
                last_heartbeat=last_heartbeat,
            )

            if _reaper_enabled():
                now = time.monotonic()
                if now - last_reap >= _REAPER_INTERVAL_SEC:
                    reclaim_stale_jobs(WORKER_ID)
                    last_reap = now

            time.sleep(0.25)

        for future in futures:
            future.result()


def run_worker_loop() -> None:
    """Blocking worker loop. Call in a thread or separate process."""
    global _stop_flag
    _stop_flag = False

    # signal handlers only work on the main thread (Windows co-process mode runs in executor).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    kinds = _order_kinds_for_poll(_get_enabled_kinds())
    concurrency = _get_worker_concurrency()
    logger.info(
        "WORKER_BOOT worker_id=%s kinds=%s concurrency=%d poll_sec=%.1f handlers_registered=%d",
        WORKER_ID, kinds, concurrency, POLL_INTERVAL_SEC, len(_HANDLERS),
    )

    if concurrency == 1:
        _run_serial_worker_loop(kinds)
    else:
        _run_concurrent_worker_loop(kinds, concurrency)

    logger.info("worker_loop_stopped worker_id=%s", WORKER_ID)


async def run_worker_async() -> None:
    """Async-compatible runner for co-process mode (RUN_WORKER=1)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_worker_loop)


if __name__ == "__main__":
    # Direct execution: python -m app.workers.runner
    import sys
    from pathlib import Path

    # CRITICAL ── `python -m app.workers.runner` runs this file as `__main__`
    # but does NOT register it under its canonical name `app.workers.runner`
    # in sys.modules. Without the alias below, handlers.py's
    # `from app.workers.runner import register_handler` would load this file
    # a SECOND time, creating a separate module object with its own empty
    # `_HANDLERS` dict. Registrations would populate that orphan dict; the
    # `run_worker_loop()` running in `__main__` would still see an empty
    # `_HANDLERS` and dispatch every job with `no_handler_for_kind`.
    sys.modules.setdefault("app.workers.runner", sys.modules[__name__])

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    # Import handlers to register them (will be populated as slices are built)
    try:
        import app.workers.handlers  # noqa: F401
        logger.info(
            "worker_handlers_module_imported registered=%d kinds=%s",
            len(_HANDLERS), sorted(_HANDLERS.keys()),
        )
    except ImportError as exc:
        logger.warning("worker_handlers_module_not_found err=%s — no handlers registered yet", exc)
    except Exception as exc:  # noqa: BLE001 — diagnostic: don't swallow other errors silently
        logger.exception("worker_handlers_module_import_FAILED err=%s", exc)
        raise

    # Init DB pool
    from app.db import init_pool
    init_pool()

    run_worker_loop()
