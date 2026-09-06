from __future__ import annotations

import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)


class WebhookSignatureError(Exception):
    pass


def verify_meta_webhook_signature(
    *,
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> None:
    if not signature_header or not signature_header.startswith("sha256="):
        raise WebhookSignatureError("Missing or invalid X-Hub-Signature-256")
    expected_hex = signature_header.removeprefix("sha256=").strip()
    digest = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, expected_hex):
        raise WebhookSignatureError("Signature mismatch")


def should_enforce_webhook_signature() -> bool:
    if (os.environ.get("WEBHOOK_SKIP_SIGNATURE_VERIFY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.warning(
            "webhook_signature_verify_disabled event=webhook_signature_verify_disabled"
        )
        return False
    return bool((os.environ.get("FB_APP_SECRET") or "").strip())


def verify_if_configured(*, raw_body: bytes, signature_header: str | None) -> None:
    secret = (os.environ.get("FB_APP_SECRET") or "").strip()
    if not should_enforce_webhook_signature():
        return
    if not secret:
        raise WebhookSignatureError("FB_APP_SECRET missing while signature verify required")
    verify_meta_webhook_signature(
        raw_body=raw_body,
        signature_header=signature_header,
        app_secret=secret,
    )
