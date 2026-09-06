"""
Admin V9 routes — Slice 6.
V9-specific admin dashboard: donations, abuse flags, cost, support tickets, jobs, cleanup.
docs/ARCHITECTURE/12_authority_and_evidence.md

Access:
  - All GET routes: any authenticated admin (viewer/operator/admin)
  - POST/action routes: operator or admin role only (_writer_ok)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from psycopg.rows import dict_row

from app.db import get_connection
from app.services.admin_audit_service import write_audit_log
from app.services.admin_session_service import get_session_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-v9"])

ADMIN_COOKIE = "admin_sid"


def _require_user(admin_sid: str | None) -> dict[str, Any] | RedirectResponse:
    u = get_session_user(admin_sid)
    if not u:
        return RedirectResponse(url="/admin/login", status_code=302)
    return u


def _writer_ok(user: dict[str, Any]) -> bool:
    return (user.get("role") or "").lower() in ("admin", "operator")


def _json_resp(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status)


# ── Donation reports ─────────────────────────────────────────────────

@router.get("/admin/v9/donations", response_class=HTMLResponse)
def v9_donations_list(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    status: str = "reported",
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    allowed_statuses = ("reported", "verified", "rejected", "all")
    if status not in allowed_statuses:
        status = "reported"

    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if status == "all":
                    cur.execute(
                        """
                        SELECT id, sender_id, amount_claimed, transfer_note,
                               status, admin_note, verified_by, verified_at, created_at
                        FROM donate_reports ORDER BY created_at DESC LIMIT 200
                        """
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, sender_id, amount_claimed, transfer_note,
                               status, admin_note, verified_by, verified_at, created_at
                        FROM donate_reports WHERE status=%s
                        ORDER BY created_at DESC LIMIT 200
                        """,
                        (status,),
                    )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("v9_donations_list_error")

    return _json_resp({"status_filter": status, "count": len(rows), "rows": rows})


@router.post("/admin/v9/donations/{report_id}/verify")
def v9_donation_verify(
    report_id: int,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    note: str = Form(""),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return _json_resp({"error": "Forbidden — operator or admin role required"}, 403)

    admin_email = u.get("email", "admin")
    from app.services.donate_service import admin_verify_donation
    ok = admin_verify_donation(report_id, admin_email, note)

    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="donation_verified" if ok else "donation_verify_failed",
        resource_type="donate_report",
        resource_id=str(report_id),
        detail={"note": note[:200]},
    )

    if ok:
        return _json_resp({"ok": True, "report_id": report_id})
    return _json_resp({"ok": False, "error": "Report not found or already processed"}, 404)


@router.post("/admin/v9/donations/{report_id}/reject")
def v9_donation_reject(
    report_id: int,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    note: str = Form(""),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return _json_resp({"error": "Forbidden"}, 403)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE donate_reports
                    SET status='rejected', admin_note=%s, updated_at=now()
                    WHERE id=%s AND status='reported'
                    RETURNING id
                    """,
                    (note[:500], report_id),
                )
                row = cur.fetchone()
    except Exception:
        logger.exception("v9_donation_reject_error id=%d", report_id)
        return _json_resp({"error": "DB error"}, 500)

    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="donation_rejected",
        resource_type="donate_report",
        resource_id=str(report_id),
        detail={"note": note[:200]},
    )

    if row:
        return _json_resp({"ok": True, "report_id": report_id})
    return _json_resp({"ok": False, "error": "Report not found or already processed"}, 404)


# ── Abuse flags ──────────────────────────────────────────────────────

@router.get("/admin/v9/abuse")
def v9_abuse_flags(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    flag_type: str = "all",
    resolved: str = "false",
    limit: int = 100,
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    limit = min(limit, 500)
    resolved_bool = resolved.lower() in ("1", "true", "yes")
    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if flag_type == "all":
                    cur.execute(
                        """
                        SELECT id, sender_id, session_id, flag_type, severity, detail, resolved, created_at
                        FROM abuse_flags WHERE resolved=%s
                        ORDER BY created_at DESC LIMIT %s
                        """,
                        (resolved_bool, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, sender_id, session_id, flag_type, severity, detail, resolved, created_at
                        FROM abuse_flags WHERE flag_type=%s AND resolved=%s
                        ORDER BY created_at DESC LIMIT %s
                        """,
                        (flag_type, resolved_bool, limit),
                    )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("v9_abuse_flags_error")

    return _json_resp({"count": len(rows), "filter": {"flag_type": flag_type, "resolved": resolved_bool}, "rows": rows})


