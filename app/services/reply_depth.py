"""User-controlled reply depth — default detailed (standard), override on explicit request."""

from __future__ import annotations

import re
from typing import Any

from app.services.messenger_state import MessengerSession

REPLY_DEPTH_STANDARD = "standard"
REPLY_DEPTH_QUICK = "quick"
REPLY_DEPTH_DEEP = "deep"

_ROUTING_KEY = "reply_depth"

# Reset to default (detailed) — checked before quick/deep.
_STANDARD_RESET = re.compile(
    r"(?:"
    r"binh\s*thuong|bình\s*thường|"
    r"nhu\s*cu|như\s*cũ|"
    r"khong\s*can\s*ngan|không\s*cần\s*ngắn|"
    r"giai\s*thich\s*day\s*du|giải\s*thích\s*đầy\s*đủ|"
    r"noi\s*day\s*du|nói\s*đầy\s*đủ|"
    r"noi\s*dai|nói\s*dài|"
    r"khong\s*can\s*ngan\s*nua|không\s*cần\s*ngắn\s*nữa"
    r")",
    re.IGNORECASE,
)

_QUICK = re.compile(
    r"(?:"
    r"ngan\s*gon|ngắn\s*gọn|"
    r"ngan\s*thoi|ngắn\s*thôi|"
    r"tom\s*lai|tóm\s*lại|"
    r"nhanh\s*thoi|nhanh\s*thôi|"
    r"ban\s*lam|bận\s*lắm|ban\s*qua|bận\s*quá|"
    r"noi\s*thang|nói\s*thẳng|"
    r"dung\s*dai|đừng\s*dài|"
    r"van\s*tat|vắn\s*tắt|"
    r"gon\s*thoi|gọn\s*thôi"
    r")",
    re.IGNORECASE,
)

_DEEP = re.compile(
    r"(?:"
    r"giai\s*thich\s*ky|giải\s*thích\s*kỹ|"
    r"di\s*sau|đi\s*sâu|"
    r"phan\s*tich\s*chi\s*tiet|phân\s*tích\s*chi\s*tiết|"
    r"ky\s*hon|kỹ\s*hơn|"
    r"chi\s*tiet\s*hon|chi\s*tiết\s*hơn|"
    r"noi\s*ro|nói\s*rõ|"
    r"doc\s*ky|đọc\s*kỹ"
    r")",
    re.IGNORECASE,
)


def _fold_vi(text: str) -> str:
    from app.services.subject_detector import fold_vi

    return fold_vi(text or "")


def detect_reply_depth_signal(text: str) -> str | None:
    """Return explicit depth signal in this message, or None if user did not adjust."""
    raw = (text or "").strip()
    if not raw:
        return None
    folded = _fold_vi(raw)
    if _STANDARD_RESET.search(folded):
        return REPLY_DEPTH_STANDARD
    if _QUICK.search(folded):
        return REPLY_DEPTH_QUICK
    if _DEEP.search(folded):
        return REPLY_DEPTH_DEEP
    return None


def get_reply_depth(session: MessengerSession) -> str:
    rq = session.routing if isinstance(session.routing, dict) else {}
    depth = rq.get(_ROUTING_KEY)
    if depth in (REPLY_DEPTH_QUICK, REPLY_DEPTH_DEEP, REPLY_DEPTH_STANDARD):
        return str(depth)
    return REPLY_DEPTH_STANDARD


def apply_reply_depth_from_user_message(session: MessengerSession, text: str) -> str | None:
    """Update session.routing when user explicitly adjusts reply depth. Returns signal or None."""
    signal = detect_reply_depth_signal(text)
    if signal is None:
        return None
    if not isinstance(session.routing, dict):
        session.routing = {}
    if signal == REPLY_DEPTH_STANDARD:
        session.routing[_ROUTING_KEY] = REPLY_DEPTH_STANDARD
        session.routing.pop("reply_depth_source", None)
    else:
        session.routing[_ROUTING_KEY] = signal
        session.routing["reply_depth_source"] = "user_explicit"
    return signal


def build_reply_depth_context(session: MessengerSession) -> str:
    """Inject only when user has overridden default (quick/deep). Standard → empty (use default prompt)."""
    depth = get_reply_depth(session)
    rq = session.routing if isinstance(session.routing, dict) else {}
    if depth == REPLY_DEPTH_QUICK and rq.get("reply_depth_source") == "user_explicit":
        return (
            "ĐỘ SÂU TRẢ LỜI (user đã yêu cầu — áp dụng cho các turn sau cho đến khi user đổi ý):\n"
            "- Trả lời NGẮN GỌN: 2–3 ý chính, đi thẳng vào kết luận, không liệt kê dài.\n"
            "- Cuối có thể hỏi nhẹ: \"Bạn muốn mình giải thích kỹ phần nào thêm không?\"\n"
            "- Lệnh này thắng hướng dẫn trả lời chi tiết mặc định.\n"
            "- Không nhắc lại kiểu \"hôm trước bạn bảo ngắn\" trừ khi user hỏi."
        )

    if depth == REPLY_DEPTH_DEEP and rq.get("reply_depth_source") == "user_explicit":
        return (
            "ĐỘ SÂU TRẢ LỜI (user vừa yêu cầu đi sâu / chi tiết hơn):\n"
            "- Trả lời chi tiết, có chiều sâu như mặc định; tập trung vào phần user hỏi.\n"
            "- Nêu rõ tên sao, tên cung liên quan.\n"
            "- Lệnh này thắng mọi xu hướng \"thích ngắn\" từ memory cũ (nếu có)."
        )

    return ""
