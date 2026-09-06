"""
Admin HTML baseline — form login + HttpOnly cookie session (Nhịp 2).
Không preview dynamic free result; chỉ config/campaign/transcript/export tĩnh.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from app.db import get_connection
from app.services.admin_audit_service import write_audit_log
from app.services.admin_password import verify_password
from app.services.customer_data_reset import reset_all_customer_data
from app.services.db_storage_stats import get_db_storage_stats
from app.services.data_subject_service import anonymize_sender_baseline
from app.services.admin_session_service import (
    create_session,
    delete_session,
    get_session_user,
    purge_expired_sessions,
    sessions_summary_json,
)
from app.services.webhook_dedupe_cleanup import run_webhook_dedupe_retention_cleanup

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

ADMIN_COOKIE = "admin_sid"


def _cookie_secure() -> bool:
    return (os.environ.get("ADMIN_COOKIE_SECURE") or "").strip().lower() in ("1", "true", "yes")


def _writer_ok(user: dict[str, Any]) -> bool:
    r = (user.get("role") or "").lower()
    return r in ("admin", "operator")


def _page_owner_ok(user: dict[str, Any]) -> bool:
    r = (user.get("role") or "").lower()
    return r in ("admin", "operator", "page_owner")


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request) -> Any:
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/admin/login")
def admin_login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    purge_expired_sessions()
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, role, is_active
                    FROM admin_users WHERE LOWER(email) = LOWER(%s)
                    """,
                    (email.strip(),),
                )
                row = cur.fetchone()
    except Exception:
        logger.exception("admin_login_db_error")
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Hệ thống đang bận."},
            status_code=503,
        )

    if not row or not row.get("is_active"):
        write_audit_log(
            admin_user_id=None,
            action="admin_login_failed",
            resource_type="admin_user",
            detail={"email": email.strip()},
        )
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Sai email hoặc mật khẩu."},
            status_code=401,
        )

    if not verify_password(password, str(row["password_hash"])):
        write_audit_log(
            admin_user_id=int(row["id"]),
            action="admin_login_failed",
            resource_type="admin_user",
            resource_id=str(row["id"]),
            detail={"reason": "bad_password"},
        )
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Sai email hoặc mật khẩu."},
            status_code=401,
        )

    sid = create_session(admin_user_id=int(row["id"]))
    write_audit_log(
        admin_user_id=int(row["id"]),
        action="admin_login_ok",
        resource_type="admin_user",
        resource_id=str(row["id"]),
        detail={},
    )
    resp = RedirectResponse(url="/admin/dashboard", status_code=302)
    resp.set_cookie(
        ADMIN_COOKIE,
        sid,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=86400 * 7,
        path="/",
    )
    return resp


