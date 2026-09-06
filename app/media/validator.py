"""
Image validation: resolution, blur, brightness, MIME type.
Slice 2 · V9.2 §3.2 / docs/ARCHITECTURE/05_thresholds.md §2

Dependencies: Pillow==10.4.0, opencv-python-headless==4.10.0.84
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# ── Thresholds from V9.2 §3.2 (DO NOT change without Change Request) ─
MIN_SHORT_EDGE_PX = int(os.environ.get("IMAGE_MIN_SHORT_EDGE_PX", "720"))
MAX_FILE_BYTES = int(os.environ.get("IMAGE_MAX_FILE_MB", "10")) * 1024 * 1024
ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
BLUR_THRESHOLD = float(os.environ.get("IMAGE_BLUR_THRESHOLD_LAPLACIAN", "80.0"))
BRIGHTNESS_MIN = float(os.environ.get("IMAGE_BRIGHTNESS_MIN", "30"))
BRIGHTNESS_MAX = float(os.environ.get("IMAGE_BRIGHTNESS_MAX", "245"))


class ValidationFailReason(str, Enum):
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_MIME = "INVALID_MIME"
    RESOLUTION_TOO_LOW = "RESOLUTION_TOO_LOW"
    BLUR_TOO_HIGH = "BLUR_TOO_HIGH"
    BRIGHTNESS_TOO_LOW = "BRIGHTNESS_TOO_LOW"
    BRIGHTNESS_TOO_HIGH = "BRIGHTNESS_TOO_HIGH"
    CANNOT_DECODE = "CANNOT_DECODE"
    MULTIPLE_FACES = "MULTIPLE_FACES"


@dataclass
class ValidationResult:
    passed: bool
    reason: ValidationFailReason | None = None
    width_px: int | None = None
    height_px: int | None = None
    blur_score: float | None = None
    brightness: float | None = None
    detected_mime: str | None = None

    @property
    def error_code(self) -> str | None:
        return self.reason.value if self.reason else None


def _compute_blur_score(img_gray_bytes: bytes) -> float:
    """Laplacian variance as blur score. Higher = sharper."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(img_gray_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0.0
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
    except ImportError:
        logger.debug("opencv not available — skipping blur check")
        return 999.0  # pass if cv2 not installed


