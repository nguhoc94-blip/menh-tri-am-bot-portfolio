"""
Capture Messenger outbound (text + image) for debug sender IDs (dbg_*).

Sync debug chat uses a request-scoped collector (ContextVar).
Worker/async sends persist to session.routing in Postgres so Poll works
across separate API and worker processes.
"""

from __future__ import annotations

import base64
import logging
import os
from contextvars import ContextVar
from typing import Any, Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_collector: ContextVar[list["DebugOutboundItem"] | None] = ContextVar(
    "debug_outbound_collector",
    default=None,
)

_DEBUG_ROUTING_KEY = "debug_pending_outbound"
_MAX_PENDING_ITEMS = 20


class DebugOutboundItem(BaseModel):
    kind: Literal["text", "image"]
    text: str | None = None
    image_b64: str | None = None
    mime: str = "image/jpeg"


def is_debug_sender(sender_id: str) -> bool:
    return bool(sender_id) and sender_id.startswith("dbg_")


def debug_capture_enabled() -> bool:
    return os.environ.get("DEBUG_CHAT_ENABLED", "").strip() == "1"


def should_capture(sender_id: str) -> bool:
    return debug_capture_enabled() and is_debug_sender(sender_id)


def prefer_full_text_for_debug(
    sender_id: str,
    full_text: str,
    production_message: str | None,
) -> str | None:
    """Debug chat shows the full bot reply; production gets short teaser."""
    if should_capture(sender_id) and (full_text or "").strip():
        return full_text.strip()
    return production_message


def start_collector() -> None:
    _collector.set([])


def drain_collector() -> list[DebugOutboundItem]:
    items = _collector.get()
    _collector.set(None)
    return list(items or [])


def clear_collector() -> None:
    _collector.set(None)


def texts_to_items(texts: list[str]) -> list[DebugOutboundItem]:
    return [
        DebugOutboundItem(kind="text", text=t)
        for t in texts
        if t and str(t).strip()
    ]


def merge_outbound(*parts: list[DebugOutboundItem]) -> list[DebugOutboundItem]:
    merged: list[DebugOutboundItem] = []
    for part in parts:
        merged.extend(part)
    return merged


def _parse_item(raw: Any) -> DebugOutboundItem | None:
    if isinstance(raw, DebugOutboundItem):
        return raw
    if isinstance(raw, dict):
        try:
            return DebugOutboundItem.model_validate(raw)
        except Exception:
            return None
    return None


def _load_db_pending(sender_id: str) -> list[DebugOutboundItem]:
    from app.services.messenger_state_db import DbMessengerStateStore

    try:
        session = DbMessengerStateStore().get_or_create(sender_id)
    except Exception:
        logger.exception("debug_outbound_load_failed sender_id=%s", sender_id)
        return []
    routing = dict(session.routing) if isinstance(session.routing, dict) else {}
    raw_list = routing.get(_DEBUG_ROUTING_KEY) or []
    if not isinstance(raw_list, list):
        return []
    items: list[DebugOutboundItem] = []
    for raw in raw_list:
        parsed = _parse_item(raw)
        if parsed is not None:
            items.append(parsed)
    return items


def _append_pending_db(sender_id: str, item: DebugOutboundItem) -> None:
    from app.services.messenger_state_db import DbMessengerStateStore

    try:
        store = DbMessengerStateStore()
        session = store.get_or_create(sender_id)
        routing = dict(session.routing) if isinstance(session.routing, dict) else {}
        raw_list = list(routing.get(_DEBUG_ROUTING_KEY) or [])
        items: list[DebugOutboundItem] = []
        for raw in raw_list:
            parsed = _parse_item(raw)
            if parsed is not None:
                items.append(parsed)
        items.append(item)
        if len(items) > _MAX_PENDING_ITEMS:
            items = items[-_MAX_PENDING_ITEMS:]
        routing[_DEBUG_ROUTING_KEY] = [i.model_dump() for i in items]
        session.routing = routing
        store.save(session)
        logger.info(
            "debug_outbound_persisted sender_id=%s kind=%s total=%s",
            sender_id,
            item.kind,
            len(items),
        )
    except Exception:
        logger.exception("debug_outbound_persist_failed sender_id=%s", sender_id)


def _clear_db_pending(sender_id: str) -> list[DebugOutboundItem]:
    from app.services.messenger_state_db import DbMessengerStateStore

    items = _load_db_pending(sender_id)
    if not items:
        return []
    try:
        store = DbMessengerStateStore()
        session = store.get_or_create(sender_id)
        routing = dict(session.routing) if isinstance(session.routing, dict) else {}
        routing.pop(_DEBUG_ROUTING_KEY, None)
        session.routing = routing
        store.save(session)
    except Exception:
        logger.exception("debug_outbound_clear_failed sender_id=%s", sender_id)
    return items


def capture_text(sender_id: str, text: str) -> None:
    if not should_capture(sender_id):
        return
    stripped = (text or "").strip()
    if not stripped:
        return
    item = DebugOutboundItem(kind="text", text=stripped)
    coll = _collector.get()
    if coll is not None:
        coll.append(item)
    else:
        _append_pending(sender_id, item)


def capture_image(
    sender_id: str,
    image_bytes: bytes,
    *,
    mime: str = "image/jpeg",
) -> None:
    if not should_capture(sender_id) or not image_bytes:
        return
    item = DebugOutboundItem(
        kind="image",
        image_b64=base64.b64encode(image_bytes).decode("ascii"),
        mime=mime or "image/jpeg",
    )
    coll = _collector.get()
    if coll is not None:
        coll.append(item)
    else:
        _append_pending(sender_id, item)


def poll_pending(sender_id: str, *, clear: bool = True) -> list[DebugOutboundItem]:
    if not should_capture(sender_id):
        return []
    if clear:
        return _clear_db_pending(sender_id)
    return _load_db_pending(sender_id)


def peek_pending(sender_id: str) -> list[DebugOutboundItem]:
    if not should_capture(sender_id):
        return []
    return _load_db_pending(sender_id)


def pending_count(sender_id: str) -> int:
    return len(peek_pending(sender_id))


def _append_pending(sender_id: str, item: DebugOutboundItem) -> None:
    _append_pending_db(sender_id, item)
