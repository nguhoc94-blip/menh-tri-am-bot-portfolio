"""Lát 3 — KB-2 birthdata confirmation trước generate (CP3 wording)."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from openai import OpenAI

from app.services.app_config_store import get_config_text_first
from app.services.messenger_state import MessengerSession

logger = logging.getLogger(__name__)

_KB2_CLASSIFIER_MODEL_DEFAULT = "gpt-4o-mini"
_KB2_CLASSIFIER_TIMEOUT = 12.0
_KB2_CLASSIFIER_MAX_TOKENS = 16

_VALID_KB2_LABELS = frozenset({"CONFIRM", "EDIT", "ASK_IDENTITY", "ASK_OTHER", "UNCLEAR"})


def _fold(text: str) -> str:
    """Lowercase + strip Vietnamese diacritics for fuzzy matching."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", text.lower())
    s = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d")


def format_birth_summary(session: MessengerSession) -> str:
    b = session.birth_data
    parts: list[str] = []
    if b.get("full_name") and b["full_name"] != "Bạn":
        parts.append(f"Họ tên: {b['full_name']}")
    dm = (b.get("birth_day"), b.get("birth_month"), b.get("birth_year"))
    if all(dm):
        parts.append(f"Ngày sinh: {dm[0]}/{dm[1]}/{dm[2]}")
    # Display branch-hour differently from numeric hour
    if b.get("birth_hour_display"):
        # User entered via earthly branch (e.g. giờ Tuất)
        hour = b.get("birth_hour")
        end_hour = (hour + 2) % 24 if hour is not None else None
        if hour is not None and end_hour is not None:
            parts.append(
                f"Giờ sinh: {b['birth_hour_display']}, khoảng {hour:02d}:00–{end_hour:02d}:59"
            )
        else:
            parts.append(f"Giờ sinh: {b['birth_hour_display']}")
    else:
        hm = (b.get("birth_hour"), b.get("birth_minute"))
        if all(x is not None for x in hm):
            parts.append(f"Giờ sinh: {hm[0]}h{hm[1]:02d}")
    g = b.get("gender")
    if g:
        parts.append("Giới tính: nam" if g == "male" else "Giới tính: nữ")
    ct = b.get("calendar_type")
    if ct:
        parts.append("Lịch: dương" if ct == "solar" else "Lịch: âm")
    if b.get("calendar_type") == "lunar" and "is_leap_lunar_month" in b:
        parts.append("Tháng nhuận: có" if b["is_leap_lunar_month"] else "Tháng nhuận: không")
    # Indicate provisional assumptions if any
    assumptions = (session.routing or {}).get("birth_assumptions")
    if assumptions:
        parts.append("(bản luận tạm — có giả định một số giá trị mặc định)")
    return "; ".join(parts) if parts else "(chưa đủ để hiển thị)"


_KB2_YES = frozenset(
    {
        "đúng rồi",
        "dung roi",
        "ok",
        "yes",
        "chính xác",
        "chinh xac",
        "xác nhận",
        "xac nhan",
        "chuẩn",
        "chuan",
        "chuẩn rồi",
        "chuan roi",
        "chuẩn đét",
        "chuan det",
        "chuẩn đét rồi xem đi",
        "chuan det roi xem di",
        "ok rồi",
        "ok roi",
        "ok rồi xem đi",
        "ok roi xem di",
        "ok rồi xem tiếp đi",
        "ok roi xem tiep di",
        "đúng hết",
        "dung het",
        "đúng rồi xem đi",
        "dung roi xem di",
        "xem đi",
        "xem di",
        "xem tiếp đi",
        "xem tiep di",
        "làm luôn",
        "lam luon",
        "triển luôn",
        "trien luon",
        "luận giải luôn",
        "luan giai luon",
        "bắt đầu đi",
        "bat dau di",
        "được rồi",
        "duoc roi",
        "ổn rồi",
        "on roi",
        "ổn áp rồi",
        "on ap roi",
    }
)

_NEGATIVE_CONFIRM_HINTS = (
    "khong dung",
    "chua dung",
    "khong ok",
    "chua ok",
    "khong chuan",
    "chua chuan",
    "con phan van",
    "de xem",
    "xem lai",
    "chuan bi",
)

_CONFIRM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:^|\s)chu[aâ]n(?!\s+b[iị])(\s+det|\s+d[eé]t)?(\s+r[oô]i)?(\s+xem(\s+di|\s+tiep)?)?",
        re.I,
    ),
    re.compile(r"(?:^|\s)ok(?:\s+r[oô]i)?(?:\s+xem(\s+di|\s+tiep)?)?(?:\s|$)", re.I),
    re.compile(
        r"(?:^|\s)(xem\s+(?:di|tiep|tiếp)|"
        r"lu[aâ]n\s*gi[aả]i\s+lu[oô]n|"
        r"l[aà]m\s+lu[oô]n|"
        r"tri[eê]n\s+lu[oô]n|"
        r"b[aắ]t\s*[dđ][aâ]u\s+di)(?:\s|$)",
        re.I,
    ),
    re.compile(
        r"(?:^|\s)(?:d[uư][oơ]c\s+r[oô]i|ổn(?:\s+[aá]p)?\s+r[oô]i)(?:\s|$)",
        re.I,
    ),
    re.compile(r"(?:^|\s)d[uư]ng(?:\s+r[oô]i|\s+h[eê]t)?(?:\s|$)", re.I),
    re.compile(r"(?:^|\s)ch[ií]nh\s*x[aá]c(?:\s|$)", re.I),
)

