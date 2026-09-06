"""
Baseline runtime metrics — PR-010 (Plan v1, Phase 0).

Additive-only observability: no product behavior change, no new migration.
Gated by RUNTIME_METRICS_ENABLED (default "0", on with "1"). When disabled,
log_metric() and log_snapshot() are safe no-ops — no DB query, no log line.

Every metric is emitted through log_metric() as a single structured line:

    runtime_metric metric=<name> value=<value> key=val key=val ...

An operator can extract a series from Render logs with `grep 'metric=<name>'`.
See backend/docs/baseline_v1.md for the full metric catalog and real log
line examples.

PII rule: sender_id is never logged raw in a runtime_metric line — log_metric()
hashes any `sender_id` tag via app.utils.sender_hash.hash_sender_id() before
emitting.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from app.db import get_connection
from app.utils.sender_hash import hash_sender_id

logger = logging.getLogger(__name__)


_DEFAULT_METRICS_INTERVAL_SEC = 60.0
_MIN_METRICS_INTERVAL_SEC = 5.0


def _enabled() -> bool:
    return (os.environ.get("RUNTIME_METRICS_ENABLED") or "0").strip() == "1"


def validate_metrics_interval_sec(raw: str | None) -> float:
    """Parse RUNTIME_METRICS_INTERVAL_SEC; invalid values fall back to 60.0."""
    fallback = _DEFAULT_METRICS_INTERVAL_SEC

    if raw is None or not str(raw).strip():
        logger.warning(
            "runtime_metrics_interval_sec_invalid raw=%r fallback=%s",
            raw,
            fallback,
        )
        return fallback

    try:
        value = float(str(raw).strip())
    except (ValueError, TypeError):
        logger.warning(
            "runtime_metrics_interval_sec_invalid raw=%r fallback=%s",
            raw,
            fallback,
        )
        return fallback

    if value <= 0 or value < _MIN_METRICS_INTERVAL_SEC:
        logger.warning(
            "runtime_metrics_interval_sec_invalid raw=%r fallback=%s",
            raw,
            fallback,
        )
        return fallback

    return value


def log_metric(name: str, value: float, **tags: str | int | float) -> None:
    """Emit one structured `runtime_metric` log line. No-op when disabled.

    `sender_id`, if present in tags, is hashed (never logged raw — no PSID
    in logs, matching the redaction rule already enforced for other logging).
    """
    if not _enabled():
        return
    safe_tags = dict(tags)
    raw_sender = safe_tags.get("sender_id")
    if raw_sender:
        safe_tags["sender_id"] = hash_sender_id(str(raw_sender))
    tag_str = " ".join(f"{k}={v}" for k, v in safe_tags.items())
    if tag_str:
        logger.info("runtime_metric metric=%s value=%s %s", name, value, tag_str)
    else:
        logger.info("runtime_metric metric=%s value=%s", name, value)


def jobs_by_kind_and_status() -> list[dict]:
    """Row count of `jobs` grouped by (kind, status). [] on any DB error."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kind, status, count(*) AS n FROM jobs "
                    "GROUP BY kind, status ORDER BY kind, status"
                )
                rows = cur.fetchall()
        return [{"kind": r[0], "status": r[1], "n": int(r[2])} for r in rows]
    except Exception as exc:
        logger.warning("runtime_metrics_jobs_by_kind_and_status_failed err=%s", exc)
        return []


def stale_running_jobs_count(*, as_of: datetime | None = None) -> int:
    """Count jobs stuck in `running` past their lease (`locked_until`). 0 on error."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if as_of is not None:
                    cur.execute(
                        "SELECT count(*) FROM jobs WHERE status = 'running' AND locked_until < %s",
                        (as_of,),
                    )
                else:
                    cur.execute(
                        "SELECT count(*) FROM jobs WHERE status = 'running' AND locked_until < now()"
                    )
                row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("runtime_metrics_stale_running_jobs_failed err=%s", exc)
        return 0


def connection_usage() -> dict:
    """`{"in_use": N, "max_connections": M}` from a live query. Zeros on error.

    Reference value from backend/docs/infra_constants.md (PR-000, provisional):
    max_connections ≈ 100 on the Render DEMO-db-v2 instance. This function
    always queries live rather than trusting that reference.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW max_connections")
                max_conn_row = cur.fetchone()
                cur.execute("SELECT count(*) FROM pg_stat_activity")
                in_use_row = cur.fetchone()
        return {
            "in_use": int(in_use_row[0]) if in_use_row else 0,
            "max_connections": int(max_conn_row[0]) if max_conn_row else 0,
        }
    except Exception as exc:
        logger.warning("runtime_metrics_connection_usage_failed err=%s", exc)
        return {"in_use": 0, "max_connections": 0}


def threadpool_snapshot() -> dict | None:
    """`{"total_tokens": N, "borrowed_tokens": M}` for the AnyIO default thread
    limiter, or None if unavailable.

    Only readable from inside a running async event loop (uvicorn's request
    handling context). Returns None — never raises — when called outside one
    (e.g. from the standalone worker process, or from a sync test) or if the
    installed anyio version does not expose this API.
    """
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        return {
            "total_tokens": limiter.total_tokens,
            "borrowed_tokens": limiter.borrowed_tokens,
        }
    except Exception as exc:
        logger.info("runtime_metrics_threadpool_snapshot_unavailable err=%s", exc)
        return None


