"""Conservative pre-GPT scope guard — blocks obvious general-purpose AI abuse."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SCOPE_GUARD_REPLY = (
    "Mình chủ yếu hỗ trợ bạn về Tử Vi và những vấn đề trong đời sống cá nhân như "
    "tình cảm, gia đình, công việc, tài chính hay những lựa chọn bạn đang cân nhắc. "
    "Các yêu cầu như viết code, làm nội dung/kịch bản, hướng dẫn công cụ hoặc những "
    "công việc kiểu trợ lý AI tổng quát thì mình không hỗ trợ nhé 🌿\n\n"
    "Nếu bạn muốn, cứ kể tình huống của chính bạn hoặc hỏi tiếp về lá số."
)

_SKIP_INTENT_ACTIONS = frozenset({
    "cancel",
    "support",
    "confirm_profile_switch",
    "decline_profile_switch",
    "ambiguous_profile_switch",
    "correct_birth",
    "request_chart",
    "proceed_provisional",
    "reject_assumptions",
})


@dataclass(frozen=True)
class ScopeDecision:
    blocked: bool
    category: str | None = None


def _fold_vi(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", text.lower())
    s = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d")


def _normalize(text: str) -> str:
    folded = _fold_vi(text)
    return re.sub(r"\s+", " ", folded).strip()


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


# Personal-life safe harbor — fail open when clearly life guidance / communication help.
_PERSONAL_SAFE_PATTERNS: tuple[re.Pattern[str], ...] = (
    _compile(r"\bco nen\b"),
    _compile(r"\bnen (lam|nghi|noi|gap|chon)\b"),
    _compile(r"\b(tinh cam|tinh duyen|vo chong|ban trai|ban gai|gia dinh)\b"),
    _compile(r"\b(ap luc|cang thang|mau thuan|cai nhau)\b"),
    _compile(r"\b(giup|giup toi).*\b(nhan|noi|xin loi|noi chuyen)\b"),
    _compile(r"\b(nhan|noi|viet).*\b(xin loi|cam on)\b.*\b(vo|chong|sep|me|bo|ban)\b"),
    _compile(r"\bnoi (voi|chuyen voi)\b.*\b(vo|chong|sep|me|bo|ban)\b"),
    _compile(r"\bphuong an\b.*\b(luc nay|truoc|thu (hai|ba|tu|nam|sau|bay|tam|chin|muoi))\b"),
    _compile(r"\bcong viec\b.*\b(nam|toi|minh|cua toi|the nao|ra sao)\b"),
    _compile(r"\b(tai chinh|tien bac|no tien|tien nong)\b"),
    _compile(r"\b(la so|tu vi|cung|sao|menh|tai bach|quan loc|phu the)\b.*\b(the nao|ra sao|nhu the nao|y nghia)\b"),
)

# High-confidence block rules: require strong deliverable/tool intent, not single keywords.
_BLOCK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("technical", _compile(
        r"\b(viet|tao|lam|giup viet|soan|debug|sua|fix|crawl|implement)\b"
        r".*\b(python|javascript|\bjs\b|typescript|sql|docker|html|css|react|nodejs|"
        r"node js|api\b|\bcode\b|website|web app|\bapp\b)\b"
    )),
    ("technical", _compile(
        r"\b(python|javascript|\bjs\b|typescript|sql)\b.*\b(code|script|chuong trinh)\b"
    )),
    ("technical", _compile(
        r"\bdebug\b.*\b(javascript|\bjs\b|python|typescript|code)\b"
    )),
    ("technical", _compile(
        r"\b(huong dan|chi toi)\b.*\b(lap trinh|code|python|javascript|sql|docker|api)\b"
    )),
    ("content_creation", _compile(
        r"\b(kich ban|script)\b.*\b(tiktok|youtube|reels|shorts|video)\b"
    )),
    ("content_creation", _compile(
        r"\b(tiktok|youtube|reels)\b.*\b(kich ban|script|60 giay|60s)\b"
    )),
    ("content_creation", _compile(
        r"\b(viet|tao|lam|soan)\b.*\b(seo|caption|content marketing|quang cao|\bads\b)\b"
    )),
    ("content_creation", _compile(
        r"\bprompt\b.*\b(veo|anh|video|midjourney|dall|stable diffusion)\b"
    )),
    ("content_creation", _compile(
        r"\b(google flow|veo 3|veo3|capcut|canva)\b"
    )),
    ("content_creation", _compile(
        r"\bhuong dan\b.*\b(lam video|tao video|google flow|veo|capcut|canva)\b"
    )),
    ("productivity", _compile(
        r"\b(lam|tao|viet|giup)\b.*\b(excel|powerpoint|\bppt\b|spreadsheet|file excel)\b"
    )),
    ("productivity", _compile(
        r"\bexcel\b.*\b(cong thuc|tinh doanh thu|bang tinh|pivot)\b"
    )),
    ("academic", _compile(
        r"\b(giai bai|giai toan|giai de|bai tap|luan van|bai luan)\b"
    )),
    ("general_assistant", _compile(
        r"\bdich\b.*\b(doan|van ban|tieng anh|document|bai viet|paragraph)\b"
    )),
    ("general_assistant", _compile(
        r"\btranslate\b"
    )),
    ("general_assistant", _compile(
        r"\b(tom tat|summarize)\b.*\b(bai viet|article|document|van ban|paper)\b"
    )),
)


def _is_personal_life_context(normalized: str) -> bool:
    return any(pattern.search(normalized) for pattern in _PERSONAL_SAFE_PATTERNS)


def classify_obvious_out_of_scope(text: str) -> ScopeDecision:
    """Return blocked=True only for high-confidence out-of-scope deliverable requests."""
    raw = (text or "").strip()
    if not raw:
        return ScopeDecision(blocked=False)

    normalized = _normalize(raw)
    if _is_personal_life_context(normalized):
        return ScopeDecision(blocked=False)

    for category, pattern in _BLOCK_RULES:
        if pattern.search(normalized):
            return ScopeDecision(blocked=True, category=category)

    return ScopeDecision(blocked=False)


def should_apply_scope_guard(
    *,
    intent_action: str,
    profile_guard_reply: str | None,
    intent_reply: str | None,
    is_short_ack_turn: bool,
    message_payload: str | None,
    is_bot_identity: bool,
    has_birth_signal: bool,
    payload_is_intent_confirmation: bool,
) -> bool:
    """True when the deterministic scope guard may run on this turn."""
    if profile_guard_reply or intent_reply:
        return False
    if is_short_ack_turn or is_bot_identity:
        return False
    if payload_is_intent_confirmation:
        return False
    if intent_action in _SKIP_INTENT_ACTIONS:
        return False
    if has_birth_signal:
        return False
    return True
