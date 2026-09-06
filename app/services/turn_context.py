"""Job-local turn context for CTA hooks — never persisted in session.routing."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Literal

TurnMode = Literal["generate", "followup"] | None


@dataclass
class ConversationTurnContext:
    conversation_mode: TurnMode = None
    render_enqueued: bool = False
    busy_owner_id: str | None = None
    # Premium CTA (turn-local, never persisted)
    premium_selected: bool = False
    premium_level: str | None = None
    premium_soft_hint: str | None = None
    premium_cta_text: str | None = None
    premium_handoff_requested: bool = False
    suppress_supporter_cta: bool = False
    offer_id: str | None = None
    cooldown_advance_id: str | None = None
    skip_long_term_memory_extraction: bool = False
    premium_decision_authorized: bool = False
    premium_gap: str | None = None
    premium_intent: str | None = None
    premium_topic: str | None = None
    premium_commercial_action: str | None = None
    premium_post_v1: bool = False
    human_handoff_requested: bool = False
    handoff_intent: str | None = None
    handoff_evidence: str | None = None
    handoff_source: str | None = None
    handoff_skip_transition_ack: bool = False
    handoff_delivery_timing: str | None = None  # "direct" | "after_main" | None
    free_usage_chargeable: bool = False
    free_usage_blocked: bool = False
    free_usage_charge_id: str | None = None
    free_usage_used_before: int | None = None
    skip_conversation_history: bool = False
    suppress_premium_surface: bool = False
    deferred_comparison_surface: bool = False
    turn_plan: object | None = None  # TurnPlan — debug only, never persisted
    debug_routing_block_found: bool = False
    debug_semantic_route: dict[str, Any] | None = None
    turn_total_tokens: int = 0
    turn_cost_vnd_est: int = 0


_active_turn_context: ContextVar[ConversationTurnContext | None] = ContextVar(
    "active_turn_context", default=None,
)


@contextmanager
def turn_context_scope() -> Iterator[ConversationTurnContext]:
    ctx = ConversationTurnContext()
    token = _active_turn_context.set(ctx)
    try:
        yield ctx
    finally:
        _active_turn_context.reset(token)


def get_turn_context() -> ConversationTurnContext | None:
    return _active_turn_context.get()


def get_current_turn_context() -> ConversationTurnContext | None:
    return _active_turn_context.get()


def set_turn_mode(mode: TurnMode) -> None:
    ctx = _active_turn_context.get()
    if ctx is not None:
        ctx.conversation_mode = mode


def mark_render_enqueued() -> None:
    ctx = _active_turn_context.get()
    if ctx is not None:
        ctx.render_enqueued = True
