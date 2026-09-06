"""Context-aware outbound reply sanitizers — Phase 6A."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from app.services.intake_policy import (
    FORBIDDEN_INTAKE_REDIRECT,
    TIMEZONE_COPY,
    labels_for_missing,
    runtime_missing_birth_fields,
)
from app.services.feature_flags import is_multimodal_enabled
from app.services.messenger_state import ConversationState, MessengerSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SanitizedReply:
    text: str
    changed: bool
    reason: str | None = None


_BIRTH_PLACE_KEYWORDS = (
    "noi sinh",
    "dia diem sinh",
    "sinh o dau",
    "sinh tai",
    "tinh thanh sinh",
    "que quan",
    "que o",
    "thanh pho sinh",
    "dia chi sinh",
)

_CHART_OBJECT_KEYWORDS = (
    "la so",
    "laso",
    "birth chart",
)

_CHART_UPLOAD_VERBS = (
    "gui",
    "upload",
    "tai len",
    "chup",
    "gui hinh",
    "gui anh",
    "send",
)

_PALM_OBJECT_HINTS = (
    "ban tay",
    "long ban tay",
    "chi tay",
    "anh tay",
)

_FACE_OBJECT_HINTS = (
    "khuon mat",
    "tuong mat",
    "anh mat",
    "mat ban",
)

_GENERATE_PROMISE_KEYWORDS = (
    "xem ngay",
    "lap la so ngay",
    "da du thong tin",
    "se dung la so",
    "dung la so cho ban",
)

_MULTIMODAL_PROMO_KEYWORDS = (
    "chi tay",
    "tuong mat",
    "ban tay",
    "khuon mat",
    "anh tay",
    "anh mat",
    "xem tuong",
    "nhan tuong",
    "thu tuong",
)

# Internal corpus / source identifiers — must not reach customer-facing text.
_INTERNAL_SOURCE_DETECT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\btb56[_\.]\w+", re.I), "tb56_id"),
    (re.compile(r"\btb56_1956\b", re.I), "tb56_1956"),
    (re.compile(r"\bTB56\b", re.I), "TB56"),
    (re.compile(r"\bchunk_id\b", re.I), "chunk_id"),
    (re.compile(r"\bsource_id\b", re.I), "source_id"),
    (re.compile(r"\bPARAPHRASE_NON_FATALISTIC\b", re.I), "delivery_note"),
)

_GROUNDING_PROSE_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bTB56\s+yêu cầu\b", re.I), "Khi luận giải cần"),
    (re.compile(r"\bTB56\s+mô tả\b", re.I), "Theo hướng luận giải"),
    (re.compile(r"\bTB56\s+coi\b", re.I), "Luận giải coi"),
    (re.compile(r"\bTB56\s+phân biệt\b", re.I), "Cần phân biệt"),
    (re.compile(r"\bTB56\s+lấy\b", re.I), "Nên lấy"),
    (re.compile(r"\bTB56\s+nói\b", re.I), "Theo hướng luận giải"),
    (re.compile(r"\bcủa\s+TB56\b", re.I), "trong luận giải"),
    (re.compile(r"\btheo\s+TB56\b", re.I), "theo hướng luận giải"),
    (re.compile(r"\bTB56\b", re.I), ""),
)

_INTERNAL_TERM_SENTENCE_FALLBACK = (
    "Mình diễn đạt lại theo hướng luận giải tử vi phù hợp với lá số của bạn."
)


def _fold_vi(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", text.lower())
    s = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d")


def contains_internal_source_term(text: str) -> str | None:
    """Return the matched internal-source label if ``text`` leaks corpus metadata."""
    if not text:
        return None
    for pattern, label in _INTERNAL_SOURCE_DETECT_PATTERNS:
        if pattern.search(text):
            return label
    return None


def rewrite_internal_source_terms(text: str) -> tuple[str, bool]:
    """Rewrite internal source phrasing into neutral customer-safe prose."""
    if not text:
        return text, False

    rewritten = text
    changed = False
    for pattern, replacement in _GROUNDING_PROSE_REWRITES:
        updated, count = pattern.subn(replacement, rewritten)
        if count:
            rewritten = updated
            changed = True

    for pattern, _label in _INTERNAL_SOURCE_DETECT_PATTERNS:
        updated, count = pattern.subn("", rewritten)
        if count:
            rewritten = updated
            changed = True

    rewritten = re.sub(r"\s{2,}", " ", rewritten)
    rewritten = re.sub(r"\s+([,.;:!?])", r"\1", rewritten)
    rewritten = rewritten.strip()
    if rewritten != text.strip():
        changed = True
    return rewritten, changed


def _sanitize_internal_source_sentences(reply: str) -> SanitizedReply:
    sentences = _split_sentences(reply)
    if not sentences:
        return SanitizedReply(reply, False, None)

    changed = False
    out: list[str] = []
    for sentence in sentences:
        leak = contains_internal_source_term(sentence)
        if not leak:
            out.append(sentence)
            continue

        rewritten, did_rewrite = rewrite_internal_source_terms(sentence)
        if did_rewrite and rewritten and not contains_internal_source_term(rewritten):
            out.append(rewritten)
            changed = True
            logger.info("internal_term_leak_blocked label=%s action=rewrite", leak)
            continue

        out.append(_INTERNAL_TERM_SENTENCE_FALLBACK)
        changed = True
        logger.info("internal_term_leak_blocked label=%s action=replace", leak)

    if not changed:
        return SanitizedReply(reply, False, None)
    joined = _join_sentences(out)
    if not joined.strip():
        joined = _INTERNAL_TERM_SENTENCE_FALLBACK
    return SanitizedReply(joined, True, "internal_source_term")


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _join_sentences(parts: list[str]) -> str:
    return "\n".join(parts) if any("\n" in p for p in parts) else " ".join(parts)


def _sentence_mentions_birth_place(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    return any(kw in folded for kw in _BIRTH_PLACE_KEYWORDS)


def _sentence_requests_chart_upload(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    has_chart = any(kw in folded for kw in _CHART_OBJECT_KEYWORDS)
    if not has_chart:
        return False
    has_verb = any(v in folded for v in _CHART_UPLOAD_VERBS)
    if has_verb:
        return True
    return any(
        p in folded
        for p in ("khong thay la so", "chua thay la so", "can anh la so", "can hinh la so")
    )


def _sentence_is_valid_palm_upload(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    if not any(h in folded for h in _PALM_OBJECT_HINTS):
        return False
    return any(v in folded for v in _CHART_UPLOAD_VERBS) or "gui" in folded


def _sentence_is_valid_face_upload(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    if not any(h in folded for h in _FACE_OBJECT_HINTS):
        return False
    return any(v in folded for v in _CHART_UPLOAD_VERBS) or "gui" in folded


def _birth_place_redirect(session: MessengerSession) -> str:
    missing = runtime_missing_birth_fields(session)
    if not missing:
        return (
            f"Thông tin địa lý mình không cần — {TIMEZONE_COPY}. "
            "Bạn có thể hỏi tiếp về lá số hiện tại nhé."
        )
    labels = labels_for_missing(session, limit=1)
    what = labels[0] if labels else "thông tin sinh còn thiếu"
    return (
        f"Thông tin địa lý mình không cần — {TIMEZONE_COPY}. "
        f"Bạn chỉ cần cho mình biết thêm {what} là được nhé."
    )


def _chart_upload_redirect(session: MessengerSession, *, allow_generate_teaser: bool) -> str:
    missing = runtime_missing_birth_fields(session)
    if not missing:
        if allow_generate_teaser and session.chart_json:
            return (
                "Mình có thể tự lập lá số từ thông tin sinh — không cần gửi ảnh lá số. "
                "Bạn hỏi tiếp về lá số hiện tại nhé!"
            )
        if allow_generate_teaser:
            return (
                "Mình có thể tự lập lá số từ thông tin sinh — không cần gửi ảnh lá số."
            )
        return (
            "Mình có thể tự lập lá số từ thông tin sinh — không cần gửi ảnh lá số. "
            "Bạn xác nhận thông tin sinh trước nhé."
        )
    labels = labels_for_missing(session, limit=3)
    return (
        "Mình có thể tự lập lá số từ thông tin sinh — không cần gửi ảnh lá số. "
        f"Bạn bổ sung: {', '.join(labels)} nhé."
    )


def _sanitize_birth_place_sentences(
    reply: str,
    session: MessengerSession,
) -> SanitizedReply:
    sentences = _split_sentences(reply)
    if not sentences:
        return SanitizedReply(reply, False, None)
    changed = False
    out: list[str] = []
    for s in sentences:
        if _sentence_mentions_birth_place(s):
            out.append(_birth_place_redirect(session))
            changed = True
        else:
            out.append(s)
    if not changed:
        return SanitizedReply(reply, False, None)
    return SanitizedReply(_join_sentences(out), True, "birth_place_request")


def _multimodal_off_redirect(session: MessengerSession) -> str:
    if session.chart_json:
        return "Hiện mình chỉ hỗ trợ hỏi tử vi qua tin nhắn. Bạn hỏi tiếp về lá số nhé!"
    missing = runtime_missing_birth_fields(session)
    if missing:
        labels = labels_for_missing(session, limit=2)
        return (
            "Hiện mình chỉ hỗ trợ tử vi qua tin nhắn và lá số từ ngày sinh. "
            f"Bạn cho mình {', '.join(labels)} nhé."
        )
    return (
        "Hiện mình chỉ hỗ trợ tử vi qua tin nhắn và lá số từ ngày sinh. "
        "Bạn có thể hỏi tử vi hoặc cung cấp ngày sinh nhé."
    )


def _sentence_promotes_multimodal(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    return any(kw in folded for kw in _MULTIMODAL_PROMO_KEYWORDS)


def _sanitize_multimodal_promo_sentences(
    reply: str,
    session: MessengerSession,
) -> SanitizedReply:
    if is_multimodal_enabled():
        return SanitizedReply(reply, False, None)
    sentences = _split_sentences(reply)
    if not sentences:
        return SanitizedReply(reply, False, None)
    changed = False
    out: list[str] = []
    for s in sentences:
        if (
            _sentence_is_valid_palm_upload(s)
            or _sentence_is_valid_face_upload(s)
            or _sentence_promotes_multimodal(s)
        ):
            out.append(_multimodal_off_redirect(session))
            changed = True
        else:
            out.append(s)
    if not changed:
        return SanitizedReply(reply, False, None)
    return SanitizedReply(_join_sentences(out), True, "multimodal_promo_stripped")


def _sanitize_chart_upload_sentences(
    reply: str,
    session: MessengerSession,
    *,
    allow_generate_teaser: bool,
) -> SanitizedReply:
    sentences = _split_sentences(reply)
    if not sentences:
        return SanitizedReply(reply, False, None)
    changed = False
    out: list[str] = []
    for s in sentences:
        if _sentence_requests_chart_upload(s):
            if not is_multimodal_enabled() and (
                _sentence_is_valid_palm_upload(s) or _sentence_is_valid_face_upload(s)
            ):
                out.append(_multimodal_off_redirect(session))
                changed = True
            elif _sentence_is_valid_palm_upload(s) or _sentence_is_valid_face_upload(s):
                out.append(s)
                continue
            else:
                out.append(_chart_upload_redirect(session, allow_generate_teaser=allow_generate_teaser))
                changed = True
        else:
            out.append(s)
    if not changed:
        return SanitizedReply(reply, False, None)
    return SanitizedReply(_join_sentences(out), True, "chart_upload_request")


def _sanitize_generate_promise(reply: str, *, allow_generate_teaser: bool) -> SanitizedReply:
    if allow_generate_teaser:
        return SanitizedReply(reply, False, None)
    folded = _fold_vi(reply)
    if not any(kw in folded for kw in _GENERATE_PROMISE_KEYWORDS):
        return SanitizedReply(reply, False, None)
    return SanitizedReply(
        "Mình cần bạn xác nhận thông tin sinh trước khi lập lá số nhé.",
        True,
        "generate_veto",
    )


def _sentence_mentions_forbidden_intake_field(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    if "phut sinh" in folded or "thang nhuan" in folded:
        return True
    if "ho va ten" in folded or "ten day du" in folded:
        return True
    # Calendar choice — forbidden; stating "dương lịch" alone is OK.
    if "am lich" in folded and "duong lich" in folded:
        return True
    if "am lich hay" in folded or "hay am lich" in folded:
        return True
    if "lich am" in folded and ("hay" in folded or "?" in sentence):
        return True
    return False


def _sanitize_forbidden_field_questions(reply: str) -> SanitizedReply:
    sentences = _split_sentences(reply)
    if not sentences:
        return SanitizedReply(reply, False, None)
    kept: list[str] = []
    removed = False
    for s in sentences:
        if _sentence_mentions_forbidden_intake_field(s):
            removed = True
            continue
        kept.append(s)
    if not removed:
        return SanitizedReply(reply, False, None)
    if kept:
        return SanitizedReply(
            f"{_join_sentences(kept)} {FORBIDDEN_INTAKE_REDIRECT}",
            True,
            "forbidden_field_stripped",
        )
    return SanitizedReply(FORBIDDEN_INTAKE_REDIRECT, True, "forbidden_field_all_stripped")


_HANDOFF_ACTION_CLAIM = re.compile(
    r"(?:"
    r"m[iì]nh\s+(?:da|đã)\s+(?:chuy[eể]n|k[eế]t\s+n[oố]i)"
    r"|(?:da|đã)\s+chuy[eể]n\s+(?:b[aạ]n|y[eê]u\s+c[aầ]u)"
    r"|(?:d[oộ]i|team)\s+tri\s+[aâ]m\s+(?:s[eẽ]|da|đã)\s+(?:ti[eế]p|nh[aậ]n|trao\s+doi)"
    r"|t[uừ]\s+(?:day|đ[âa]y)\s+tr[oợ]\s+l[yý]\s+(?:s[eẽ]\s+)?(?:t[aạ]m\s+d[uừ]ng|d[uừ]ng)"
    r"|trong\s+l[uú]c\s+ch[oờ]\s+(?:d[oộ]i|m[iì]nh)"
    r")",
    re.IGNORECASE,
)


def _sentence_has_handoff_action_claim(sentence: str) -> bool:
    folded = _fold_vi(sentence)
    if _HANDOFF_ACTION_CLAIM.search(sentence):
        return True
    if _HANDOFF_ACTION_CLAIM.search(folded):
        return True
    return False


def _sanitize_handoff_action_claims(
    reply: str,
    *,
    handoff_committed: bool,
) -> SanitizedReply:
    """Strip GPT prose that claims backend handoff succeeded without orchestrator ack."""
    if handoff_committed or not (reply or "").strip():
        return SanitizedReply(reply, False, None)
    sentences = _split_sentences(reply)
    if not sentences:
        return SanitizedReply(reply, False, None)
    kept: list[str] = []
    removed = False
    for s in sentences:
        if _sentence_has_handoff_action_claim(s):
            removed = True
            continue
        kept.append(s)
    if not removed:
        return SanitizedReply(reply, False, None)
    logger.info("semantic_action_promise_blocked")
    if kept:
        return SanitizedReply(_join_sentences(kept), True, "handoff_action_claim_stripped")
    return SanitizedReply("", True, "handoff_action_claim_all_stripped")


def sanitize_outbound_reply(
    reply: str,
    session: MessengerSession,
    *,
    allow_generate_teaser: bool = True,
    handoff_committed: bool = False,
) -> SanitizedReply:
    """Apply context-aware outbound sanitizers without calling GPT."""
    current = reply
    any_changed = False
    last_reason: str | None = None

    for step in (
        lambda t: _sanitize_handoff_action_claims(t, handoff_committed=handoff_committed),
        _sanitize_internal_source_sentences,
        lambda t: _sanitize_multimodal_promo_sentences(t, session),
        lambda t: _sanitize_birth_place_sentences(t, session),
        lambda t: _sanitize_chart_upload_sentences(
            t, session, allow_generate_teaser=allow_generate_teaser,
        ),
        lambda t: _sanitize_generate_promise(t, allow_generate_teaser=allow_generate_teaser),
        _sanitize_forbidden_field_questions,
    ):
        result = step(current)
        current = result.text
        if result.changed:
            any_changed = True
            last_reason = result.reason
            logger.info("sanitizer_applied reason=%s", result.reason)

    return SanitizedReply(current, any_changed, last_reason)
