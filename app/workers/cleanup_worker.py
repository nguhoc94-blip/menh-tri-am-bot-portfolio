"""
Cleanup worker — Slice 6.
Scheduled cleanup of stale data per retention policy.
docs/ARCHITECTURE/07_storage_and_privacy.md

Tasks (run in order, each independent):
  1. Purge message_burst_log older than 1 hour (spam detection data)
  2. Purge old input assets older than ASSET_INPUT_RETENTION_DAYS (default 7)
  3. Purge expired admin sessions (already handled by purge_expired_sessions, call here too)
  4. Purge stale jobs (failed jobs older than FAILED_JOB_RETENTION_DAYS, default 30)
  5. Purge stale tracking_events (older than TRACKING_RETENTION_DAYS, default 90)

This module exposes:
  - run_all_cleanup() -> CleanupReport  (call from handler or cron)
  - Individual task functions for testing
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _int_env(key: str, default: int) -> int:
    try:
        return int((os.environ.get(key) or "").strip() or default)
    except ValueError:
        return default


BURST_LOG_RETENTION_MINUTES = _int_env("BURST_LOG_RETENTION_MINUTES", 60)
ASSET_INPUT_RETENTION_DAYS = _int_env("ASSET_INPUT_RETENTION_DAYS", 7)
FAILED_JOB_RETENTION_DAYS = _int_env("FAILED_JOB_RETENTION_DAYS", 30)
TRACKING_RETENTION_DAYS = _int_env("TRACKING_RETENTION_DAYS", 90)
DAILY_COUNTER_RETENTION_DAYS = _int_env("DAILY_COUNTER_RETENTION_DAYS", 90)


@dataclass
class CleanupReport:
    burst_log_deleted: int = 0
    assets_deleted: int = 0
    failed_jobs_deleted: int = 0
    tracking_events_deleted: int = 0
    daily_counters_deleted: int = 0
    admin_sessions_expired: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_deleted(self) -> int:
        return (
            self.burst_log_deleted + self.assets_deleted +
            self.failed_jobs_deleted + self.tracking_events_deleted +
            self.daily_counters_deleted + self.admin_sessions_expired
        )


def _purge_burst_log(conn) -> int:
    """Delete message_burst_log entries older than retention window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=BURST_LOG_RETENTION_MINUTES)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM message_burst_log WHERE ts < %s",
            (cutoff,),
        )
        return cur.rowcount or 0


def _purge_old_input_assets(conn) -> int:
    """
    Delete input assets (palm/face images) older than retention policy.
    Only deletes DB record — physical file cleanup is responsibility of
    storage backend (Disk/R2 lifecycle policy).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=ASSET_INPUT_RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM assets
            WHERE asset_type IN ('palm_input', 'face_input')
              AND created_at < %s
            """,
            (cutoff,),
        )
        return cur.rowcount or 0


def _purge_failed_jobs(conn) -> int:
    """Delete failed jobs older than retention period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=FAILED_JOB_RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM jobs
            WHERE status = 'failed' AND created_at < %s
            """,
            (cutoff,),
        )
        return cur.rowcount or 0


def _purge_old_tracking_events(conn) -> int:
    """Delete tracking_events older than retention period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRACKING_RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM tracking_events WHERE created_at < %s",
            (cutoff,),
        )
        return cur.rowcount or 0


def _purge_old_daily_counters(conn) -> int:
    """Delete user_daily_counters older than retention period."""
    from datetime import date
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=DAILY_COUNTER_RETENTION_DAYS)).date()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_daily_counters WHERE date_utc < %s",
            (cutoff_date,),
        )
        return cur.rowcount or 0


def run_all_cleanup() -> CleanupReport:
    """
    Run all cleanup tasks in a single DB connection.
    Each task is isolated — failures don't block subsequent tasks.
    Returns a CleanupReport with counts and any errors.
    """
    from app.db import get_connection
    report = CleanupReport()

    tasks = [
        ("burst_log", _purge_burst_log, "burst_log_deleted"),
        ("old_input_assets", _purge_old_input_assets, "assets_deleted"),
        ("failed_jobs", _purge_failed_jobs, "failed_jobs_deleted"),
        ("tracking_events", _purge_old_tracking_events, "tracking_events_deleted"),
        ("daily_counters", _purge_old_daily_counters, "daily_counters_deleted"),
    ]

    try:
        with get_connection() as conn:
            for name, fn, attr in tasks:
                try:
                    count = fn(conn)
                    setattr(report, attr, count)
                    logger.info("cleanup_task_done task=%s deleted=%d", name, count)
                except Exception as exc:
                    msg = f"{name}: {exc}"
                    report.errors.append(msg)
                    logger.exception("cleanup_task_error task=%s", name)
    except Exception as exc:
        report.errors.append(f"db_connect: {exc}")
        logger.exception("cleanup_db_connect_error")

    # Admin sessions (uses its own connection internally)
    try:
        from app.services.admin_session_service import purge_expired_sessions
        purge_expired_sessions()
        report.admin_sessions_expired = -1  # ran, count not exposed by that API
    except Exception as exc:
        report.errors.append(f"admin_sessions: {exc}")

    logger.info(
        "cleanup_complete total_deleted=%d errors=%d",
        report.total_deleted, len(report.errors),
    )
    return report
