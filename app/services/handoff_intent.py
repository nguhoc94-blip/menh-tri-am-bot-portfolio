"""Deterministic human handoff intent — independent from Premium CTA suppression."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from app.services.premium_trigger import PremiumCommercialAction

_ASTROLOGY_SUBSTANTIVE = re.compile(
    r"(?:lá\s*số|cung|sao|mệnh|tử\s*vi|vận|đại\s*vận|tiểu\s*vận|công\s*việc|tài\s*chính|"
    r"sự\s*nghiệp|tình\s*cảm|gia\s*đình)",
    re.I,
)


class HandoffIntent(str, Enum):
    NONE = "none"
    INTEREST = "interest"
    REQUEST = "request"


class HandoffEvidence(str, Enum):
    NONE = "none"
    EXPLICIT_RULE = "explicit_rule"
    COMMERCIAL_ACTION = "commercial_action"
    MEDIUM_ACCEPTANCE = "medium_acceptance"
    GPT_SEMANTIC = "gpt_semantic"
    FRUSTRATION_ESCALATION = "frustration_escalation"


class HandoffSource(str, Enum):
    NONE = "none"
    PREMIUM = "premium"
    SUPPORT = "support"
    SEMANTIC_SUPPORT = "semantic_support"


@dataclass(frozen=True)
class HandoffIntentDecision:
    intent: HandoffIntent
    evidence: HandoffEvidence
    source: HandoffSource


_EXPLICIT_HUMAN = re.compile(
    r"(?:"
    r"người\s*thật|nhân\s*viên|người\s*tư\s*vấn|người\s*hỗ\s*trợ|người\s*phụ\s*trách|"
    r"nói\s*chuyện\s*trực\s*tiếp|trao\s*đổi\s*trực\s*tiếp|"
    r"đội\s*(?:tri\s*âm|của\s*bạn|ngũ|hỗ\s*trợ)|"
    r"kết\s*nối\s*(?:mình\s*)?(?:với\s*)?(?:người|đội|nhân\s*viên|bên)|"
    r"người\s*hỗ\s*trợ\s*trực\s*tiếp|"
    r"gặp\s*(?:người|nhân\s*viên|họ\b)|"
    r"liên\s*hệ\s*người|"
    r"ai\s*bên\s*bạn|người\s*bên\s*bạn|bên\s*tri\s*âm|"
    r"cho\s*(?:mình\s*)?gặp\s*(?:người|nhân\s*viên)"
    r")",
    re.I,
)

_BOT_AS_ADVISOR = re.compile(
    r"bạn\s*tư\s*vấn|bot\s*tư\s*vấn|mình\s*tư\s*vấn",
    re.I,
)

_CRISIS_ONLY = re.compile(
    r"^(?:mình\s*)?(?:tuyệt\s*vọng|chán\s*nản|buồn\s*quá|mệt\s*mỏi\s*quá)(?:\s*quá)?[!.?\s]*$",
    re.I,
)

_COMPLAINT_WITHOUT_SUPPORT = re.compile(
    r"(?:bot|bạn)\s*(?:nói|trả\s*lời)\s*sai|"
    r"sai\s*rồi|"
    r"không\s*đúng",
    re.I,
)

_PREMIUM_FRICTION = re.compile(
    r"(?:"
    r"phiền\s*thế|"
    r"mãi\s*không\s*được|"
    r"đăng\s*k[ýí]\s*mãi\s*không\s*được|"
    r"mua\s*mãi\s*không\s*được|"
    r"sao\s*(?:đăng\s*k[ýí]|mua)\s*khó"
    r")",
    re.I,
)

_PURCHASE_REFERENCE = re.compile(
    r"(?:đăng\s*k[ýí]|mua|hồ\s*sơ|chuyên\s*sâu|thanh\s*toán)",
    re.I,
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", (text or "").strip().lower())


def detect_explicit_human_request(user_message: str) -> bool:
    """High-precision patterns for explicit human-support requests."""
    norm = _normalize(user_message)
    if not norm:
        return False
    if _BOT_AS_ADVISOR.search(norm) and not _EXPLICIT_HUMAN.search(norm):
        return False
    return bool(_EXPLICIT_HUMAN.search(norm))


def detect_human_handoff_request(user_message: str) -> bool:
    """Backward-compatible alias for explicit human request detection."""
    return detect_explicit_human_request(user_message)


def is_pure_handoff_request(user_message: str) -> bool:
    """KEEP_HARD_GUARD — explicit human REQUEST without substantive astrology."""
    if not detect_explicit_human_request(user_message):
        return False
    norm = _normalize(user_message)
    return not bool(_ASTROLOGY_SUBSTANTIVE.search(norm))


def map_commercial_action_to_handoff(
    action: PremiumCommercialAction,
) -> HandoffIntent:
    if action == PremiumCommercialAction.INFO:
        return HandoffIntent.NONE
    if action in (
        PremiumCommercialAction.SAMPLE,
        PremiumCommercialAction.PRICE,
        PremiumCommercialAction.PURCHASE,
        PremiumCommercialAction.SUPPORT,
    ):
        return HandoffIntent.REQUEST
    return HandoffIntent.NONE


def parse_gpt_handoff_hint(raw_value: str | None) -> HandoffIntent:
    value = (raw_value or "").strip().upper()
    if value == "REQUEST":
        return HandoffIntent.REQUEST
    if value == "INTEREST":
        return HandoffIntent.INTEREST
    return HandoffIntent.NONE


@dataclass(frozen=True)
class ResolveHandoffInput:
    user_message: str
    gpt_hint: HandoffIntent = HandoffIntent.NONE
    medium_active: bool = False
    premium_decline_recorded: bool = False
    commercial_action: PremiumCommercialAction = PremiumCommercialAction.NONE
    pure_commercial: bool = False
    commercial_handoff_eligible: bool = False
    product_context_active: bool = False
    semantic_route: object | None = None  # SemanticRoute — policy-pre-validated REQUEST bypass


def is_premium_frustration_escalation(
    user_message: str,
    *,
    product_context_active: bool,
) -> bool:
    if not product_context_active:
        return False
    norm = _normalize(user_message)
    if not norm:
        return False
    if not _PREMIUM_FRICTION.search(norm):
        return False
    return bool(_PURCHASE_REFERENCE.search(norm))


def resolve_handoff_intent(inp: ResolveHandoffInput) -> HandoffIntentDecision:
    """Backend fusion — fail-closed; GPT hint alone never triggers REQUEST."""
    msg = inp.user_message or ""

    if _CRISIS_ONLY.match(_normalize(msg)):
        return HandoffIntentDecision(
            HandoffIntent.NONE, HandoffEvidence.NONE, HandoffSource.NONE,
        )

    if detect_explicit_human_request(msg):
        return HandoffIntentDecision(
            HandoffIntent.REQUEST,
            HandoffEvidence.EXPLICIT_RULE,
            HandoffSource.SEMANTIC_SUPPORT,
        )

    if is_premium_frustration_escalation(
        msg,
        product_context_active=inp.product_context_active,
    ):
        return HandoffIntentDecision(
            HandoffIntent.REQUEST,
            HandoffEvidence.FRUSTRATION_ESCALATION,
            HandoffSource.PREMIUM,
        )

    if inp.commercial_handoff_eligible and not inp.premium_decline_recorded:
        mapped = map_commercial_action_to_handoff(inp.commercial_action)
        if mapped == HandoffIntent.REQUEST:
            return HandoffIntentDecision(
                HandoffIntent.REQUEST,
                HandoffEvidence.COMMERCIAL_ACTION,
                HandoffSource.PREMIUM,
            )

    if inp.pure_commercial and inp.commercial_action != PremiumCommercialAction.NONE:
        mapped = map_commercial_action_to_handoff(inp.commercial_action)
        if mapped == HandoffIntent.REQUEST:
            if inp.premium_decline_recorded:
                return HandoffIntentDecision(
                    HandoffIntent.NONE, HandoffEvidence.NONE, HandoffSource.NONE,
                )
            return HandoffIntentDecision(
                HandoffIntent.REQUEST,
                HandoffEvidence.COMMERCIAL_ACTION,
                HandoffSource.PREMIUM,
            )
        return HandoffIntentDecision(
            HandoffIntent.NONE, HandoffEvidence.NONE, HandoffSource.NONE,
        )

    if inp.gpt_hint == HandoffIntent.REQUEST:
        sr = inp.semantic_route
        if sr is not None and getattr(sr, "handoff_intent", None) == HandoffIntent.REQUEST:
            return HandoffIntentDecision(
                HandoffIntent.REQUEST,
                HandoffEvidence.GPT_SEMANTIC,
                HandoffSource.SEMANTIC_SUPPORT,
            )
        return HandoffIntentDecision(
            HandoffIntent.NONE, HandoffEvidence.NONE, HandoffSource.NONE,
        )

    if (
        _COMPLAINT_WITHOUT_SUPPORT.search(_normalize(msg))
        and not detect_explicit_human_request(msg)
    ):
        return HandoffIntentDecision(
            HandoffIntent.NONE, HandoffEvidence.NONE, HandoffSource.NONE,
        )

    return HandoffIntentDecision(
        HandoffIntent.NONE, HandoffEvidence.NONE, HandoffSource.NONE,
    )


def apply_handoff_decision_to_context(ctx: object, decision: HandoffIntentDecision) -> None:
    """Populate turn context from fused handoff decision."""
    ctx.handoff_intent = decision.intent.value
    ctx.handoff_evidence = decision.evidence.value
    ctx.handoff_source = decision.source.value
    ctx.premium_handoff_requested = (
        decision.intent == HandoffIntent.REQUEST
        and decision.source == HandoffSource.PREMIUM
    )
    ctx.human_handoff_requested = (
        decision.intent == HandoffIntent.REQUEST
        and decision.source in (HandoffSource.SEMANTIC_SUPPORT, HandoffSource.SUPPORT)
    )
    if decision.intent == HandoffIntent.REQUEST:
        ctx.skip_long_term_memory_extraction = True
        ctx.suppress_supporter_cta = True


def apply_handoff_delivery_timing(
    ctx: object,
    *,
    pure_commercial: bool,
) -> None:
    """Set DIRECT vs AFTER_MAIN timing once fusion resolved REQUEST."""
    if getattr(ctx, "handoff_intent", None) != "request":
        ctx.handoff_delivery_timing = None
        return
    ctx.handoff_delivery_timing = "direct" if pure_commercial else "after_main"
