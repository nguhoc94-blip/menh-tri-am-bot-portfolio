"""Runtime intake policy — fields the bot may ask users to provide."""

from __future__ import annotations

from app.services.messenger_state import MessengerSession

# Source-of-truth: only these may appear in outbound intake questions.
USER_REQUESTABLE_BIRTH_FIELDS: tuple[str, ...] = (
    "birth_day",
    "birth_month",
    "birth_year",
    "birth_hour",
    "gender",
)

_FIELD_LABELS: dict[str, str] = {
    "birth_day": "ngày sinh",
    "birth_month": "tháng sinh",
    "birth_year": "năm sinh",
    "birth_hour": "giờ sinh (ví dụ: 19h30, hoặc theo địa chi như giờ Tuất, giờ Dậu)",
    "gender": "giới tính (nam hay nữ)",
}

TIMEZONE_COPY = "hệ thống dùng múi giờ UTC+7 cố định"

SOLAR_CALENDAR_COPY = "dương lịch (UTC+7)"

INTAKE_FIELDS_SHORT = (
    "ngày/tháng/năm sinh (dương lịch), giờ sinh (hoặc giờ địa chi) và giới tính"
)

FORBIDDEN_INTAKE_REDIRECT = (
    f"Mình chỉ cần {INTAKE_FIELDS_SHORT} là đủ nhé. "
    f"Hệ thống luôn dùng {SOLAR_CALENDAR_COPY}, không cần chọn âm hay dương lịch."
)

SESSION_RESET_HINT = ""


def append_session_reset_hint(text: str) -> str:
    """Return copy unchanged — reset hint no longer shown to end users."""
    return (text or "").rstrip()


INTAKE_OPENING_FALLBACK = append_session_reset_hint(
    "Chào bạn, mình là Demo Bot — trợ lý luận giải Tử Vi qua Messenger. "
    f"Khi bạn muốn xem lá số, cho mình {INTAKE_FIELDS_SHORT} nhé."
)


def build_intake_context_for_gpt(
    session: MessengerSession,
    *,
    opening_already_sent: bool = False,
) -> str:
    """Prompt block for unified chat — aligned with runtime intake policy."""
    missing = runtime_missing_birth_fields(session)
    if session.birth_data and missing:
        missing_labels = [label_for_field(f) for f in missing]
        return (
            f"THÔNG TIN NGÀY SINH ĐÃ CÓ: {session.birth_data}\n"
            f"THÔNG TIN CÒN THIẾU: {', '.join(missing_labels)}\n\n"
            "→ Hỏi tự nhiên, tối đa 1–2 thứ mỗi lượt.\n"
            f"→ Luôn ghi rõ {SOLAR_CALENDAR_COPY} khi nhắc ngày sinh.\n"
            "→ KHÔNG hỏi họ tên, nơi sinh, âm/dương lịch, phút sinh."
        )
    if not session.birth_data:
        if opening_already_sent:
            return (
                "Bot đã gửi lời chào/giới thiệu mở đầu.\n"
                "→ Nếu user chỉ chào/xã giao: chào lại ấm, giới thiệu Demo Bot, "
                "KHÔNG liệt kê form thông tin.\n"
                "→ Nếu user hỏi muốn xem lá số / tử vi / 'làm gì': giải thích ngắn mục đích, "
                f"rồi hỏi {INTAKE_FIELDS_SHORT}.\n"
                f"→ Luôn dùng {SOLAR_CALENDAR_COPY}; KHÔNG hỏi chọn âm hay dương lịch.\n"
                "→ KHÔNG hỏi họ tên, nơi sinh, phút sinh."
            )
        return (
            "Người dùng chưa cung cấp thông tin ngày sinh.\n"
            "→ Nếu chỉ chào/xã giao: chào lại, giới thiệu Demo Bot — chưa hỏi form.\n"
            f"→ Nếu hỏi về lá số/tử vi: hỏi nhẹ {INTAKE_FIELDS_SHORT}.\n"
            f"→ Luôn dùng {SOLAR_CALENDAR_COPY}; KHÔNG hỏi chọn âm hay dương lịch.\n"
            "→ KHÔNG hỏi họ tên, nơi sinh, phút sinh."
        )
    return ""


def runtime_missing_birth_fields(session: MessengerSession) -> list[str]:
    """Missing fields filtered to user-requestable runtime policy."""
    missing = session.missing_birth_fields()
    return [f for f in missing if f in USER_REQUESTABLE_BIRTH_FIELDS]


def label_for_field(field: str) -> str:
    return _FIELD_LABELS.get(field, field)


def labels_for_missing(session: MessengerSession, *, limit: int = 3) -> list[str]:
    return [label_for_field(f) for f in runtime_missing_birth_fields(session)[:limit]]


def is_runtime_birth_complete(session: MessengerSession) -> bool:
    """True when all user-facing intake fields are present (runtime policy)."""
    return len(runtime_missing_birth_fields(session)) == 0


def apply_generate_defaults(session: MessengerSession) -> None:
    """Fill engine-required fields not collected from users (solar-only MVP)."""
    if "full_name" not in session.birth_data:
        session.birth_data["full_name"] = "Bạn"
    if "calendar_type" not in session.birth_data:
        session.birth_data["calendar_type"] = "solar"
    if "birth_minute" not in session.birth_data and (
        "birth_hour" in session.birth_data or "birth_hour_branch" in session.birth_data
    ):
        session.birth_data["birth_minute"] = 0