_PROCEED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:"
        r"xem\s+(?:di|tiep|tiếp)|"
        r"lu[aâ]n\s*gi[aả]i\s+lu[oô]n|"
        r"l[aà]m\s+lu[oô]n|"
        r"tri[eê]n\s+lu[oô]n|"
        r"b[aắ]t\s*[dđ][aâ]u\s+di|"
        r"ok\s+r[oô]i\s+xem(?:\s+di|\s+tiep)?|"
        r"chu[aâ]n(?:\s+det|\s+đ[eé]t)?\s+r[oô]i\s+xem\s+di"
        r")(?:\s|$)",
        re.I,
    ),
)


def is_kb2_confirm_text(text: str) -> bool:
    """Natural-language confirm / proceed intent for KB-2."""
    raw = text.strip()
    if not raw:
        return False
    folded = _fold(raw)
    if any(x in folded for x in _NEGATIVE_CONFIRM_HINTS):
        return False
    t = raw.lower()
    if t in _KB2_YES or folded in {_fold(x) for x in _KB2_YES}:
        return True
    for pat in _CONFIRM_PATTERNS:
        if pat.search(t) or pat.search(folded):
            return True
    return False


def is_kb2_proceed_intent(text: str) -> bool:
    """Proceed/generate intent — explicit only (e.g. 'xem đi', not 'xem tử vi giúp mình')."""
    raw = text.strip()
    if not raw:
        return False
    if is_kb2_edit_text(raw):
        return False
    t = raw.lower()
    folded = _fold(raw)
    proceed_only = (
        "xem đi",
        "xem di",
        "xem tiếp đi",
        "xem tiep di",
        "làm luôn",
        "lam luon",
        "triển luôn",
        "trien luon",
        "luận giải luôn",
        "luan giai luon",
        "bắt đầu đi",
        "bat dau di",
        "ok rồi xem đi",
        "ok roi xem di",
        "ok rồi xem tiếp đi",
        "ok roi xem tiep di",
        "chuẩn đét rồi xem đi",
        "chuan det roi xem di",
    )
    folded_proceed = {_fold(x) for x in proceed_only}
    if t in proceed_only or folded in folded_proceed:
        return True
    for pat in _PROCEED_PATTERNS:
        if pat.search(t) or pat.search(folded):
            return True
    return False


_KB2_EDIT = frozenset(
    {
        "sửa lại",
        "sua lai",
        "sai rồi",
        "sai roi",
        "nhập lại",
        "nhap lai",
        "sửa lại thông tin",
        "sua lai thong tin",
    }
)


def is_kb2_edit_text(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    if t in _KB2_EDIT:
        return True
    folded = _fold(text)
    if "sua" in folded and ("lai" in folded or "thong tin" in folded):
        return True
    if "sai" in folded and "roi" in folded:
        return True
    if "nhap lai" in folded or "doi ten" in folded or "doi gio" in folded:
        return True
    return "sửa" in t or "sua" in t


def build_confirmation_summary_message(session: MessengerSession) -> str:
    tmpl = get_config_text_first(
        "confirmation_birthdata_summary",
        default=(
            "Mình chốt lại thông tin để xem cho bạn chính xác hơn nhé: [tóm tắt dữ liệu]. "
            "Nếu đúng rồi mình xem tiếp, còn nếu chưa đúng bạn sửa lại giúp mình ở đây."
        ),
    )
    return tmpl.replace("[tóm tắt dữ liệu]", format_birth_summary(session))


def kb2_reminder_when_awaiting(session: MessengerSession) -> str:
    custom = get_config_text_first("confirmation_birthdata_soft_reminder", default="")
    if custom.strip():
        return custom.strip()
    return (
        "Mình hiểu rồi. Bạn muốn mình dùng thông tin vừa chốt để xem tiếp luôn, "
        "hay có điểm nào cần sửa trước không?"
    )


def _load_kb2_classifier_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "prompts" / "system_kb2_classifier.txt"
    return path.read_text(encoding="utf-8")


def classify_kb2_awaiting_reply_with_gpt(
    session: MessengerSession,
    text: str,
    *,
    request_id: str | None = None,
) -> str:
    """GPT fallback classifier — returns one of CONFIRM|EDIT|ASK_IDENTITY|ASK_OTHER|UNCLEAR."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return "UNCLEAR"
    model = (
        (os.environ.get("OPENAI_EXTRACTION_MODEL") or "").strip()
        or _KB2_CLASSIFIER_MODEL_DEFAULT
    )
    birth_summary = format_birth_summary(session)
    system_prompt = _load_kb2_classifier_prompt()
    user_content = (
        f"Thông tin đang chờ xác nhận:\n{birth_summary}\n\n"
        f"Tin nhắn người dùng:\n{text.strip()}"
    )
    try:
        client = OpenAI(api_key=api_key, timeout=_KB2_CLASSIFIER_TIMEOUT)
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=_KB2_CLASSIFIER_MAX_TOKENS,
        )
        raw = (completion.choices[0].message.content or "").strip().upper()
        raw = re.sub(r"[^A-Z_]", "", raw)
        if raw in _VALID_KB2_LABELS:
            logger.info(
                "kb2_classifier_ok request_id=%s label=%s event=kb2_classifier_ok",
                request_id,
                raw,
            )
            return raw
        logger.info(
            "kb2_classifier_invalid request_id=%s raw=%s event=kb2_classifier_invalid",
            request_id,
            raw[:40],
        )
        return "UNCLEAR"
    except Exception as e:
        logger.warning(
            "kb2_classifier_failed request_id=%s reason=%s event=kb2_classifier_failed",
            request_id,
            type(e).__name__,
        )
        return "UNCLEAR"
