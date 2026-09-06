"""Operational feature flags — parse env once per call (matches DEBUG_CHAT / CTA patterns)."""

from __future__ import annotations

import os

MULTIMODAL_DISABLED_REPLY = (
    "Tính năng xem chỉ tay và xem tướng mặt hiện đang tạm tắt. "
    "Bạn vẫn có thể hỏi tử vi bằng tin nhắn hoặc cung cấp ngày sinh để xem lá số nhé!"
)
MULTIMODAL_DISABLED_IMAGE_REPLY = (
    "Hiện mình chưa nhận ảnh để phân tích chỉ tay hay tướng mặt. "
    "Bạn nhắn tin để hỏi tử vi hoặc gửi ngày sinh để xem lá số nhé."
)
SWITCH_MODE_DEMO_ONLY_REPLY = (
    "Bạn muốn xem tử vi theo ngày sinh? "
    "Cho mình ngày, tháng, năm sinh (dương lịch) và giờ sinh nhé."
)

_MULTIMODAL_V9_PAYLOADS: frozenset[str] | None = None


def is_multimodal_enabled() -> bool:
    raw = (os.environ.get("MULTIMODAL_ENABLED") or "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def multimodal_v9_payloads() -> frozenset[str]:
    global _MULTIMODAL_V9_PAYLOADS
    if _MULTIMODAL_V9_PAYLOADS is None:
        from app.services import payload_specs as ps

        _MULTIMODAL_V9_PAYLOADS = frozenset({
            ps.PALM_START,
            ps.FACE_START,
            ps.COMBINED_START,
            ps.RETRY_IMAGE,
            ps.FACE_CONSENT_ACCEPT,
            ps.FACE_CONSENT_DECLINE,
        })
    return _MULTIMODAL_V9_PAYLOADS