@router.post("/admin/logout")
def admin_logout(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Response:
    if admin_sid:
        delete_session(admin_sid)
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie(ADMIN_COOKIE, path="/")
    return resp


def _require_user(admin_sid: str | None) -> dict[str, Any] | RedirectResponse:
    u = get_session_user(admin_sid)
    if not u:
        return RedirectResponse(url="/admin/login", status_code=302)
    return u


@router.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE)) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    counts: dict[str, Any] = {}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM messenger_sessions")
                counts["sessions"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM readings")
                counts["readings"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM app_config")
                counts["config_keys"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM campaigns")
                counts["campaigns"] = cur.fetchone()[0]
    except Exception:
        logger.exception("admin_dashboard_counts")
        counts = {"sessions": "?", "readings": "?", "config_keys": "?", "campaigns": "?"}
    return templates.TemplateResponse(request, "admin/dashboard.html", {"user": u, "counts": counts})


@router.get("/admin/config", response_class=HTMLResponse)
def admin_config_list(request: Request, admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE)) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    rows: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT config_key, updated_at,
                           (draft_value IS NOT NULL) AS has_draft
                    FROM app_config ORDER BY config_key
                    """
                )
                rows = [dict(x) for x in cur.fetchall()]
    except Exception:
        logger.exception("admin_config_list")
    return templates.TemplateResponse(request, "admin/config_list.html", {"user": u, "rows": rows})


@router.get("/admin/config/{key}/edit", response_class=HTMLResponse)
def admin_config_edit(
    request: Request,
    key: str,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    row: dict[str, Any] | None = None
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT config_key, config_value, draft_value, updated_at
                    FROM app_config WHERE config_key = %s
                    """,
                    (key,),
                )
                row = cur.fetchone()
    except Exception:
        logger.exception("admin_config_edit")
    if not row:
        return HTMLResponse("Không tìm thấy key", status_code=404)
    draft_raw = row.get("draft_value") or row.get("config_value")
    draft_txt = json.dumps(draft_raw, ensure_ascii=False, indent=2) if draft_raw else "{}"
    pub_txt = json.dumps(row.get("config_value") or {}, ensure_ascii=False, indent=2)
    return templates.TemplateResponse(
        request,
        "admin/config_edit.html",
        {
            "user": u,
            "key": key,
            "published_json": pub_txt,
            "draft_json": draft_txt,
            "can_write": _writer_ok(u),
        },
    )


@router.post("/admin/config/{key}/draft")
def admin_config_save_draft(
    request: Request,
    key: str,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    body: str = Form(..., alias="draft_json"),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return HTMLResponse("Forbidden (viewer)", status_code=403)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return RedirectResponse(url=f"/admin/config/{key}/edit?err=json", status_code=302)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app_config SET draft_value = %s::jsonb, updated_at = NOW()
                    WHERE config_key = %s
                    """,
                    (json.dumps(parsed), key),
                )
        write_audit_log(
            admin_user_id=int(u["user_id"]),
            action="app_config_draft_saved",
            resource_type="app_config",
            resource_id=key,
            detail={},
        )
    except Exception:
        logger.exception("admin_config_draft_save")
        return HTMLResponse("Lỗi lưu", status_code=500)
    return RedirectResponse(url="/admin/config", status_code=302)


@router.post("/admin/config/{key}/publish")
def admin_config_publish(
    key: str,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Response:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return HTMLResponse("Forbidden (viewer)", status_code=403)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app_config
                    SET config_value = COALESCE(draft_value, config_value),
                        draft_value = NULL,
                        updated_at = NOW()
                    WHERE config_key = %s
                    """,
                    (key,),
                )
        write_audit_log(
            admin_user_id=int(u["user_id"]),
            action="app_config_published",
            resource_type="app_config",
            resource_id=key,
            detail={},
        )
    except Exception:
        logger.exception("admin_config_publish")
        return HTMLResponse("Lỗi publish", status_code=500)
    return RedirectResponse(url="/admin/config", status_code=302)


@router.get("/admin/campaigns", response_class=HTMLResponse)
def admin_campaigns(request: Request, admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE)) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    rows: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, name, external_ref, updated_at,
                           (draft_config_json IS NOT NULL) AS has_draft
                    FROM campaigns ORDER BY id DESC
                    """
                )
                rows = [dict(x) for x in cur.fetchall()]
    except Exception:
        logger.exception("admin_campaigns")
    return templates.TemplateResponse(request, "admin/campaigns.html", {"user": u, "rows": rows})


@router.get("/admin/transcript", response_class=HTMLResponse)
def admin_transcript(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    sender_id: str = "",
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    timeline: list[dict[str, Any]] = []
    conversation_owner = ""
    chat_tier = ""
    token_events: list[dict[str, Any]] = []
    session_summary: dict[str, Any] | None = None
    sender_jobs: list[dict[str, Any]] = []
    has_ops_data = False
    if sender_id.strip():
        sid = sender_id.strip()
        try:
            from app.services.admin_conversation_view import build_admin_transcript_context
            from app.services.chat_tier import get_chat_tier
            from app.services.conversation_owner import get_conversation_owner

            ctx = build_admin_transcript_context(sid)
            timeline = ctx["timeline"]
            session_summary = ctx.get("session_summary")
            sender_jobs = ctx.get("jobs") or []
            has_ops_data = bool(ctx.get("has_ops_data"))
            conversation_owner = get_conversation_owner(sid).value
            chat_tier = get_chat_tier(sid).value
        except Exception:
            logger.exception("admin_transcript")
        try:
            with get_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        SELECT event_type, request_id, payload_json, created_at
                        FROM funnel_events
                        WHERE sender_id = %s
                          AND event_type IN ('openai_token_usage', 'turn_cost_summary')
                        ORDER BY created_at DESC LIMIT 30
                        """,
                        (sid,),
                    )
                    token_events = [dict(r) for r in cur.fetchall()]
        except Exception:
            logger.exception("admin_transcript_token_events")
    return templates.TemplateResponse(
        request,
        "admin/transcript.html",
        {
            "user": u,
            "sender_id": sender_id,
            "timeline": timeline,
            "conversation_owner": conversation_owner,
            "chat_tier": chat_tier,
            "token_events": token_events,
            "session_summary": session_summary,
            "sender_jobs": sender_jobs,
            "has_ops_data": has_ops_data,
            "can_page_send": _page_owner_ok(u),
            "can_recovery": _writer_ok(u),
            "can_job_cancel": _writer_ok(u),
            "recovery_msg": request.query_params.get("recovery_msg", ""),
        },
    )


