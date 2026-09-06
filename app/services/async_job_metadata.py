"""Session routing metadata for async analyze/render/send jobs."""

from __future__ import annotations

from typing import Any

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"


def _validate_positive_job_id(job_id: int) -> None:
    """Require a positive integer queue ID."""
    if not isinstance(job_id, int) or job_id <= 0:
        raise ValueError("pending_job requires a positive integer job_id")


def _is_positive_job_id(job_id: int) -> bool:
    return isinstance(job_id, int) and job_id > 0


def _routing(session) -> dict[str, Any]:
    r = session.routing
    return dict(r) if isinstance(r, dict) else {}


def set_pending_job(
    session,
    *,
    kind: str,
    job_id: int,
    generation_id: str,
    status: str = JOB_PENDING,
) -> None:
    """Record an in-flight async job. job_id must be a positive queue ID."""
    _validate_positive_job_id(job_id)
    routing = _routing(session)
    routing["pending_job"] = {
        "kind": kind,
        "job_id": job_id,
        "generation_id": generation_id,
        "status": status,
    }
    routing["job_status"] = status
    session.routing = routing


def update_pending_job_stage(
    session,
    *,
    kind: str,
    job_id: int,
    generation_id: str,
    status: str,
) -> bool:
    """Advance pending_job if generation still matches. Returns False when stale."""
    routing = _routing(session)
    current = routing.get("pending_job")
    if isinstance(current, dict):
        cur_gen = current.get("generation_id")
        if cur_gen and cur_gen != generation_id:
            return False
    if not _is_positive_job_id(job_id):
        return False
    routing["pending_job"] = {
        "kind": kind,
        "job_id": job_id,
        "generation_id": generation_id,
        "status": status,
    }
    routing["job_status"] = status
    session.routing = routing
    return True


def mark_job_running(session, *, generation_id: str) -> bool:
    routing = _routing(session)
    pending = routing.get("pending_job")
    if not isinstance(pending, dict):
        return False
    if pending.get("generation_id") and pending["generation_id"] != generation_id:
        return False
    pending = {**pending, "status": JOB_RUNNING}
    routing["pending_job"] = pending
    routing["job_status"] = JOB_RUNNING
    session.routing = routing
    return True


def clear_pending_job_success(session) -> None:
    routing = _routing(session)
    routing.pop("pending_job", None)
    routing["job_status"] = JOB_COMPLETED
    session.routing = routing


def clear_pending_job_failure(session, *, error_code: str) -> None:
    routing = _routing(session)
    routing.pop("pending_job", None)
    routing["job_status"] = JOB_FAILED
    routing["last_delivery_error"] = error_code
    session.routing = routing


def clear_pending_job_only(session) -> None:
    routing = _routing(session)
    routing.pop("pending_job", None)
    session.routing = routing
