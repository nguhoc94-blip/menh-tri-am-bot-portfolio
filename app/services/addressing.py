"""Age- and gender-based addressing rules for Demo Bot GPT chat."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.services.messenger_state import MessengerSession

_DEFAULT_BOT_BIRTH_YEAR = 1995
_ELDER_RESPECT_GAP = 16


@dataclass(frozen=True)
class AddressingRules:
    """How the bot should address the user and refer to itself."""

    user_honorific: str  # bạn | anh | chị | chú | cô
    bot_self: str  # mình | em | cháu
    tier: str  # peer | senior_near | senior_elder | unknown
    age_gap_years: int | None  # bot_birth_year - user_birth_year (positive = user older)


def bot_birth_year() -> int:
    raw = (os.environ.get("demo.bot_BOT_BIRTH_YEAR") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_BOT_BIRTH_YEAR


def elder_respect_gap() -> int:
    raw = (os.environ.get("demo.bot_ELDER_RESPECT_GAP") or "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _ELDER_RESPECT_GAP


def compute_addressing(
    *,
    user_birth_year: int | None,
    user_gender: str | None,
    bot_year: int | None = None,
    respect_gap: int | None = None,
) -> AddressingRules:
    """
    Derive xưng hô from birth-year gap vs bot (1995) and user gender.

    - User same age or younger (gap <= 0): bạn / mình
    - User 1..16 years older: anh|chị / em
    - User >16 years older: chú|cô / cháu
    """
    bot_y = bot_year if bot_year is not None else bot_birth_year()
    gap_limit = respect_gap if respect_gap is not None else elder_respect_gap()

    if user_birth_year is None:
        return AddressingRules("bạn", "mình", "unknown", None)

    try:
        user_y = int(user_birth_year)
    except (TypeError, ValueError):
        return AddressingRules("bạn", "mình", "unknown", None)

    gap = bot_y - user_y  # positive when user is older

    if gap <= 0:
        return AddressingRules("bạn", "mình", "peer", gap)

    if gap > gap_limit:
        if user_gender == "male":
            return AddressingRules("chú", "cháu", "senior_elder", gap)
        if user_gender == "female":
            return AddressingRules("cô", "cháu", "senior_elder", gap)
        return AddressingRules("bạn", "mình", "senior_elder", gap)

    if user_gender == "male":
        return AddressingRules("anh", "em", "senior_near", gap)
    if user_gender == "female":
        return AddressingRules("chị", "em", "senior_near", gap)
    return AddressingRules("bạn", "mình", "senior_near", gap)


def addressing_from_birth_data(birth_data: dict[str, Any] | None) -> AddressingRules:
    b = birth_data or {}
    return compute_addressing(
        user_birth_year=b.get("birth_year"),
        user_gender=b.get("gender"),
    )


def _as_input_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {}


def birth_data_from_chart(chart_json: dict[str, Any] | None) -> dict[str, Any]:
    """Extract birth_year/gender from chart_json.input_normalized for xưng hô."""
    chart = chart_json or {}
    inp = _as_input_dict(chart.get("input_normalized"))
    return {
        "birth_year": inp.get("birth_year"),
        "gender": inp.get("gender"),
    }


def format_addressing_context(rules: AddressingRules) -> str:
    """Prompt block instructing GPT how to xưng hô with this user."""
    bot_y = bot_birth_year()
    lines = [
        "QUY TẮC XƯNG HÔ (BẮT BUỘC — Demo Bot sinh năm "
        f"{bot_y}, tính chênh lệch qua năm sinh người dùng):",
        f"- Gọi người dùng: \"{rules.user_honorific}\"",
        f"- Bot tự xưng: \"{rules.bot_self}\"",
        "- LUÔN giữ nhất quán xưng hô này trong mọi câu trả lời chat; "
        "không đổi sang bạn/anh/chị/cô/chú khác trừ khi user yêu cầu rõ.",
    ]

    if rules.age_gap_years is not None:
        lines.append(
            f"- Chênh lệch năm sinh (bot − user): {rules.age_gap_years} "
            f"(dương = user lớn tuổi hơn bot)."
        )

    if rules.tier == "unknown":
        lines.append(
            "- Chưa có năm sinh — tạm dùng bạn/mình cho đến khi có đủ ngày sinh."
        )
    elif rules.tier == "peer":
        lines.append("- User bằng tuổi hoặc trẻ hơn bot → bạn / mình.")
    elif rules.tier == "senior_near":
        lines.append(
            f"- User hơn bot từ 1 đến {elder_respect_gap()} tuổi → "
            f"{rules.user_honorific} / {rules.bot_self}."
        )
    elif rules.tier == "senior_elder":
        lines.append(
            f"- User hơn bot trên {elder_respect_gap()} tuổi → "
            f"{rules.user_honorific} / {rules.bot_self}."
        )

    if rules.tier in ("senior_near", "senior_elder") and rules.user_honorific == "bạn":
        lines.append(
            "- Chưa rõ giới tính user — tạm bạn/mình; ưu tiên hỏi xác nhận giới tính "
            "nếu cần xưng hô lịch sự hơn."
        )

    return "\n".join(lines)


def build_addressing_context(session: MessengerSession) -> str:
    rules = addressing_from_birth_data(session.birth_data)
    return format_addressing_context(rules)


def build_addressing_context_from_chart(chart_json: dict[str, Any] | None) -> str:
    rules = addressing_from_birth_data(birth_data_from_chart(chart_json))
    return format_addressing_context(rules)


def inject_addressing_context(template: str, chart_json: dict[str, Any] | None) -> str:
    """Replace {{ADDRESSING_CONTEXT}} or append addressing block for full readings."""
    ctx = build_addressing_context_from_chart(chart_json)
    if "{{ADDRESSING_CONTEXT}}" in template:
        return template.replace("{{ADDRESSING_CONTEXT}}", ctx)
    if ctx.strip():
        return template.rstrip() + "\n\n" + ctx
    return template
