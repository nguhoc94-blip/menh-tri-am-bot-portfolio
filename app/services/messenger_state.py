from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def new_generation_id() -> str:
    return str(uuid.uuid4())


class ConversationState(str, Enum):
    # Active states for conversational bridge MVP
    CHATTING = "CHATTING"    # no birth data yet, or general chat
    HAS_CHART = "HAS_CHART"  # chart generated; follow-up mode

    # Payment / manual-verify seam (Nhịp 1)
    CHECKOUT = "CHECKOUT"
    PAYMENT_VERIFYING = "PAYMENT_VERIFYING"
    PAID_GENERATING = "PAID_GENERATING"
    PAID_READY = "PAID_READY"
    SUPPORT_HANDOFF = "SUPPORT_HANDOFF"

    # V9 multimodal states — additive, do NOT remove legacy states above
    INTAKE_PALM = "INTAKE_PALM"               # waiting for palm image
    INTAKE_FACE = "INTAKE_FACE"               # consent given, waiting for face image
    IMAGE_VALIDATING = "IMAGE_VALIDATING"     # async validation in progress
    ANALYZING = "ANALYZING"                   # GPT/model call in queue
    RENDERING = "RENDERING"                   # render job in queue
    DELIVERING = "DELIVERING"                 # send_asset job in queue
    DONATE_REPORTED = "DONATE_REPORTED"       # user reported transfer, await admin verify
    DONATE_VERIFIED = "DONATE_VERIFIED"       # admin confirmed donation
    ABUSE_SOFT_LIMIT = "ABUSE_SOFT_LIMIT"     # cost/abuse threshold hit, serving short version

    # Legacy states kept for DB backward-compat migration only.
    # New sessions are never created in these states.
    NEW = "NEW"
    WAIT_FULL_NAME = "WAIT_FULL_NAME"
    WAIT_BIRTH_DAY = "WAIT_BIRTH_DAY"
    WAIT_BIRTH_MONTH = "WAIT_BIRTH_MONTH"
    WAIT_BIRTH_YEAR = "WAIT_BIRTH_YEAR"
    WAIT_BIRTH_HOUR = "WAIT_BIRTH_HOUR"
    WAIT_BIRTH_MINUTE = "WAIT_BIRTH_MINUTE"
    WAIT_GENDER = "WAIT_GENDER"
    WAIT_CALENDAR_TYPE = "WAIT_CALENDAR_TYPE"
    WAIT_IS_LEAP_LUNAR_MONTH = "WAIT_IS_LEAP_LUNAR_MONTH"
    READY_TO_GENERATE = "READY_TO_GENERATE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# Any legacy state that should be treated as CHATTING when loading from DB
LEGACY_CHATTING_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.NEW,
        ConversationState.WAIT_FULL_NAME,
        ConversationState.WAIT_BIRTH_DAY,
        ConversationState.WAIT_BIRTH_MONTH,
        ConversationState.WAIT_BIRTH_YEAR,
        ConversationState.WAIT_BIRTH_HOUR,
        ConversationState.WAIT_BIRTH_MINUTE,
        ConversationState.WAIT_GENDER,
        ConversationState.WAIT_CALENDAR_TYPE,
        ConversationState.WAIT_IS_LEAP_LUNAR_MONTH,
        ConversationState.READY_TO_GENERATE,
        ConversationState.COMPLETED,
        ConversationState.CANCELLED,
    }
)

SESSION_RESET_COMMAND = "REDACTED_RESET_CODE"
RESET_COMMANDS: frozenset[str] = frozenset({SESSION_RESET_COMMAND})

# Fields required to attempt chart generation (legacy — kept for is_birth_complete())
REQUIRED_BIRTH_FIELDS: list[str] = [
    "full_name",
    "birth_day",
    "birth_month",
    "birth_year",
    "birth_hour",
    "birth_minute",
    "gender",
    "calendar_type",
]

# Hard required: cannot generate at all without these
HARD_BIRTH_FIELDS: tuple[str, ...] = ("birth_day", "birth_month", "birth_year")

# Soft required: can assume defaults for provisional reading
SOFT_BIRTH_FIELDS: tuple[str, ...] = (
    "full_name",
    "birth_hour",
    "birth_minute",
    "gender",
    "calendar_type",
)

