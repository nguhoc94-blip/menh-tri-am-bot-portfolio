from __future__ import annotations

import hashlib
import hmac

import pytest

from app.middleware.webhook_signature import (
    WebhookSignatureError,
    verify_meta_webhook_signature,
)


def test_verify_meta_webhook_signature_ok() -> None:
    body = b'{"object":"page","entry":[]}'
    secret = "my_app_secret"
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    verify_meta_webhook_signature(
        raw_body=body,
        signature_header=f"sha256={digest}",
        app_secret=secret,
    )


def test_verify_meta_webhook_signature_rejects_bad_sig() -> None:
    body = b"{}"
    secret = "s"
    with pytest.raises(WebhookSignatureError):
        verify_meta_webhook_signature(
            raw_body=body,
            signature_header="sha256=deadbeef",
            app_secret=secret,
        )
