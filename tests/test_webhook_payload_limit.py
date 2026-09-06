"""Nhịp 3 — webhook body size guardrail."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_webhook_post_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SKIP_SIGNATURE_VERIFY", "1")
    monkeypatch.delenv("FB_APP_SECRET", raising=False)
    client = TestClient(app)
    big = b'{"entry":[]}' + b"x" * (300 * 1024)
    r = client.post("/webhook", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