@router.post("/admin/recovery/bundle")
def admin_recovery_bundle(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    sender_id: str = Form(...),
) -> Any:
    """Re-enqueue generate_overview_bundle for a stuck/cancelled premium delivery."""
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return HTMLResponse("Forbidden — operator/admin role required", status_code=403)

    sid = (sender_id or "").strip()
    if not sid:
        return RedirectResponse(url="/admin/transcript", status_code=303)

    result_msg = ""
    try:
        from app.db import get_connection as _get_conn
        from app.services.messenger_state_db import DbMessengerStateStore
        from app.services.async_job_metadata import JOB_PENDING, set_pending_job
        from app.workers.queue import enqueue_with_status
        from psycopg.rows import dict_row as _dict_row
        import uuid as _uuid

        store = DbMessengerStateStore()
        session = store.get_or_create(sid)
        if not session:
            result_msg = "❌ Không tìm được session"
        elif not session.chart_json:
            result_msg = "❌ Session chưa có chart_json — không thể generate bundle"
        else:
            routing = session.routing if isinstance(session.routing, dict) else {}
            ob = routing.get("overview_bundle") or {}
            generation_id = ob.get("generation_id") or str(session.generation_id)
            tier = ob.get("tier") or "premium"

            # Verify interpretation is completed (required for premium)
            interp_ready = True
            if tier == "premium":
                with _get_conn() as conn:
                    with conn.cursor(row_factory=_dict_row) as cur:
                        cur.execute(
                            "SELECT status FROM premium_interpretations "
                            "WHERE sender_id=%s AND generation_id=%s LIMIT 1",
                            (sid, generation_id),
                        )
                        row = cur.fetchone()
                if not row or row["status"] != "completed":
                    interp_ready = False
                    result_msg = (
                        f"❌ premium_interpretation chưa completed "
                        f"(status={row['status'] if row else 'not found'}) — "
                        f"cần chạy compile trước"
                    )

            if interp_ready:
                request_id = f"admin-recovery-{_uuid.uuid4().hex[:8]}"
                bundle_id = ob.get("bundle_id") or f"{generation_id}:{tier}"
                # Fresh idempotency key so old cancelled/dead job does not block recovery.
                idem_key = (
                    f"overview_bundle:{sid}:{generation_id}:{tier}:admin_recovery:{request_id}"
                )
                payload = {
                    "sender_id": sid,
                    "generation_id": generation_id,
                    "request_id": request_id,
                    "tier": tier,
                    "bundle_id": bundle_id,
                    "source": "admin_recovery",
                }
                if not isinstance(session.routing, dict):
                    session.routing = {}
                session.routing["overview_bundle"] = {
                    "tier": tier,
                    "source": "admin_recovery",
                    "generation_id": generation_id,
                    "bundle_id": bundle_id,
                    "status": "pending",
                }
                enqueue_result = enqueue_with_status(
                    "generate_overview_bundle",
                    payload=payload,
                    idempotency_key=idem_key,
                )
                if enqueue_result.job_id and enqueue_result.job_id > 0:
                    set_pending_job(
                        session,
                        kind="generate_overview_bundle",
                        job_id=enqueue_result.job_id,
                        generation_id=generation_id,
                        status=JOB_PENDING,
                    )
                # Reset pipeline stage to show bundle is pending again
                from app.services.delivery_progress import STAGE_WAIT, set_pipeline_stage
                set_pipeline_stage(
                    session, "bundle", STAGE_WAIT,
                    generation_id=generation_id, allow_init=True,
                )
                store.save(session)
                result_msg = (
                    f"✅ Đã enqueue generate_overview_bundle "
                    f"(tier={tier}, gen={generation_id[:8]}…, "
                    f"job_id={enqueue_result.job_id}, status={enqueue_result.user_status})"
                )
                write_audit_log(
                    admin_user_id=int(u["user_id"]),
                    action="admin_recovery_bundle",
                    resource_type="messenger_session",
                    resource_id=sid,
                    detail={
                        "generation_id": generation_id,
                        "tier": tier,
                        "job_id": enqueue_result.job_id,
                        "user_status": enqueue_result.user_status,
                        "admin_email": u.get("email"),
                    },
                )
    except Exception as exc:
        logger.exception("admin_recovery_bundle sender_id=%s", sid)
        result_msg = f"❌ Lỗi: {exc}"

    return RedirectResponse(
        url=f"/admin/transcript?sender_id={sid}&recovery_msg={result_msg}",
        status_code=303,
    )


