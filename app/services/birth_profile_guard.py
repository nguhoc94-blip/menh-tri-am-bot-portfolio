"""Validate extracted birth fields before merging into session profile."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.services.messenger_state import MessengerSession
from app.services.subject_detector import (
    SubjectDecision,
    detect_subject,
    fold_vi,
    has_birth_profile_signal,
    is_explicit_self_edit,
    is_general_hypothetical_question,
)
from app.services.profile_transition import apply_self_birth_correction, mark_profile_switch_pending
from app.utils.sender_hash import hash_sender_id

logger = logging.getLogger(__name__)

_GENDER_NEGATION = re.compile(
    r"khong\s+phai\s+(?:nam|nu|nữ)|khong\s+(?:nam|nu|nữ)",
    re.I,
)

_EXPLICIT_GENDER_SELF = (
    re.compile(r"\b(?:toi|minh)\s+(?:la|là)\s+(?:nam|nu|nữ)\b", re.I),
    re.compile(r"\bgioi\s+tinh\s+(?:nam|nu|nữ)\b", re.I),
    re.compile(r"\b(?:nam|nu|nữ)\s+(?:gioi|giới)\b", re.I),
)

_PRONOUN_GENDER_ONLY = re.compile(
    r"\b(?:^|\s)(?:anh|ong|ông|chi|chị|co|cô|chu|bac|bác|ba|bà)\b",
    re.I,
)

_OWNER_CLARIFICATION = (
    "Mình thấy bạn vừa nhắc thông tin sinh. "
    "Đây là thông tin của bạn hay của người khác? "
    "Cho mình biết rõ để mình xử lý đúng nhé."
)

_THIRD_PARTY_CLARIFICATION = (
    "Mình thấy bạn đang muốn xem cho người khác, không phải hồ sơ hiện tại. "
    "Bạn xác nhận giúp mình để chuyển sang hồ sơ phù hợp nhé."
)

_GENDER_CLARIFICATION = (
    "Bạn cho mình biết giới tính của bạn là nam hay nữ nhé?"
)


@dataclass(frozen=True)
class ProfileMergeOutcome:
    early_reply: str | None = None
    merged_fields: tuple[str, ...] = ()
    profile_action: str = "ignore"


def session_has_protected_profile(session: MessengerSession) -> bool:
    return bool(session.chart_json) or session.is_birth_complete() or session.has_any_birth_data()


def allow_gender_merge(
    text: str,
    *,
    missing_fields: set[str] | list[str],
    subject: str,
    extracted_gender: str | None,
) -> bool:
    """Backend guard — do not trust extractor gender alone."""
    if not extracted_gender:
        return True

    folded = fold_vi(text)
    if subject == "third_party":
        return False
    if subject == "unclear" and "gender" in missing_fields:
        return False
    if _GENDER_NEGATION.search(folded):
        return False

    missing = set(missing_fields)
    if missing == {"gender"} or missing.issubset({"gender"}):
        if folded.strip() in {"nam", "nu", "nữ", "male", "female"}:
            return True
        if extracted_gender == "male" and folded.strip() in {"nam", "male"}:
            return True
        if extracted_gender == "female" and folded.strip() in {"nu", "nữ", "female"}:
            return True

    if any(p.search(text) for p in _EXPLICIT_GENDER_SELF):
        return True

    if _PRONOUN_GENDER_ONLY.search(text) and not any(p.search(text) for p in _EXPLICIT_GENDER_SELF):
        return False

    if extracted_gender == "male" and re.search(r"\b(?:nam|male)\b", folded):
        return True
    if extracted_gender == "female" and re.search(r"\b(?:nu|nữ|female)\b", folded):
        return True

    return False


def _filter_gender(extracted: dict[str, Any], text: str, decision: SubjectDecision, session: MessengerSession) -> dict[str, Any]:
    if "gender" not in extracted:
        return extracted
    missing = set(session.missing_birth_fields())
    if allow_gender_merge(
        text,
        missing_fields=missing,
        subject=decision.subject,
        extracted_gender=str(extracted.get("gender")),
    ):
        return extracted
    out = dict(extracted)
    out.pop("gender", None)
    return out


def _log_profile_action(
    *,
    sender_id: str,
    request_id: str,
    decision: SubjectDecision,
    profile_action: str,
    has_existing_profile: bool,
    pending_fields_count: int,
) -> None:
    logger.info(
        "profile_merge request_id=%s sender_id_hash=%s subject=%s subject_reason=%s "
        "profile_action=%s has_existing_profile=%s pending_fields_count=%d",
        request_id,
        hash_sender_id(sender_id),
        decision.subject,
        decision.reason,
        profile_action,
        has_existing_profile,
        pending_fields_count,
    )


def apply_birth_extraction_to_session(
    session: MessengerSession,
    text: str,
    extracted: dict[str, Any],
    *,
    sender_id: str,
    request_id: str,
    subject_decision: SubjectDecision | None = None,
) -> ProfileMergeOutcome:
    """Extract-then-validate-then-merge. Never merges third-party into birth_data."""
    if not extracted:
        return ProfileMergeOutcome(profile_action="ignore")

    decision = subject_decision or detect_subject(text, session=session)
    filtered = _filter_gender(extracted, text, decision, session)
    if not filtered:
        if "gender" in extracted and not allow_gender_merge(
            text,
            missing_fields=set(session.missing_birth_fields()),
            subject=decision.subject,
            extracted_gender=str(extracted.get("gender")),
        ):
            _log_profile_action(
                sender_id=sender_id,
                request_id=request_id,
                decision=decision,
                profile_action="clarify",
                has_existing_profile=session_has_protected_profile(session),
                pending_fields_count=0,
            )
            return ProfileMergeOutcome(
                early_reply=_GENDER_CLARIFICATION,
                profile_action="clarify",
            )
        return ProfileMergeOutcome(profile_action="ignore")

    has_profile = session_has_protected_profile(session)

    if is_general_hypothetical_question(text) and has_profile:
        _log_profile_action(
            sender_id=sender_id,
            request_id=request_id,
            decision=decision,
            profile_action="ignore",
            has_existing_profile=True,
            pending_fields_count=0,
        )
        return ProfileMergeOutcome(profile_action="ignore")

    if decision.subject == "third_party" and has_birth_profile_signal(text):
        routing = session.routing if isinstance(session.routing, dict) else {}
        routing["pending_profile_birth"] = dict(filtered)
        mark_profile_switch_pending(session)
        session.routing = routing
        _log_profile_action(
            sender_id=sender_id,
            request_id=request_id,
            decision=decision,
            profile_action="pending",
            has_existing_profile=has_profile,
            pending_fields_count=len(filtered),
        )
        return ProfileMergeOutcome(
            early_reply=_THIRD_PARTY_CLARIFICATION,
            profile_action="pending",
        )

    if decision.subject == "unclear" and has_profile and has_birth_profile_signal(text):
        routing = session.routing if isinstance(session.routing, dict) else {}
        routing["pending_profile_birth"] = dict(filtered)
        mark_profile_switch_pending(session)
        session.routing = routing
        _log_profile_action(
            sender_id=sender_id,
            request_id=request_id,
            decision=decision,
            profile_action="clarify",
            has_existing_profile=True,
            pending_fields_count=len(filtered),
        )
        return ProfileMergeOutcome(
            early_reply=_OWNER_CLARIFICATION,
            profile_action="clarify",
        )

    if has_profile and session.chart_json and not is_explicit_self_edit(text):
        if decision.subject != "self" or not is_explicit_self_edit(text):
            if has_birth_profile_signal(text):
                routing = session.routing if isinstance(session.routing, dict) else {}
                routing["pending_profile_birth"] = dict(filtered)
                mark_profile_switch_pending(session)
                session.routing = routing
                _log_profile_action(
                    sender_id=sender_id,
                    request_id=request_id,
                    decision=decision,
                    profile_action="pending",
                    has_existing_profile=True,
                    pending_fields_count=len(filtered),
                )
                return ProfileMergeOutcome(
                    early_reply=_THIRD_PARTY_CLARIFICATION,
                    profile_action="pending",
                )

    if decision.subject == "third_party" and not has_birth_profile_signal(text):
        _log_profile_action(
            sender_id=sender_id,
            request_id=request_id,
            decision=decision,
            profile_action="ignore",
            has_existing_profile=has_profile,
            pending_fields_count=0,
        )
        return ProfileMergeOutcome(profile_action="ignore")

    from app.services.delivery_progress import birth_date_change_blocked, birth_date_locked_reply

    if birth_date_change_blocked(session, filtered):
        _log_profile_action(
            sender_id=sender_id,
            request_id=request_id,
            decision=decision,
            profile_action="birth_date_locked",
            has_existing_profile=has_profile,
            pending_fields_count=len(filtered),
        )
        return ProfileMergeOutcome(
            early_reply=birth_date_locked_reply(),
            profile_action="birth_date_locked",
        )

    if is_explicit_self_edit(text) and session.chart_json:
        apply_self_birth_correction(session, filtered, reason="explicit_self_edit")
    else:
        session.birth_data.update(filtered)
    _log_profile_action(
        sender_id=sender_id,
        request_id=request_id,
        decision=decision,
        profile_action="merge",
        has_existing_profile=has_profile,
        pending_fields_count=0,
    )
    return ProfileMergeOutcome(
        merged_fields=tuple(filtered.keys()),
        profile_action="merge",
    )