def _compute_brightness(img_gray_bytes: bytes) -> float:
    """Mean pixel value of grayscale image (0–255)."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(img_gray_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 128.0
        return float(np.mean(img))
    except ImportError:
        return 128.0


def validate_image(data: bytes, declared_content_type: str = "") -> ValidationResult:
    """
    Validate image bytes for ingestion as palm/face input.
    Checks: file size, MIME, resolution, blur, brightness.

    Args:
        data: raw image bytes
        declared_content_type: MIME type from HTTP Content-Type header

    Returns:
        ValidationResult — check .passed before using the image.
    """
    if len(data) > MAX_FILE_BYTES:
        return ValidationResult(passed=False, reason=ValidationFailReason.FILE_TOO_LARGE)

    # MIME detection via Pillow (more reliable than HTTP header)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.verify()
        # Re-open after verify (verify() makes image unusable)
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").lower()
        mime_map = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
        detected_mime = mime_map.get(fmt, f"image/{fmt}")
        width, height = img.size
    except Exception as exc:
        logger.warning("image_decode_failed err=%s", exc)
        return ValidationResult(passed=False, reason=ValidationFailReason.CANNOT_DECODE)

    if detected_mime not in ALLOWED_MIME_TYPES:
        return ValidationResult(
            passed=False,
            reason=ValidationFailReason.INVALID_MIME,
            detected_mime=detected_mime,
        )

    short_edge = min(width, height)
    if short_edge < MIN_SHORT_EDGE_PX:
        return ValidationResult(
            passed=False,
            reason=ValidationFailReason.RESOLUTION_TOO_LOW,
            width_px=width,
            height_px=height,
            detected_mime=detected_mime,
        )

    # Blur + brightness (OpenCV-based)
    blur_score = _compute_blur_score(data)
    brightness = _compute_brightness(data)

    if blur_score < BLUR_THRESHOLD:
        return ValidationResult(
            passed=False,
            reason=ValidationFailReason.BLUR_TOO_HIGH,
            width_px=width,
            height_px=height,
            blur_score=blur_score,
            brightness=brightness,
            detected_mime=detected_mime,
        )

    if brightness < BRIGHTNESS_MIN:
        return ValidationResult(
            passed=False,
            reason=ValidationFailReason.BRIGHTNESS_TOO_LOW,
            width_px=width,
            height_px=height,
            blur_score=blur_score,
            brightness=brightness,
            detected_mime=detected_mime,
        )

    if brightness > BRIGHTNESS_MAX:
        return ValidationResult(
            passed=False,
            reason=ValidationFailReason.BRIGHTNESS_TOO_HIGH,
            width_px=width,
            height_px=height,
            blur_score=blur_score,
            brightness=brightness,
            detected_mime=detected_mime,
        )

    logger.info(
        "image_validated ok w=%d h=%d blur=%.1f brightness=%.1f mime=%s",
        width, height, blur_score, brightness, detected_mime,
    )
    return ValidationResult(
        passed=True,
        width_px=width,
        height_px=height,
        blur_score=blur_score,
        brightness=brightness,
        detected_mime=detected_mime,
    )


def build_retry_copy(reason: ValidationFailReason | None, attempt: int, mode: str) -> str:
    """User-facing copy for failed image validation (tri kỷ tone, no blame)."""
    base_copies: dict[str, str] = {
        ValidationFailReason.FILE_TOO_LARGE.value: (
            "Ảnh của bạn hơi nặng quá (trên 10 MB). Bạn thử nén nhỏ lại hoặc "
            "chụp thẳng từ camera điện thoại rồi gửi lại nhé."
        ),
        ValidationFailReason.INVALID_MIME.value: (
            "Ảnh gửi chưa đúng định dạng. Bạn gửi ảnh JPG, PNG hoặc WEBP giúp mình nhé."
        ),
        ValidationFailReason.RESOLUTION_TOO_LOW.value: (
            "Ảnh hơi nhỏ, mình cần ảnh sắc nét hơn một chút (tối thiểu 720px cạnh ngắn). "
            "Bạn chụp lại ảnh gốc từ camera rồi gửi nhé."
        ),
        ValidationFailReason.BLUR_TOO_HIGH.value: (
            "Ảnh hơi mờ, mình không phân tích được chính xác. "
            "Bạn chụp lại chỗ đủ sáng, giữ điện thoại thật cố định nhé."
        ),
        ValidationFailReason.BRIGHTNESS_TOO_LOW.value: (
            "Ảnh hơi tối quá. Bạn chụp lại nơi sáng hơn một chút nhé — "
            "cạnh cửa sổ hoặc bật đèn phòng là ổn."
        ),
        ValidationFailReason.BRIGHTNESS_TOO_HIGH.value: (
            "Ảnh bị chói sáng quá. Bạn chụp lại tránh ánh sáng trực tiếp chiếu thẳng nhé."
        ),
        ValidationFailReason.CANNOT_DECODE.value: (
            "Ảnh bị lỗi hoặc không mở được. Bạn thử gửi lại ảnh khác nhé."
        ),
    }
    reason_key = reason.value if reason else ""
    copy = base_copies.get(reason_key, "Ảnh chưa đạt yêu cầu. Bạn gửi lại giúp mình nhé.")

    if attempt >= 2:
        mode_label = "xem chỉ tay" if mode == "palm" else "xem tướng mặt"
        copy += (
            f"\n\nNếu vẫn gặp khó khăn, bạn có thể tạm bỏ qua phần {mode_label} "
            "và mình hỗ trợ theo cách khác nhé."
        )
    return copy
