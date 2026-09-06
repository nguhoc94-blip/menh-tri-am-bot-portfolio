"""
Postgres-as-queue using SELECT … FOR UPDATE SKIP LOCKED.
Slice 1 · V9.3 §4.3 / docs/ARCHITECTURE/06_queue_worker.md
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass

from app.db import get_connection
from app.utils.sender_hash import hash_sender_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnqueueResult:
    job_id: int | None
    created: bool
    existing_status: str | None
    existing_attempt: int | None
    user_status: str  # new|already_running|retry_pending|dead|completed|enqueue_error


@dataclass(frozen=True)
class RenderEnqueueDecision:
    accepted: bool
    user_status: str
    job_id: int | None = None
    message: str | None = None

    def __bool__(self) -> bool:
        return self.accepted


def lookup_job_by_idempotency_key(idempotency_key: str) -> dict | None:
    """Return existing job row for an idempotency key, if any."""
    if not idempotency_key:
        return None
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, status, attempt, max_attempts
                    FROM jobs
                    WHERE idempotency_key = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (idempotency_key,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "status": row[1],
            "attempt": row[2],
            "max_attempts": row[3],
        }
    except Exception:
        logger.exception("job_lookup_failed ikey=%s", idempotency_key[:16])
        return None


def _status_from_existing(existing: dict) -> str:
    status = existing["status"]
    attempt = int(existing.get("attempt") or 0)
    max_attempts = int(existing.get("max_attempts") or 3)
    if status in ("pending", "running"):
        return "already_running"
    if status == "failed" and attempt < max_attempts:
        return "retry_pending"
    if status == "dead":
        return "dead"
    if status == "completed":
        return "completed"
    return "already_running"


def enqueue_with_status(
    kind: str,
    payload: dict,
    *,
    max_attempts: int = 3,
    idempotency_key: str | None = None,
    run_at_offset_sec: int = 0,
    allow_retry: bool = False,
    retry_token: str | None = None,
) -> EnqueueResult:
    """
    Enqueue with idempotency status inspection.

    When allow_retry=True and retry_token is set, dead jobs get a fresh key:
      `{original_key}:retry:{retry_token}`
    """
    effective_key = idempotency_key
    if allow_retry and retry_token and idempotency_key:
        existing = lookup_job_by_idempotency_key(idempotency_key)
        if existing and existing["status"] == "dead":
            effective_key = f"{idempotency_key}:retry:{retry_token}"

    if effective_key:
        existing = lookup_job_by_idempotency_key(effective_key)
        if existing:
            user_status = _status_from_existing(existing)
            return EnqueueResult(
                job_id=int(existing["id"]),
                created=False,
                existing_status=str(existing["status"]),
                existing_attempt=int(existing.get("attempt") or 0),
                user_status=user_status,
            )

    try:
        job_id = enqueue(
            kind,
            payload,
            max_attempts=max_attempts,
            idempotency_key=effective_key,
            run_at_offset_sec=run_at_offset_sec,
        )
    except Exception:
        logger.exception("enqueue_with_status_failed kind=%s", kind)
        return EnqueueResult(
            job_id=None,
            created=False,
            existing_status=None,
            existing_attempt=None,
            user_status="enqueue_error",
        )

    if job_id is None and effective_key:
        existing = lookup_job_by_idempotency_key(effective_key)
        if existing:
            user_status = _status_from_existing(existing)
            return EnqueueResult(
                job_id=int(existing["id"]),
                created=False,
                existing_status=str(existing["status"]),
                existing_attempt=int(existing.get("attempt") or 0),
                user_status=user_status,
            )
        return EnqueueResult(
            job_id=None,
            created=False,
            existing_status=None,
            existing_attempt=None,
            user_status="enqueue_error",
        )

    return EnqueueResult(
        job_id=job_id,
        created=True,
        existing_status=None,
        existing_attempt=None,
        user_status="new",
    )


def enqueue(
    kind: str,
    payload: dict,
    *,
    max_attempts: int = 3,
    idempotency_key: str | None = None,
    run_at_offset_sec: int = 0,
) -> int | None:
    """
    Insert a job into the queue.

    Returns job_id on success, None if deduplicated (idempotency hit).
    idempotency_key prevents duplicate jobs for the same logical operation.
    """
    ikey = idempotency_key
    if ikey is None:
        ikey = None  # allow duplicates when no key given

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobs (kind, payload, max_attempts, idempotency_key,
                                      run_at)
                    VALUES (%s, %s, %s, %s,
                            now() + (interval '1 second' * %s))
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (kind, json.dumps(payload), max_attempts, ikey, run_at_offset_sec),
                )
                row = cur.fetchone()
        if row is None:
            logger.debug("job_deduplicated kind=%s ikey=%s", kind, ikey)
            return None
        job_id: int = row[0]
        sender_id = payload.get("sender_id", "")
        generation_id = payload.get("generation_id", "")
        logger.info(
            "job_enqueued kind=%s job_id=%s sender_id_hash=%s generation_id=%s",
            kind,
            job_id,
            hash_sender_id(sender_id),
            generation_id,
        )
        return job_id
    except Exception:
        logger.exception("job_enqueue_failed kind=%s", kind)
        raise


def make_idempotency_key(*parts: str) -> str:
    """Deterministic 64-char key from arbitrary string parts."""
    raw = ":".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def claim_next_job(kind: str, worker_id: str, lock_timeout_sec: int = 180) -> dict | None:
    """
    Atomically claim the oldest eligible job of the given kind.
    Returns the job row as a dict, or None if no job available.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs SET
                        status = 'running',
                        attempt = attempt + 1,
                        locked_by = %s,
                        locked_until = now() + (interval '1 second' * %s),
                        updated_at = now()
                    WHERE id = (
                        SELECT id FROM jobs
                        WHERE kind = %s
                          AND status IN ('pending', 'failed')
                          AND attempt < max_attempts
                          AND run_at <= now()
                        ORDER BY run_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING id, kind, payload, attempt, max_attempts
                    """,
                    (worker_id, lock_timeout_sec, kind),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "kind": row[1],
            "payload": row[2],
            "attempt": row[3],
            "max_attempts": row[4],
        }
    except Exception:
        logger.exception("claim_next_job_failed kind=%s", kind)
        return None


def reclaim_stale_jobs(worker_id: str) -> int:
    """
    Reset zombie jobs (status='running' with expired lease) back to 'pending'.
    Returns count of jobs reclaimed. Safe to call from any worker — uses atomic UPDATE.
    Only reclaims jobs where attempt < max_attempts so dead jobs stay dead.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status     = 'pending',
                        locked_by  = NULL,
                        locked_until = NULL,
                        run_at     = now(),
                        updated_at = now()
                    WHERE status = 'running'
                      AND locked_until < now()
                      AND attempt < max_attempts
                    """,
                )
                count = cur.rowcount
        if count:
            logger.info(
                "reaper_reclaimed count=%d worker_id=%s", count, worker_id,
            )
        return count
    except Exception:
        logger.exception("reaper_reclaim_failed worker_id=%s", worker_id)
        return 0


def _backoff_seconds(attempt: int, job_id: int) -> int:
    """Exponential backoff with deterministic per-job jitter. No randomness — fully testable."""
    base = int(os.getenv("WORKER_BACKOFF_BASE_SEC", "15"))
    cap = int(os.getenv("WORKER_BACKOFF_CAP_SEC", "300"))
    delay = min(base * (2 ** (attempt - 1)), cap)
    jitter = job_id % 30  # 0-29s, deterministic from job_id
    return min(delay + jitter, cap)


def complete_job(job_id: int) -> None:
    """Mark a job as successfully completed."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status='completed', outcome='succeeded', updated_at=now() WHERE id=%s",
                (job_id,),
            )
    logger.info("job_completed job_id=%s", job_id)


