"""
Trigger Render deploy via API (web + worker) and poll /health.

Requires environment variables:
  RENDER_API_KEY  — Render dashboard → Account Settings → API Keys
  RENDER_WEB_SERVICE_ID   — DEMO-backend service id (srv-...)
  RENDER_WORKER_SERVICE_ID — DEMO-worker service id (srv-...)

Optional:
  RENDER_DEPLOY_COMMIT — full commit SHA (default: ad0fd49 release HEAD)
  RENDER_PUBLIC_URL — default https://example-backend.onrender.com
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

API = "https://api.render.com/v1"
DEFAULT_COMMIT = "ad0fd49"
DEFAULT_URL = "https://example-backend.onrender.com"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    key = (os.environ.get("RENDER_API_KEY") or "").strip()
    if not key:
        raise SystemExit("RENDER_API_KEY missing — add to backend/.env")
    data = None
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Render API {method} {path} -> {exc.code}: {err}") from exc


def list_services() -> None:
    page = _request("GET", "/services?limit=20")
    print(json.dumps(page, indent=2))


def trigger_deploy(service_id: str, commit_id: str) -> dict:
    return _request(
        "POST",
        f"/services/{service_id}/deploys",
        {"commitId": commit_id, "clearCache": "do_not_clear"},
    )


def poll_health(base_url: str, want_sha_prefix: str, timeout_sec: int = 900) -> dict:
    deadline = time.time() + timeout_sec
    last: dict = {}
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=30) as resp:
                last = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = {"status": "error", "detail": str(exc)}
        sha = (last.get("git_sha") or "")[:7]
        if sha.startswith(want_sha_prefix[:7]):
            return last
        print(f"health poll: {last} (waiting for git_sha ~{want_sha_prefix[:7]})")
        time.sleep(20)
    return last


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        list_services()
        return 0

    commit = (os.environ.get("RENDER_DEPLOY_COMMIT") or DEFAULT_COMMIT).strip()
    web_id = (os.environ.get("RENDER_WEB_SERVICE_ID") or "").strip()
    worker_id = (os.environ.get("RENDER_WORKER_SERVICE_ID") or "").strip()
    public_url = (os.environ.get("RENDER_PUBLIC_URL") or DEFAULT_URL).strip()

    if not web_id:
        # Try discover by name
        services = _request("GET", "/services?limit=50")
        items = services if isinstance(services, list) else services.get("items", services)
        for item in items:
            svc = item.get("service") or item
            name = svc.get("name") or ""
            sid = svc.get("id") or ""
            if name == "DEMO-backend":
                web_id = sid
            elif name == "DEMO-worker":
                worker_id = sid
        if not web_id:
            raise SystemExit(
                "RENDER_WEB_SERVICE_ID missing and DEMO-backend not found — "
                "run: python scripts/render_deploy.py list"
            )

    print(f"Deploy commit {commit} -> web {web_id}")
    web_dep = trigger_deploy(web_id, commit)
    print("web deploy:", json.dumps(web_dep, indent=2))

    if worker_id:
        print(f"Deploy commit {commit} -> worker {worker_id}")
        worker_dep = trigger_deploy(worker_id, commit)
        print("worker deploy:", json.dumps(worker_dep, indent=2))
    else:
        print("WARN: RENDER_WORKER_SERVICE_ID not set — worker not triggered")

    health = poll_health(public_url, commit)
    print("final health:", json.dumps(health, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