def session_model_calls_stats() -> dict:
    """Proxy baseline for `ai_calls_per_completed_reading` (PR-010 §9 SLO table).

    This is counted per SESSION (messenger_sessions.session_model_calls), not
    per completed reading — a coarser proxy, documented as such in
    backend/docs/baseline_v1.md. It is only meaningful when
    SESSION_V9_PERSIST_ENABLED=1 (PR-002); if that flag is off, the column
    stays 0 for every session and this function will report all-zero stats
    (it still queries — it does not special-case the flag).

    Returns {} on any DB error (e.g. column missing on an unmigrated DB).
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        count(*) FILTER (WHERE session_model_calls > 0) AS sessions_with_calls,
                        avg(session_model_calls) FILTER (WHERE session_model_calls > 0) AS avg_calls,
                        max(session_model_calls) AS max_calls,
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY session_model_calls)
                            FILTER (WHERE session_model_calls > 0) AS p50_calls,
                        percentile_cont(0.95) WITHIN GROUP (ORDER BY session_model_calls)
                            FILTER (WHERE session_model_calls > 0) AS p95_calls
                    FROM messenger_sessions
                    """
                )
                row = cur.fetchone()
        if not row:
            return {}
        return {
            "sessions_with_calls": int(row[0] or 0),
            "avg": float(row[1]) if row[1] is not None else 0.0,
            "max": int(row[2] or 0),
            "p50": float(row[3]) if row[3] is not None else 0.0,
            "p95": float(row[4]) if row[4] is not None else 0.0,
        }
    except Exception as exc:
        logger.warning("runtime_metrics_session_model_calls_stats_failed err=%s", exc)
        return {}


def log_db_snapshot() -> None:
    """Emit DB-backed baseline metrics (no threadpool snapshot).

    No-op (no DB query at all) when RUNTIME_METRICS_ENABLED=0. Each metric
    group has its own try/except so one failing query never blocks the rest.
    """
    if not _enabled():
        return

    try:
        for row in jobs_by_kind_and_status():
            log_metric("jobs_backlog", row["n"], kind=row["kind"], status=row["status"])
    except Exception:
        logger.warning("runtime_metrics_snapshot_jobs_backlog_failed", exc_info=True)

    try:
        log_metric("jobs_stale_running", stale_running_jobs_count())
    except Exception:
        logger.warning("runtime_metrics_snapshot_stale_running_failed", exc_info=True)

    try:
        usage = connection_usage()
        log_metric("db_connections_in_use", usage.get("in_use", 0))
        log_metric("db_connections_max", usage.get("max_connections", 0))
    except Exception:
        logger.warning("runtime_metrics_snapshot_connection_usage_failed", exc_info=True)

    try:
        stats = session_model_calls_stats()
        if stats:
            log_metric("session_model_calls_sessions_with_calls", stats.get("sessions_with_calls", 0))
            log_metric("session_model_calls_avg", stats.get("avg", 0.0))
            log_metric("session_model_calls_max", stats.get("max", 0))
            log_metric("session_model_calls_p50", stats.get("p50", 0.0))
            log_metric("session_model_calls_p95", stats.get("p95", 0.0))
    except Exception:
        logger.warning("runtime_metrics_snapshot_session_model_calls_failed", exc_info=True)


def log_snapshot() -> None:
    """Query every baseline metric and emit one log_metric() line per number.

    No-op (no DB query at all) when RUNTIME_METRICS_ENABLED=0. Each metric
    group has its own try/except so one failing query never blocks the rest.
    """
    if not _enabled():
        return

    log_db_snapshot()

    try:
        tp = threadpool_snapshot()
        if tp is not None:
            log_metric("threadpool_total_tokens", tp.get("total_tokens", 0))
            log_metric("threadpool_borrowed_tokens", tp.get("borrowed_tokens", 0))
    except Exception:
        logger.warning("runtime_metrics_snapshot_threadpool_failed", exc_info=True)


async def runtime_metrics_sampler_loop(interval_sec: float = 60.0) -> None:
    """Periodic sampler task — DB metrics offloaded via to_thread every interval.

    Intended to run as an asyncio task in the web process lifespan (main.py),
    guarded by RUNTIME_METRICS_ENABLED. DB work runs in a worker thread so the
    event loop stays responsive; threadpool metrics are read on the loop
    thread (anyio limiter is not visible from worker threads). A failure
    inside one iteration is logged and swallowed so the loop keeps running;
    asyncio.CancelledError is NOT caught, so the task cancels cleanly on
    shutdown.
    """
    import asyncio

    while True:
        try:
            await asyncio.to_thread(log_db_snapshot)
        except Exception:
            logger.warning("runtime_metrics_sampler_loop_db_snapshot_failed", exc_info=True)

        try:
            tp = threadpool_snapshot()
            if tp is not None:
                log_metric("threadpool_total_tokens", tp.get("total_tokens", 0))
                log_metric("threadpool_borrowed_tokens", tp.get("borrowed_tokens", 0))
        except Exception:
            logger.warning("runtime_metrics_snapshot_threadpool_failed", exc_info=True)

        await asyncio.sleep(interval_sec)