@router.post("/admin/jobs/cancel")
def admin_cancel_job(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    job_id: str = Form(...),
    sender_id: str = Form(""),
) -> Any:
    """Cancel a pending/failed worker job (e.g. duplicate recovery enqueue)."""
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return HTMLResponse("Forbidden — operator/admin role required", status_code=403)

    sid = (sender_id or "").strip()
    redirect_base = f"/admin/transcript?sender_id={sid}" if sid else "/admin/transcript"
    result_msg = ""

    try:
        jid = int((job_id or "").strip())
    except ValueError:
        result_msg = "❌ job_id không hợp lệ"
        return RedirectResponse(url=f"{redirect_base}&recovery_msg={result_msg}", status_code=303)

    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id, kind, status, payload FROM jobs WHERE id=%s",
                    (jid,),
                )
                row = cur.fetchone()

        if not row:
            result_msg = f"❌ Không tìm thấy job #{jid}"
        elif row["status"] not in ("pending", "failed"):
            result_msg = (
                f"❌ Job #{jid} đang `{row['status']}` — "
                f"chỉ hủy được pending/failed (running phải để chạy xong)"
            )
        else:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            job_sender = str(payload.get("sender_id") or "")
            if sid and job_sender and job_sender != sid:
                result_msg = f"❌ Job #{jid} không thuộc sender {sid}"
            else:
                from app.workers.queue import cancel_job

                cancel_job(jid, reason="admin_cancel")
                write_audit_log(
                    admin_user_id=int(u["user_id"]),
                    action="admin_cancel_job",
                    resource_type="job",
                    resource_id=str(jid),
                    detail={
                        "kind": row["kind"],
                        "previous_status": row["status"],
                        "sender_id": job_sender or sid,
                        "admin_email": u.get("email"),
                    },
                )
                result_msg = f"✅ Đã hủy job #{jid} ({row['kind']}, was {row['status']})"
    except Exception as exc:
        logger.exception("admin_cancel_job job_id=%s", jid)
        result_msg = f"❌ Lỗi: {exc}"

    return RedirectResponse(
        url=f"{redirect_base}&recovery_msg={result_msg}",
        status_code=303,
    )