# Max chat turns kept in session — controls context window sent to OpenAI
MAX_HISTORY = 24  # ~12 user-assistant pairs retained in session

# Human-readable labels for targeted intake questions
FIELD_QUESTION: dict[str, str] = {
    "full_name": "Bạn cho mình biết họ và tên nhé?",
    "birth_day": "Ngày sinh của bạn là ngày mấy (1–31)?",
    "birth_month": "Tháng sinh là tháng mấy (1–12)?",
    "birth_year": "Năm sinh là năm nào?",
    "birth_hour": (
        "Giờ sinh của bạn là mấy giờ? "
        "Bạn có thể nhập theo giờ số (ví dụ 19h30) hoặc theo giờ địa chi "
        "(ví dụ giờ Tuất, giờ Dậu)."
    ),
    "birth_minute": "Phút sinh là phút mấy (0–59)?",
    "gender": "Giới tính của bạn là nam hay nữ?",
    "calendar_type": "Ngày sinh theo lịch dương (solar) hay âm (lunar)?",
    "is_leap_lunar_month": "Tháng âm của bạn có phải tháng nhuận không? (có/không)",
}


@dataclass
class MessengerSession:
    sender_id: str
    state: ConversationState = ConversationState.CHATTING
    birth_data: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)
    chart_json: dict[str, Any] | None = None
    reading_id: int | None = None
    order_id: int | None = None
    routing: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    # V9 multimodal fields — persisted via migration 025
    palm_asset_ids: list[str] = field(default_factory=list)
    face_asset_ids: list[str] = field(default_factory=list)
    face_consent_given: bool = False
    active_mode: str | None = None          # DEMO | palm | face | combined
    image_retry_count: int = 0
    session_model_calls: int = 0
    cohort_label: str = "new"               # new | supporter | repeat | high_cost | abuse_risk
    abuse_flag_count: int = 0
    admin_granted_combined: bool = False
    generation_id: str = field(default_factory=new_generation_id)

    def bump_generation(self, reason: str = "") -> str:
        """Invalidate in-flight async jobs by issuing a new generation token."""
        self.generation_id = new_generation_id()
        return self.generation_id

    def missing_birth_fields(self) -> list[str]:
        """Return required fields not yet present in birth_data.

        birth_hour_branch in birth_data counts as having birth_hour.
        """
        missing: list[str] = []
        for f in REQUIRED_BIRTH_FIELDS:
            if f not in self.birth_data:
                # birth_hour_branch stored by branch-time parser satisfies birth_hour
                if f == "birth_hour" and "birth_hour_branch" in self.birth_data:
                    continue
                missing.append(f)
        if (
            "is_leap_lunar_month" not in self.birth_data
            and self.birth_data.get("calendar_type") == "lunar"
        ):
            missing.append("is_leap_lunar_month")
        return missing

    def missing_hard_birth_fields(self) -> list[str]:
        """Return hard-required fields (day/month/year) not yet in birth_data."""
        return [f for f in HARD_BIRTH_FIELDS if f not in self.birth_data]

    def missing_soft_birth_fields(self) -> list[str]:
        """Return soft-required fields not yet in birth_data."""
        missing: list[str] = []
        for f in SOFT_BIRTH_FIELDS:
            if f not in self.birth_data:
                # birth_hour_branch satisfies birth_hour
                if f == "birth_hour" and "birth_hour_branch" in self.birth_data:
                    continue
                missing.append(f)
        return missing

    def has_minimum_birth_for_provisional_reading(self) -> bool:
        """True when all hard fields (day/month/year) are present.

        Soft fields can be assumed with defaults for a provisional reading.
        """
        return len(self.missing_hard_birth_fields()) == 0

    def has_any_birth_data(self) -> bool:
        return bool(self.birth_data)

    def has_partial_intake(self) -> bool:
        """Intake dở: có birth nhưng chưa đủ và chưa có lá số."""
        return (
            self.has_any_birth_data()
            and not self.is_birth_complete()
            and not self.chart_json
        )

    def is_birth_complete(self) -> bool:
        return len(self.missing_birth_fields()) == 0

    def add_turn(self, role: str, content: str) -> None:
        """Append a turn and trim history to MAX_HISTORY entries."""
        self.history.append({"role": role, "content": content})
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
