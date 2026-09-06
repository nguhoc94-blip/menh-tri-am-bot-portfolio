"""Lightweight delivery debug tracking in session.routing (text only, no images).

Stores page-level send status, interpretation text preview, and a small ops
chat snapshot — never JPEG bytes or new DB tables.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

OPS_CHAT_SNAPSHOT_LIMIT = 8
TEXT_PREVIEW_LIMIT = 500
CONTENT_PREVIEW_LIMIT = 200

PAGE_PENDING = "pending"
PAGE_RENDERED = "rendered"
PAGE_SENT = "sent"
PAGE_FAILED = "failed"

STAGE_OK = "ok"
STAGE_WAIT = "wait"
STAGE_WORKING = "working"
STAGE_FAILED = "failed"
STAGE_SKIP = "skip"

PIPELINE_STAGE_KEYS = ("compile", "bundle", "render", "send", "chat")

_ROUTING_BUNDLE_DEFERRED = "bundle_deferred"
_ROUTING_BIRTH_DATE_LOCKED = "birth_date_locked"
_BIRTH_DATE_FIELDS = frozenset({"birth_day", "birth_month", "birth_year"})
_ACTIVE_OVERVIEW_BUNDLE_STATUSES = frozenset({
    "pending", "running", "ready_pending_delivery",
})
_BIRTH_DATE_LOCKED_REPLY = (
    "Lá số và quyền lợi của bạn đã được lập theo ngày sinh hiện tại 🌿 "
    "Nên mình không thể đổi ngày/tháng/năm sinh nữa. "
    "Nếu cần hỗ trợ thêm, bạn liên hệ đội ngũ Demo Bot nhé."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_routing(session) -> dict[str, Any]:
    if not isinstance(session.routing, dict):
        session.routing = {}
    return session.routing


def get_delivery_progress(session) -> dict[str, Any] | None:
    routing = session.routing if isinstance(session.routing, dict) else {}
    dp = routing.get("delivery_progress")
    return dp if isinstance(dp, dict) else None


def get_pipeline_stages(session) -> dict[str, Any] | None:
    routing = session.routing if isinstance(session.routing, dict) else {}
    ps = routing.get("pipeline_stages")
    return ps if isinstance(ps, dict) else None


def is_overview_delivery_in_progress(session) -> bool:
    """True while compile/bundle/render/send pipeline is active (not grace defer)."""
    routing = _ensure_routing(session)
    if routing.get(_ROUTING_BUNDLE_DEFERRED):
        return False
    overview = routing.get("overview_bundle")
    if isinstance(overview, dict):
        status = str(overview.get("status") or "").lower()
        if status in _ACTIVE_OVERVIEW_BUNDLE_STATUSES:
            return True
    ps = get_pipeline_stages(session)
    if isinstance(ps, dict):
        for stage in ("compile", "bundle", "render", "send"):
            if str(ps.get(stage) or "").lower() in (STAGE_WORKING, STAGE_WAIT):
                return True
    return False


def is_birth_date_locked(session) -> bool:
    """True once overview benefit pipeline has started — birth date is frozen."""
    routing = _ensure_routing(session)
    if routing.get(_ROUTING_BUNDLE_DEFERRED):
        return False
    return bool(routing.get(_ROUTING_BIRTH_DATE_LOCKED))


def mark_birth_date_locked(session) -> None:
    """Freeze birth_day/month/year after overview compile/bundle pipeline starts."""
    routing = _ensure_routing(session)
    if routing.get(_ROUTING_BIRTH_DATE_LOCKED):
        return
    routing[_ROUTING_BIRTH_DATE_LOCKED] = True
    routing["birth_date_locked_at"] = _now_iso()


def clear_birth_date_lock(session) -> None:
    routing = _ensure_routing(session)
    routing.pop(_ROUTING_BIRTH_DATE_LOCKED, None)
    routing.pop("birth_date_locked_at", None)


def _normalize_birth_field_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return text


def _birth_field_value_changed(existing: Any, new: Any) -> bool:
    return _normalize_birth_field_value(existing) != _normalize_birth_field_value(new)


def is_overview_benefit_started(session) -> bool:
    """True once free overview bundle or premium compile/bundle work has begun."""
    routing = _ensure_routing(session)
    overview = routing.get("overview_bundle")
    if isinstance(overview, dict):
        status = str(overview.get("status") or "").lower()
        if status in _ACTIVE_OVERVIEW_BUNDLE_STATUSES | {
            "delivering", "flushing", "completed",
        }:
            return True
    ps = get_pipeline_stages(session)
    if not isinstance(ps, dict):
        return False
    for stage in ("compile", "bundle", "render", "send"):
        status = str(ps.get(stage) or "").lower()
        if status in (STAGE_WORKING, STAGE_WAIT, STAGE_OK):
            return True
    return False


def birth_date_change_blocked(
    session,
    fields: dict[str, Any],
) -> bool:
    """Block changing an already-set birth date after overview benefit has started."""
    if not is_birth_date_locked(session):
        return False
    if not is_overview_benefit_started(session):
        return False
    if not getattr(session, "is_birth_complete", lambda: False)():
        return False
    birth_data = getattr(session, "birth_data", None) or {}
    for key in _BIRTH_DATE_FIELDS:
        if key not in fields:
            continue
        existing = birth_data.get(key)
        if existing in (None, ""):
            continue
        if _birth_field_value_changed(existing, fields[key]):
            return True
    return False


def birth_date_locked_reply() -> str:
    return _BIRTH_DATE_LOCKED_REPLY


def _default_pipeline_stages(
    *,
    generation_id: str,
    compile_status: str = STAGE_SKIP,
) -> dict[str, Any]:
    return {
        "generation_id": generation_id,
        "compile": compile_status,
        "bundle": STAGE_WAIT,
        "render": STAGE_WAIT,
        "send": STAGE_WAIT,
        "chat": STAGE_OK,
        "last_updated_at": _now_iso(),
    }


def init_pipeline_stages(
    session,
    *,
    generation_id: str,
    compile_status: str = STAGE_SKIP,
) -> dict[str, Any]:
    """Initialize per-worker pipeline stage tracking for ops visibility."""
    routing = _ensure_routing(session)
    ps = _default_pipeline_stages(
        generation_id=generation_id,
        compile_status=compile_status,
    )
    routing["pipeline_stages"] = ps
    refresh_ops_chat_snapshot(session)
    return ps


def _resolve_ps(
    session,
    generation_id: str,
    *,
    allow_init: bool = False,
    compile_status: str = STAGE_SKIP,
) -> dict[str, Any] | None:
    ps = get_pipeline_stages(session)
    if ps is None:
        if allow_init:
            return init_pipeline_stages(
                session,
                generation_id=generation_id,
                compile_status=compile_status,
            )
        return None
    if ps.get("generation_id") != generation_id:
        if allow_init:
            return init_pipeline_stages(
                session,
                generation_id=generation_id,
                compile_status=compile_status,
            )
        return None
    return ps


def set_pipeline_stage(
    session,
    stage: str,
    status: str,
    *,
    generation_id: str,
    allow_init: bool = False,
    compile_status: str = STAGE_SKIP,
) -> None:
    if stage not in PIPELINE_STAGE_KEYS:
        return
    ps = _resolve_ps(
        session,
        generation_id,
        allow_init=allow_init,
        compile_status=compile_status,
    )
    if ps is None:
        return
    ps[stage] = status
    ps["last_updated_at"] = _now_iso()
    refresh_ops_chat_snapshot(session)


def set_pipeline_stages(
    session,
    *,
    generation_id: str,
    allow_init: bool = True,
    compile_status: str = STAGE_SKIP,
    **stages: str,
) -> None:
    ps = _resolve_ps(
        session,
        generation_id,
        allow_init=allow_init,
        compile_status=compile_status,
    )
    if ps is None:
        return
    for stage, status in stages.items():
        if stage in PIPELINE_STAGE_KEYS:
            ps[stage] = status
    ps["last_updated_at"] = _now_iso()
    refresh_ops_chat_snapshot(session)


def mark_pipeline_render_only_start(session, *, generation_id: str) -> None:
    """Palm/face/combined render path (no compile/bundle stages)."""
    set_pipeline_stages(
        session,
        generation_id=generation_id,
        allow_init=True,
        compile_status=STAGE_SKIP,
        compile=STAGE_SKIP,
        bundle=STAGE_SKIP,
        render=STAGE_WORKING,
        send=STAGE_WAIT,
    )


def mark_pipeline_render_complete(session, *, generation_id: str) -> None:
    set_pipeline_stages(
        session,
        generation_id=generation_id,
        render=STAGE_OK,
        send=STAGE_WORKING,
    )


def mark_pipeline_delivery_complete(session, *, generation_id: str) -> None:
    set_pipeline_stage(session, "send", STAGE_OK, generation_id=generation_id)


def mark_pipeline_stuck(session, reason: str, *, generation_id: str) -> None:
    reason_l = (reason or "").lower()
    if "render" in reason_l:
        set_pipeline_stage(session, "render", STAGE_FAILED, generation_id=generation_id)
    elif "send" in reason_l or "delivery" in reason_l:
        set_pipeline_stage(session, "send", STAGE_FAILED, generation_id=generation_id)
    elif "compile" in reason_l:
        set_pipeline_stage(session, "compile", STAGE_FAILED, generation_id=generation_id)
    elif "bundle" in reason_l:
        set_pipeline_stage(session, "bundle", STAGE_FAILED, generation_id=generation_id)


def refresh_ops_chat_snapshot(session) -> None:
    """Copy last N chat turns from session.history into routing for ops visibility."""
    routing = _ensure_routing(session)
    history = getattr(session, "history", None) or []
    if not isinstance(history, list):
        return
    snapshot: list[dict[str, Any]] = []
    for turn in history[-OPS_CHAT_SNAPSHOT_LIMIT:]:
        if not isinstance(turn, dict):
            continue
        content = str(turn.get("content") or "")
        if not content.strip():
            continue
        snapshot.append(
            {
                "role": str(turn.get("role") or "user"),
                "content_preview": content[:CONTENT_PREVIEW_LIMIT],
                "chars": len(content),
            }
        )
    routing["ops_chat_snapshot"] = snapshot
    routing["ops_chat_snapshot_at"] = _now_iso()


def init_delivery_progress(
    session,
    *,
    generation_id: str,
    interpretation_text: str | None = None,
) -> dict[str, Any]:
    routing = _ensure_routing(session)
    text = interpretation_text or ""
    dp: dict[str, Any] = {
        "generation_id": generation_id,
        "interpretation_text_preview": text[:TEXT_PREVIEW_LIMIT],
        "interpretation_chars": len(text),
        "pages": {},
        "last_updated_at": _now_iso(),
        "stuck_reason": None,
    }
    routing["delivery_progress"] = dp
    mark_birth_date_locked(session)
    set_pipeline_stages(
        session,
        generation_id=generation_id,
        allow_init=True,
        compile_status=STAGE_OK,
        compile=STAGE_OK,
        bundle=STAGE_WAIT,
        render=STAGE_WAIT,
        send=STAGE_WAIT,
    )
    refresh_ops_chat_snapshot(session)
    return dp


def _resolve_dp(session, generation_id: str, *, allow_init: bool = True) -> dict[str, Any] | None:
    dp = get_delivery_progress(session)
    if dp is None:
        if allow_init:
            return init_delivery_progress(session, generation_id=generation_id)
        return None
    if dp.get("generation_id") != generation_id:
        return None
    return dp


def update_interpretation_text(
    session,
    *,
    text: str,
    generation_id: str,
) -> None:
    dp = _resolve_dp(session, generation_id, allow_init=True)
    if dp is None:
        return
    dp["interpretation_text_preview"] = text[:TEXT_PREVIEW_LIMIT]
    dp["interpretation_chars"] = len(text)
    dp["last_updated_at"] = _now_iso()
    set_pipeline_stages(
        session,
        generation_id=generation_id,
        bundle=STAGE_OK,
        render=STAGE_WAIT,
    )
    refresh_ops_chat_snapshot(session)


def interpretation_text_from_payload(payload: Any) -> str:
    """Best-effort text extract from compiled interpretation payload."""
    if payload is None:
        return ""
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        return str(payload)[:TEXT_PREVIEW_LIMIT]
    parts: list[str] = []
    houses = payload.get("houses") or {}
    if isinstance(houses, dict):
        for house in houses.values():
            if not isinstance(house, dict):
                continue
            syn = house.get("synthesis") or {}
            if isinstance(syn, dict):
                conclusion = str(syn.get("final_conclusion") or "").strip()
                if conclusion:
                    parts.append(conclusion)
    if parts:
        return "\n".join(parts)
    return json.dumps(payload, ensure_ascii=False)[:TEXT_PREVIEW_LIMIT]


def set_pages_status(
    session,
    page_numbers: list[int],
    status: str,
    *,
    generation_id: str,
) -> None:
    dp = get_delivery_progress(session)
    if dp is None:
        dp = init_delivery_progress(session, generation_id=generation_id)
    elif dp.get("generation_id") != generation_id:
        return
    pages = dp.setdefault("pages", {})
    for num in page_numbers:
        pages[str(num)] = status
    dp["last_updated_at"] = _now_iso()
    if status == PAGE_PENDING:
        mark_pipeline_render_complete(session, generation_id=generation_id)
    refresh_ops_chat_snapshot(session)


def update_page_status(
    session,
    page_num: int,
    status: str,
    *,
    generation_id: str,
) -> None:
    set_pages_status(session, [page_num], status, generation_id=generation_id)


def set_stuck_reason(
    session,
    reason: str,
    *,
    generation_id: str,
) -> None:
    dp = _resolve_dp(session, generation_id, allow_init=False)
    if dp is None:
        return
    dp["stuck_reason"] = reason
    dp["last_updated_at"] = _now_iso()
    mark_pipeline_stuck(session, reason, generation_id=generation_id)
    refresh_ops_chat_snapshot(session)


def mark_bundle_delivery_complete(session, *, generation_id: str) -> None:
    """Clear stuck_reason when all pages delivered."""
    dp = _resolve_dp(session, generation_id, allow_init=False)
    if dp is None:
        return
    dp["stuck_reason"] = None
    dp["bundle_completed_at"] = _now_iso()
    dp["last_updated_at"] = _now_iso()
    mark_pipeline_delivery_complete(session, generation_id=generation_id)