@router.post("/admin/page-send")
def admin_page_send(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    sender_id: str = Form(...),
    text: str = Form(...),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _page_owner_ok(u):
        return HTMLResponse("Forbidden — page_owner role required", status_code=403)
    sid = (sender_id or "").strip()
    body = (text or "").strip()
    if not sid or not body:
        return RedirectResponse(
            url=f"/admin/transcript?sender_id={sid}",
            status_code=303,
        )
    import uuid

    from app.services.assistant_takeover import try_handle_assistant_return_command
    from app.services.messenger_handler import send_page_owner_message
    from app.services.outbound_audit import OUTCOME_SENT_OK
    from app.services.page_owner_premium_grant import try_handle_page_owner_premium_grant

    request_id = str(uuid.uuid4())
    try:
        result = send_page_owner_message(sid, body, request_id=request_id)
        if result.outcome != OUTCOME_SENT_OK:
            logger.warning(
                "admin_page_send_failed sender_id=%s outcome=%s",
                sid,
                result.outcome,
            )
        else:
            try_handle_assistant_return_command(sid, body, request_id=request_id)
            try_handle_page_owner_premium_grant(sid, body, request_id=request_id)
        write_audit_log(
            admin_user_id=int(u["user_id"]),
            action="page_owner_send",
            resource_type="sender",
            resource_id=sid,
            detail={"outcome": result.outcome, "len": len(body)},
        )
    except Exception:
        logger.exception("admin_page_send_error sender_id=%s", sid)
    return RedirectResponse(url=f"/admin/transcript?sender_id={sid}", status_code=303)


@router.get("/admin/asset-image/{asset_id}")
def admin_asset_image(
    asset_id: str,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    from fastapi.responses import Response

    from app.media.storage import StorageError, get_storage_backend

    if not asset_id or ".." in asset_id or "/" in asset_id:
        return HTMLResponse("invalid asset_id", status_code=400)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT storage_key, mime_type
                    FROM assets
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                row = cur.fetchone()
    except Exception:
        logger.exception("admin_asset_image_db_failed asset_id=%s", asset_id)
        return HTMLResponse("db error", status_code=500)
    if not row:
        return HTMLResponse("not found", status_code=404)
    storage_key, mime_type = row[0], row[1] or "application/octet-stream"
    if not storage_key:
        return HTMLResponse("no storage_key", status_code=404)
    backend = get_storage_backend()
    try:
        raw = backend.read_bytes(storage_key)
    except (AttributeError, StorageError):
        return HTMLResponse("asset unreadable", status_code=404)
    if not raw:
        return HTMLResponse("empty asset", status_code=404)
    return Response(content=raw, media_type=mime_type)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_list(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    limit: int = 500,
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    from app.services.cost_enforcer import CAP_TOKENS_DAY_REGULAR

    limit = min(max(limit, 1), 2000)
    rows: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT p.sender_id,
                           COALESCE(c.tokens, 0) AS tokens,
                           COALESCE(c.model_calls, 0) AS model_calls,
                           COALESCE(c.cost_vnd, 0) AS cost_vnd,
                           p.last_seen_at,
                           COALESCE(p.metadata_json->>'chat_tier', 'free') AS chat_tier
                    FROM user_profiles p
                    LEFT JOIN user_daily_counters c
                           ON c.sender_id = p.sender_id AND c.date_utc = CURRENT_DATE
                    ORDER BY COALESCE(p.last_activity_at, p.last_seen_at) DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(x) for x in cur.fetchall()]
    except Exception:
        logger.exception("admin_users_list")
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            "user": u,
            "rows": rows,
            "cap_tokens_day": CAP_TOKENS_DAY_REGULAR,
            "limit": limit,
        },
    )


