"""
Send rendered image pages back to user via Messenger Graph API.
Slice 4 · V9 send pipeline

Uses multipart/form-data upload to /me/messages with image attachment.
Falls back to text delivery if image send fails (reliability rule).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from io import BytesIO

logger = logging.getLogger(__name__)

from app.workers.retry_budget import get_budget as _get_budget

_SEND_TIMEOUT = 30
_SEND_RETRIES: int = _get_budget("send_asset")["service_max"]
_SEND_BACKOFF = 0.8


def send_image_to_user(
    sender_id: str,
    jpeg_bytes: bytes,
    *,
    request_id: str,
    caption: str | None = None,
) -> "OutboundSendResult":
    """
    Send a JPEG image to a Messenger user.
    Uses multipart/form-data to upload and send in one call.

    Returns OutboundSendResult with kind=image for every outcome branch.
    """
    from app.services.debug_outbound import capture_image, should_capture
    from app.services.outbound_audit import (
        KIND_IMAGE,
        OUTCOME_DEBUG_CAPTURE,
        OUTCOME_MISSING_TOKEN,
        OUTCOME_SEND_FAILED,
        OUTCOME_SENT_OK,
        OutboundSendResult,
        log_outbound_result,
        parse_graph_message_id,
    )

    if should_capture(sender_id):
        capture_image(sender_id, jpeg_bytes, mime="image/jpeg")
        logger.info(
            "debug_image_captured sender=%s size_kb=%.1f request_id=%s",
            sender_id[:8],
            len(jpeg_bytes) / 1024,
            request_id,
        )
        log_outbound_result(
            outcome=OUTCOME_DEBUG_CAPTURE,
            kind=KIND_IMAGE,
            sender_id=sender_id,
            request_id=request_id,
        )
        return OutboundSendResult(outcome=OUTCOME_DEBUG_CAPTURE, kind=KIND_IMAGE)

    page_token = (os.environ.get("FB_PAGE_ACCESS_TOKEN") or "").strip()
    if not page_token:
        logger.warning("send_image_skipped reason=no_page_token sender=%s", sender_id[:8])
        log_outbound_result(
            outcome=OUTCOME_MISSING_TOKEN,
            kind=KIND_IMAGE,
            sender_id=sender_id,
            request_id=request_id,
        )
        return OutboundSendResult(outcome=OUTCOME_MISSING_TOKEN, kind=KIND_IMAGE)

    url = f"https://graph.facebook.com/v21.0/me/messages?access_token={page_token}"

    # Build multipart payload
    boundary = "----MenhTriAmBoundary"
    message_data = {
        "recipient": {"id": sender_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"is_reusable": False},
            }
        },
    }

    body = _build_multipart(boundary, message_data, jpeg_bytes, "image/jpeg")

    import time
    for attempt in range(1, _SEND_RETRIES + 1):
        req = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_SEND_TIMEOUT) as resp:
                resp_body = resp.read()
                message_id = parse_graph_message_id(resp_body)
                logger.info(
                    "image_sent sender=%s attempt=%d size_kb=%.1f",
                    sender_id[:8], attempt, len(jpeg_bytes) / 1024,
                )
                log_outbound_result(
                    outcome=OUTCOME_SENT_OK,
                    kind=KIND_IMAGE,
                    sender_id=sender_id,
                    message_id=message_id,
                    request_id=request_id,
                )
                return OutboundSendResult(
                    outcome=OUTCOME_SENT_OK,
                    message_id=message_id,
                    kind=KIND_IMAGE,
                )
        except urllib.error.HTTPError as exc:
            logger.warning(
                "image_send_http_error sender=%s status=%s attempt=%d",
                sender_id[:8], exc.code, attempt,
            )
            if attempt < _SEND_RETRIES:
                time.sleep(_SEND_BACKOFF * attempt)
        except Exception as exc:
            logger.warning(
                "image_send_error sender=%s attempt=%d err=%s",
                sender_id[:8], attempt, str(exc)[:200],
            )
            if attempt < _SEND_RETRIES:
                time.sleep(_SEND_BACKOFF * attempt)

    log_outbound_result(
        outcome=OUTCOME_SEND_FAILED,
        kind=KIND_IMAGE,
        sender_id=sender_id,
        request_id=request_id,
    )
    return OutboundSendResult(outcome=OUTCOME_SEND_FAILED, kind=KIND_IMAGE)


def _build_multipart(
    boundary: str,
    message_data: dict,
    file_bytes: bytes,
    file_mime: str,
) -> bytes:
    """Build multipart/form-data body for Messenger image attachment."""
    parts: list[bytes] = []
    sep = f"--{boundary}\r\n".encode()
    end = f"--{boundary}--\r\n".encode()

    # Part 1: message JSON
    parts.append(sep)
    parts.append(b"Content-Disposition: form-data; name=\"recipient\"\r\n\r\n")
    parts.append(json.dumps(message_data["recipient"]).encode("utf-8"))
    parts.append(b"\r\n")

    parts.append(sep)
    parts.append(b"Content-Disposition: form-data; name=\"message\"\r\n\r\n")
    parts.append(json.dumps(message_data["message"]).encode("utf-8"))
    parts.append(b"\r\n")

    # Part 2: file
    parts.append(sep)
    parts.append(
        f"Content-Disposition: form-data; name=\"filedata\"; filename=\"result.jpg\"\r\n"
        f"Content-Type: {file_mime}\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(b"\r\n")

    parts.append(end)
    return b"".join(parts)


def send_text_fallback(sender_id: str, text: str, *, request_id: str) -> None:
    """Send text fallback when image delivery fails."""
    try:
        from app.services.messenger_handler import send_text_message
        send_text_message(sender_id, text, request_id=request_id)
    except Exception:
        logger.warning("text_fallback_failed sender=%s", sender_id[:8])


def send_affiliate_button_template(
    sender_id: str,
    url: str,
    label: str,
    *,
    request_id: str,
) -> bool:
    """
    Send a Messenger Button Template with one clickable URL button for the affiliate link.

    The template body explains the link clearly and shows the destination domain
    so users are not afraid of phishing.  The button itself is a native Messenger
    web_url button — tapping opens the URL in the in-app browser.

    Returns True on success, False on failure (never raises).
    """
    page_token = (os.environ.get("FB_PAGE_ACCESS_TOKEN") or "").strip()
    if not page_token:
        logger.warning("affiliate_btn_skipped reason=no_page_token sender=%s", sender_id[:8])
        return False

    # Extract domain to display transparently, e.g. "shp.ee" or "shopee.vn"
    domain = ""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc or ""
    except Exception:
        pass

    domain_note = f" ({domain})" if domain else ""
    safe_label = (label or "Shopee").strip()

    body_text = (
        "🛍️  Ủng hộ Tri Âm qua mua sắm\n\n"
        f"Nếu cần mua hàng trên Shopee, mở link {safe_label}{domain_note} "
        "rồi mua bình thường — bạn không trả thêm phí.\n\n"
        "Nếu đơn được ghi nhận, team nhận hoa hồng nhỏ từ nền tảng để duy trì hệ thống.\n\n"
        "Không yêu cầu OTP • Không cài app lạ • Chỉ mở link trong cuộc trò chuyện này."
    )[:640]  # Messenger Button Template text limit

    button_title = f"Mở {safe_label}"[:20]  # Messenger button title limit = 20 chars

    payload = {
        "recipient": {"id": sender_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": body_text,
                    "buttons": [
                        {
                            "type": "web_url",
                            "url": url,
                            "title": button_title,
                            "webview_height_ratio": "full",
                        }
                    ],
                },
            }
        },
    }

    api_url = f"https://graph.facebook.com/v21.0/me/messages?access_token={page_token}"
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url=api_url,
        data=body_bytes,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_SEND_TIMEOUT) as resp:
            resp.read()
            logger.info(
                "affiliate_btn_sent sender=%s label=%s domain=%s",
                sender_id[:8], safe_label[:30], domain,
            )
            return True
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        logger.warning(
            "affiliate_btn_http_error sender=%s status=%s body=%s",
            sender_id[:8], exc.code, err_body,
        )
    except Exception as exc:
        logger.warning(
            "affiliate_btn_error sender=%s err=%s",
            sender_id[:8], str(exc)[:200],
        )
    return False
