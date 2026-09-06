"""
Face mode consent — generate and validate consent tokens.
Slice 2 · V9 §7.3, V9.1 §10.4, V9.2 §3.2

Consent copy (U-13 draft — pending product review after Slice 2):
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger(__name__)

# Consent is valid for 30 minutes (user must complete face upload in this window)
CONSENT_VALIDITY_SECONDS = 1800

# ── U-13 Consent copy (draft by agent, pending product review) ─────
FACE_CONSENT_MESSAGE = """
🌿 *Xem tướng mặt — Lưu ý trước khi bắt đầu*

Tính năng xem tướng mặt dùng ảnh khuôn mặt của bạn để luận giải theo phương pháp nhân tướng học truyền thống — hoàn toàn mang tính tham khảo và định hướng tinh thần.

**Mình cam kết:**
• Ảnh chỉ dùng để phân tích, không lưu lâu hơn 7 ngày
• Không dùng cho mục đích khác ngoài luận giải cho bạn
• Không chia sẻ với bên thứ ba

**Lưu ý khi chụp ảnh:**
• Chỉ ảnh của chính bạn (không ảnh người khác / trẻ em)
• Nhìn thẳng vào camera, đủ sáng, không che mặt
• Mình không suy luận thông tin danh tính, sức khỏe hay đạo đức từ ảnh

Bạn đồng ý tiếp tục không?
""".strip()

FACE_CONSENT_QUICK_REPLIES = [
    {"content_type": "text", "title": "Đồng ý, gửi ảnh", "payload": "FACE_CONSENT_ACCEPT"},
    {"content_type": "text", "title": "Thôi, xem kiểu khác", "payload": "FACE_CONSENT_DECLINE"},
]


def generate_consent_token(sender_id: str, session_id: str) -> str:
    """
    Generate a time-limited HMAC consent token.
    Must be stored alongside the face asset in the assets table.
    """
    secret = (os.environ.get("USER_HASH_HMAC_SECRET") or "").strip().encode()
    if not secret:
        logger.warning("consent_token_degraded reason=no_hmac_secret")
        secret = b"consent-fallback"

    expires_at = int(time.time()) + CONSENT_VALIDITY_SECONDS
    payload = f"{sender_id}:{session_id}:{expires_at}"
    token = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{token}:{expires_at}"


def validate_consent_token(token: str, sender_id: str, session_id: str) -> bool:
    """
    Validate a consent token.
    Returns True only if token matches and has not expired.
    """
    if not token or ":" not in token:
        return False

    try:
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return False
        raw_token, expires_str = parts
        expires_at = int(expires_str)
    except (ValueError, TypeError):
        return False

    if time.time() > expires_at:
        logger.debug("consent_token_expired sender=%s", sender_id[:8])
        return False

    secret = (os.environ.get("USER_HASH_HMAC_SECRET") or "").strip().encode()
    if not secret:
        secret = b"consent-fallback"

    payload = f"{sender_id}:{session_id}:{expires_at}"
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(raw_token, expected)