@router.get("/admin/ops", response_class=HTMLResponse)
def admin_ops(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    request_id: str = "",
    days: int = 7,
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u

    git_sha = os.environ.get("GIT_SHA") or os.environ.get("RENDER_GIT_COMMIT") or "—"
    build_time = os.environ.get("BUILD_TIME") or "—"
    system: dict[str, Any] = {"git_sha": git_sha, "build_time": build_time, "db_ok": False}
    watchlist: list[dict[str, Any]] = []
    job_counts: dict[str, int] = {}
    jobs: list[dict[str, Any]] = []
    cost_today: dict[str, Any] = {}
    cost_top: list[dict[str, Any]] = []
    trace_events: list[dict[str, Any]] = []

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                system["db_ok"] = True
    except Exception:
        logger.exception("admin_ops_db_ping")

    try:
        from app.services.chat_tier import get_chat_tier
        from app.services.conversation_owner import get_conversation_owner

        psids = [p.strip() for p in os.environ.get("OPS_WATCHLIST_PSIDS", "").split(",") if p.strip()]
        for psid in psids:
            watchlist.append(
                {
                    "sender_id": psid,
                    "tier": get_chat_tier(psid).value,
                    "owner": get_conversation_owner(psid).value,
                }
            )
    except Exception:
        logger.exception("admin_ops_watchlist")

    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status")
                job_counts = {str(r["status"]): int(r["cnt"]) for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT id, kind, status, attempt, created_at, run_at, locked_at, error_message
                    FROM jobs ORDER BY created_at DESC LIMIT 20
                    """
                )
                jobs = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """
                    SELECT SUM(tokens) AS tokens,
                           SUM(cost_vnd) AS cost_vnd,
                           SUM(model_calls) AS model_calls,
                           COUNT(DISTINCT sender_id) AS unique_users
                    FROM user_daily_counters
                    WHERE date_utc = CURRENT_DATE
                    """
                )
                cost_row = cur.fetchone()
                cost_today = dict(cost_row) if cost_row else {}

                cur.execute(
                    """
                    SELECT sender_id, tokens, cost_vnd
                    FROM user_daily_counters
                    WHERE date_utc = CURRENT_DATE
                    ORDER BY cost_vnd DESC LIMIT 10
                    """
                )
                cost_top = [dict(r) for r in cur.fetchall()]

                rid = request_id.strip()
                if rid:
                    cur.execute(
                        """
                        SELECT event_type, payload_json, created_at
                        FROM funnel_events
                        WHERE request_id = %s
                          AND event_type IN ('openai_token_usage', 'turn_cost_summary')
                        ORDER BY created_at
                        """,
                        (rid,),
                    )
                    trace_events = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("admin_ops_queries")

    return templates.TemplateResponse(
        request,
        "admin/ops.html",
        {
            "user": u,
            "system": system,
            "watchlist": watchlist,
            "job_counts": job_counts,
            "jobs": jobs,
            "cost_today": cost_today,
            "cost_top": cost_top,
            "trace_events": trace_events,
            "request_id": request_id,
            "days": days,
        },
    )


def _privacy_page_context(
    user: dict[str, Any],
    *,
    message: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    storage_stats: dict[str, Any] | None = None
    storage_error: str | None = None
    try:
        storage_stats = get_db_storage_stats()
    except Exception:
        logger.exception("admin_privacy_storage_stats_failed")
        storage_error = "Không đọc được thống kê DB."
    return {
        "user": user,
        "message": message,
        "error": error,
        "storage_stats": storage_stats,
        "storage_error": storage_error,
    }


@router.get("/admin/privacy", response_class=HTMLResponse)
def admin_privacy_form(
    request: Request, admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE)
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    return templates.TemplateResponse(
        request,
        "admin/privacy.html",
        _privacy_page_context(u),
    )


@router.get("/admin/v9/db-storage")
def admin_db_storage_json(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    try:
        return get_db_storage_stats()
    except Exception:
        logger.exception("admin_db_storage_json_failed")
        return PlainTextResponse("DB stats unavailable", status_code=503)


@router.post("/admin/privacy/anonymize")
def admin_privacy_anonymize(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    sender_id: str = Form(...),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return HTMLResponse("Forbidden (viewer)", status_code=403)
    sid = (sender_id or "").strip()
    if not sid:
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
        _privacy_page_context(u, error="sender_id trống."),
            status_code=400,
        )
    try:
        summary = anonymize_sender_baseline(sid)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
        _privacy_page_context(u, error=str(e)),
            status_code=400,
        )
    except Exception:
        logger.exception("admin_anonymize_failed sender_id=%s", sid)
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
        _privacy_page_context(u, error="Lỗi hệ thống khi ẩn danh."),
            status_code=500,
        )
    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="data_subject_anonymized",
        resource_type="sender",
        resource_id=sid,
        detail=summary,
    )
    return templates.TemplateResponse(
        request,
        "admin/privacy.html",
        _privacy_page_context(u, message=f"Đã ẩn danh baseline cho sender_id={sid}."),
    )


@router.post("/admin/maintenance/webhook-dedupe-cleanup")
def admin_webhook_dedupe_cleanup(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if not _writer_ok(u):
        return HTMLResponse("Forbidden (viewer)", status_code=403)
    try:
        deleted = run_webhook_dedupe_retention_cleanup()
    except Exception:
        logger.exception("admin_dedupe_cleanup_failed")
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
        _privacy_page_context(u, error="Cleanup dedupe thất bại."),
            status_code=500,
        )
    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="webhook_dedupe_cleanup",
        resource_type="maintenance",
        detail={"rows_deleted": deleted, "retention_hours": 24},
    )
    return templates.TemplateResponse(
        request,
        "admin/privacy.html",
        _privacy_page_context(
            u,
            message=f"Đã chạy cleanup webhook_dedupe (24h): xóa {deleted} dòng.",
        ),
    )


@router.post("/admin/maintenance/reset-all-customers")
def admin_reset_all_customers(
    request: Request,
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
    confirm: str = Form(""),
) -> Any:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return u
    if (u.get("role") or "").lower() != "admin":
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
            _privacy_page_context(u, error="Chỉ role admin mới được reset toàn bộ khách."),
            status_code=403,
        )
    if (confirm or "").strip() != "YES DELETE ALL":
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
            _privacy_page_context(
                u,
                error="Xác nhận không đúng. Gõ chính xác: YES DELETE ALL",
            ),
            status_code=400,
        )
    try:
        summary = reset_all_customer_data()
    except Exception:
        logger.exception("admin_reset_all_customers_failed")
        return templates.TemplateResponse(
            request,
            "admin/privacy.html",
            _privacy_page_context(u, error="Reset thất bại — xem log server."),
            status_code=500,
        )
    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="reset_all_customer_data",
        resource_type="maintenance",
        detail=summary,
    )
    counts = summary.get("counts") or {}
    return templates.TemplateResponse(
        request,
        "admin/privacy.html",
        _privacy_page_context(
            u,
            message=(
                "Đã reset sạch dữ liệu khách. "
                f"sessions={counts.get('messenger_sessions', '?')}, "
                f"readings={counts.get('readings', '?')}. "
                f"Truncated {len(summary.get('truncated') or [])} bảng."
            ),
        ),
    )


@router.get("/admin/export/sessions.json")
def admin_export_sessions(
    admin_sid: str | None = Cookie(None, alias=ADMIN_COOKIE),
) -> PlainTextResponse:
    u = _require_user(admin_sid)
    if isinstance(u, RedirectResponse):
        return PlainTextResponse("Unauthorized", status_code=401)
    if not _writer_ok(u):
        return PlainTextResponse("Forbidden", status_code=403)
    body = sessions_summary_json()
    write_audit_log(
        admin_user_id=int(u["user_id"]),
        action="admin_export_sessions",
        resource_type="export",
        detail={},
    )
    return PlainTextResponse(
        content=body,
        media_type="application/json; charset=utf-8",
    )