def fail_job(job_id: int, error: str, attempt: int, max_attempts: int) -> None:
    """
    Mark a job as failed (retryable) or dead (terminal — no more retries).

    Retryable failures advance run_at by an exponential backoff so the job
    is not immediately re-claimable.
    """
    new_status = "dead" if attempt >= max_attempts else "failed"
    backoff = _backoff_seconds(attempt, job_id) if new_status == "failed" else 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            if backoff > 0:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status=%s, last_error=%s, updated_at=now(),
                        run_at = now() + (%s * interval '1 second')
                    WHERE id=%s
                    """,
                    (new_status, error[:2000], backoff, job_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status=%s, last_error=%s, updated_at=now()
                    WHERE id=%s
                    """,
                    (new_status, error[:2000], job_id),
                )
    logger.warning("job_failed job_id=%s status=%s backoff_sec=%d error=%s",
                   job_id, new_status, backoff, error[:200])


def fail_job_terminal(job_id: int, error: str) -> None:
    """Mark a job dead with outcome=failed_terminal. Never retried."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status='dead', outcome='failed_terminal', last_error=%s, updated_at=now()
                WHERE id=%s
                """,
                (error[:2000], job_id),
            )
    logger.error("job_terminal job_id=%s error=%s", job_id, error[:200])


def cancel_job(job_id: int, reason: str = "stale") -> None:
    """Mark a job cancelled due to being superseded by a newer generation."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status='cancelled', outcome='cancelled_stale',
                    last_error=%s, updated_at=now()
                WHERE id=%s
                """,
                (f"cancelled: {reason}"[:2000], job_id),
            )
    logger.info("job_cancelled job_id=%s reason=%s", job_id, reason)