@router.post("/admin/v9/abuse/{flag_id}/resolve")
def v9_abuse_resolve(
    flag_id: int,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return _json_resp({"error": "Forbidden"}, 403)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE abuse_flags SET resolved=true WHERE id=%s RETURNING id",
                    (flag_id,),
                )
                row = cur.fetchone()
    except Exception:
        return _json_resp({"error": "DB error"}, 500)

    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="abuse_flag_resolved",
        resource_type="abuse_flag",
        resource_id=str(flag_id),
        detail={},
    )
    return _json_resp({"ok": bool(row), "flag_id": flag_id})


# ── Cost dashboard ───────────────────────────────────────────────────

@router.get("/admin/v9/cost")
def v9_cost_dashboard(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    days: int = 7,
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    days = min(max(days, 1), 90)
    rows: list[dict] = []
    summary: dict = {}
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Daily totals
                cur.execute(
                    """
                    SELECT date_utc,
                           SUM(model_calls) AS total_model_calls,
                           SUM(image_analyzes) AS total_analyzes,
                           SUM(renders) AS total_renders,
                           SUM(cost_vnd) AS total_cost_vnd,
                           COUNT(DISTINCT sender_id) AS unique_users
                    FROM user_daily_counters
                    WHERE date_utc >= CURRENT_DATE - %s
                    GROUP BY date_utc
                    ORDER BY date_utc DESC
                    """,
                    (days,),
                )
                rows = [dict(r) for r in cur.fetchall()]

                # Top spenders today
                cur.execute(
                    """
                    SELECT sender_id, cost_vnd, model_calls, image_analyzes
                    FROM user_daily_counters
                    WHERE date_utc = CURRENT_DATE
                    ORDER BY cost_vnd DESC LIMIT 20
                    """
                )
                top_today = [dict(r) for r in cur.fetchall()]

                # Overall summary
                cur.execute(
                    """
                    SELECT SUM(cost_usd) AS total_cost_usd,
                           COUNT(*) AS total_model_calls
                    FROM cost_ledger
                    WHERE created_at >= now() - (%s || ' days')::interval
                    """,
                    (str(days),),
                )
                sumrow = cur.fetchone()
                summary = dict(sumrow) if sumrow else {}

    except Exception:
        logger.exception("v9_cost_dashboard_error")

    return _json_resp({
        "days": days,
        "daily_rows": rows,
        "top_spenders_today": top_today,
        "ledger_summary": summary,
    })


# ── Support tickets ──────────────────────────────────────────────────

@router.get("/admin/v9/support")
def v9_support_tickets(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    status: str = "open",
    limit: int = 100,
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    limit = min(limit, 500)
    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if status == "all":
                    cur.execute(
                        """
                        SELECT id, sender_id, trigger_type, context_json, status,
                               assigned_to, resolution_note, created_at, updated_at
                        FROM support_tickets ORDER BY created_at DESC LIMIT %s
                        """,
                        (limit,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, sender_id, trigger_type, context_json, status,
                               assigned_to, resolution_note, created_at, updated_at
                        FROM support_tickets WHERE status=%s
                        ORDER BY created_at DESC LIMIT %s
                        """,
                        (status, limit),
                    )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("v9_support_tickets_error")

    return _json_resp({"status_filter": status, "count": len(rows), "rows": rows})


@router.post("/admin/v9/support/{ticket_id}/resolve")
def v9_support_resolve(
    ticket_id: int,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    note: str = Form(""),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return _json_resp({"error": "Forbidden"}, 403)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE support_tickets
                    SET status='resolved', resolution_note=%s,
                        assigned_to=%s, updated_at=now()
                    WHERE id=%s AND status IN ('open', 'in_progress')
                    RETURNING id
                    """,
                    (note[:500], u.get("email", "admin"), ticket_id),
                )
                row = cur.fetchone()
    except Exception:
        return _json_resp({"error": "DB error"}, 500)

    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="support_ticket_resolved",
        resource_type="support_ticket",
        resource_id=str(ticket_id),
        detail={"note": note[:200]},
    )
    return _json_resp({"ok": bool(row), "ticket_id": ticket_id})


# ── Job queue monitor ─────────────────────────────────────────────────

@router.get("/admin/v9/jobs")
def v9_jobs_monitor(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    status: str = "all",
    limit: int = 50,
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    limit = min(limit, 200)
    rows: list[dict] = []
    counts: dict = {}
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Status counts
                cur.execute(
                    "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
                )
                counts = {r["status"]: r["cnt"] for r in cur.fetchall()}

                # Rows
                if status == "all":
                    cur.execute(
                        """
                        SELECT id, kind, status, attempt, max_attempts,
                               created_at, run_at, locked_at, error_message
                        FROM jobs ORDER BY created_at DESC LIMIT %s
                        """,
                        (limit,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, kind, status, attempt, max_attempts,
                               created_at, run_at, locked_at, error_message
                        FROM jobs WHERE status=%s ORDER BY created_at DESC LIMIT %s
                        """,
                        (status, limit),
                    )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("v9_jobs_monitor_error")

    return _json_resp({"status_filter": status, "counts": counts, "rows": rows})


# ── Cleanup trigger ───────────────────────────────────────────────────

@router.post("/admin/v9/cleanup/run")
def v9_cleanup_run(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return _json_resp({"error": "Forbidden"}, 403)

    from app.workers.cleanup_worker import run_all_cleanup
    report = run_all_cleanup()

    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="admin_cleanup_triggered",
        resource_type="system",
        detail={
            "burst_log_deleted": report.burst_log_deleted,
            "assets_deleted": report.assets_deleted,
            "failed_jobs_deleted": report.failed_jobs_deleted,
            "tracking_deleted": report.tracking_events_deleted,
            "errors": report.errors,
        },
    )

    return _json_resp({
        "ok": True,
        "report": {
            "burst_log_deleted": report.burst_log_deleted,
            "assets_deleted": report.assets_deleted,
            "failed_jobs_deleted": report.failed_jobs_deleted,
            "tracking_events_deleted": report.tracking_events_deleted,
            "daily_counters_deleted": report.daily_counters_deleted,
            "errors": report.errors,
            "total_deleted": report.total_deleted,
        },
    })


# ── User lookup ───────────────────────────────────────────────────────

@router.get("/admin/v9/users")
def v9_users_list(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    limit: int = 500,
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    from app.services.cost_enforcer import CAP_TOKENS_DAY_REGULAR

    limit = min(max(limit, 1), 2000)
    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT p.sender_id,
                           COALESCE(c.tokens, 0) AS tokens,
                           COALESCE(c.model_calls, 0) AS model_calls,
                           COALESCE(c.cost_vnd, 0) AS cost_vnd,
                           p.last_seen_at
                    FROM user_profiles p
                    LEFT JOIN user_daily_counters c
                           ON c.sender_id = p.sender_id AND c.date_utc = CURRENT_DATE
                    ORDER BY COALESCE(p.last_activity_at, p.last_seen_at) DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("v9_users_list_error")
        return _json_resp({"error": "DB error"}, 500)

    return _json_resp({
        "scope": "recent",
        "counter_date_utc": "today",
        "cap_tokens_day": CAP_TOKENS_DAY_REGULAR,
        "count": len(rows),
        "rows": rows,
    })


@router.get("/admin/v9/user/{sender_id}")
def v9_user_lookup(
    sender_id: str,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    # Input validation
    if not sender_id or len(sender_id) > 64:
        return _json_resp({"error": "Invalid sender_id"}, 400)

    data: dict[str, Any] = {}
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT state, cohort_label, session_model_calls, abuse_flag_count, admin_granted_combined FROM messenger_sessions WHERE sender_id=%s",
                    (sender_id,),
                )
                session_row = cur.fetchone()

                cur.execute(
                    "SELECT status, amount_claimed, created_at FROM donate_reports WHERE sender_id=%s ORDER BY created_at DESC LIMIT 5",
                    (sender_id,),
                )
                donations = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    "SELECT date_utc, model_calls, cost_vnd, abuse_flags, tokens FROM user_daily_counters WHERE sender_id=%s ORDER BY date_utc DESC LIMIT 7",
                    (sender_id,),
                )
                daily = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    "SELECT flag_type, severity, resolved, created_at FROM abuse_flags WHERE sender_id=%s ORDER BY created_at DESC LIMIT 10",
                    (sender_id,),
                )
                abuse = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    "SELECT metadata_json FROM user_profiles WHERE sender_id=%s",
                    (sender_id,),
                )
                profile_row = cur.fetchone()
                profile_meta = dict(profile_row)["metadata_json"] if profile_row else {}

        data = {
            "sender_id": sender_id,
            "session": dict(session_row) if session_row else None,
            "donations": donations,
            "daily_counters": daily,
            "abuse_flags": abuse,
            "chat_tier": profile_meta.get("chat_tier", "free"),
            "conversation_owner": profile_meta.get("conversation_owner", "bot"),
        }
    except Exception:
        logger.exception("v9_user_lookup_error sender=%s", sender_id[:8])
        return _json_resp({"error": "DB error"}, 500)

    return _json_resp(data)
