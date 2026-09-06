"""
Provision Render Postgres (DEMO-db-v2), wire DATABASE_URL, deploy backend then worker.

Requires backend/.env:
  RENDER_API_KEY=rnd_...

Optional:
  RENDER_DB_NAME=DEMO-db-v2
  RENDER_DB_REGION=oregon
  RENDER_DB_PLAN=free
  RENDER_DB_VERSION=16
  RENDER_DB_DATABASE_NAME=DEMO
  RENDER_OWNER_ID=
  RENDER_PUBLIC_URL=https://example-backend.onrender.com

Usage:
  cd backend
  python scripts/render_provision_db.py --dry-run
  python scripts/render_provision_db.py
  python scripts/render_provision_db.py --use-existing   # skip create if any postgres available
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

API = "https://api.render.com/v1"
OLD_DB_HOST_PREFIX = "dpg-example-legacy-host-a"
DEFAULT_DB_NAME = "DEMO-db-v2"
DEFAULT_REGION = "oregon"
DEFAULT_PLAN = "free"
DEFAULT_VERSION = "16"
DEFAULT_DATABASE_NAME = "DEMO"
WEB_NAME = "DEMO-backend"
WORKER_NAME = "DEMO-worker"
DEFAULT_PUBLIC_URL = "https://example-backend.onrender.com"


def _api_key() -> str:
    key = (os.environ.get("RENDER_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "RENDER_API_KEY missing.\n"
            "Render Dashboard → Account Settings → API Keys → Create\n"
            "Add to backend/.env: RENDER_API_KEY=rnd_..."
        )
    return key


def _request(method: str, path: str, body: dict | None = None) -> object:
    data = None
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Render API {method} {path} -> {exc.code}: {err}") from exc


def _items(page: object) -> list:
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        return page.get("items") or page.get("services") or []
    return []


def _unwrap(item: dict, key: str) -> dict:
    inner = item.get(key)
    return inner if isinstance(inner, dict) else item


def redact_url(url: str) -> str:
    if "@" not in url:
        return url[:40]
    prefix, host = url.split("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else "postgresql"
    return f"{scheme}://***@{host[:60]}"


def list_owners() -> list[dict]:
    page = _request("GET", "/owners?limit=20")
    out: list[dict] = []
    for item in _items(page):
        owner = _unwrap(item, "owner")
        out.append(
            {
                "id": owner.get("id"),
                "name": owner.get("name") or owner.get("email"),
                "type": owner.get("type"),
            }
        )
    return out


def list_postgres() -> list[dict]:
    page = _request("GET", "/postgres?limit=50")
    out: list[dict] = []
    for item in _items(page):
        pg = _unwrap(item, "postgres")
        if isinstance(pg, dict):
            out.append(
                {
                    "id": pg.get("id"),
                    "name": pg.get("name"),
                    "status": pg.get("status"),
                    "region": pg.get("region"),
                    "plan": pg.get("plan"),
                }
            )
    return out


def find_services() -> dict[str, str]:
    page = _request("GET", "/services?limit=50")
    found: dict[str, str] = {}
    for item in _items(page):
        svc = _unwrap(item, "service")
        name = svc.get("name") or ""
        sid = svc.get("id") or ""
        if name in (WEB_NAME, WORKER_NAME):
            found[name] = sid
    return found


def get_service_region(service_id: str) -> str:
    resp = _request("GET", f"/services/{service_id}")
    svc = _unwrap(resp, "service") if isinstance(resp, dict) else resp
    if isinstance(svc, dict):
        return (svc.get("region") or DEFAULT_REGION).strip()
    return DEFAULT_REGION


def create_postgres(
    *,
    name: str,
    owner_id: str,
    region: str,
    plan: str,
    version: str,
    database_name: str,
) -> str:
    body = {
        "name": name,
        "ownerId": owner_id,
        "region": region,
        "plan": plan,
        "version": version,
        "databaseName": database_name,
    }
    resp = _request("POST", "/postgres", body)
    pg = _unwrap(resp, "postgres") if isinstance(resp, dict) else resp
    if isinstance(pg, dict):
        pid = pg.get("id") or ""
        if pid:
            return pid
    raise SystemExit(f"Unexpected create postgres response: {json.dumps(resp)[:500]}")


def wait_postgres_available(postgres_id: str, timeout_sec: int = 900) -> dict:
    deadline = time.time() + timeout_sec
    last: dict = {}
    while time.time() < deadline:
        resp = _request("GET", f"/postgres/{postgres_id}")
        pg = _unwrap(resp, "postgres") if isinstance(resp, dict) else resp
        if isinstance(pg, dict):
            last = pg
            status = (pg.get("status") or "").lower()
            print(f"postgres status={status}")
            if status == "available":
                return pg
            if status in ("failed", "unknown"):
                raise SystemExit(f"Postgres provisioning failed: {json.dumps(pg)[:800]}")
        time.sleep(15)
    raise SystemExit(f"Timeout waiting for postgres {postgres_id}: {json.dumps(last)[:800]}")


def get_internal_database_url(postgres_id: str) -> str:
    resp = _request("GET", f"/postgres/{postgres_id}/connection-info")
    info = _unwrap(resp, "postgresConnectionInfo") if isinstance(resp, dict) else resp
    if isinstance(info, dict):
        url = (info.get("internalConnectionString") or "").strip()
        if url:
            return url
    raise SystemExit(f"No internalConnectionString in response: {json.dumps(resp)[:500]}")


def get_env_vars(service_id: str) -> list[dict]:
    resp = _request("GET", f"/services/{service_id}/env-vars")
    items = _items(resp)
    out: list[dict] = []
    for item in items:
        ev = _unwrap(item, "envVar")
        if isinstance(ev, dict) and ev.get("key"):
            out.append({"key": ev["key"], "value": ev.get("value") or ""})
    return out


def set_env_vars(service_id: str, env_vars: list[dict]) -> None:
    _request("PUT", f"/services/{service_id}/env-vars", env_vars)


def upsert_database_url(service_id: str, database_url: str) -> None:
    env_vars = get_env_vars(service_id)
    replaced = False
    for ev in env_vars:
        if ev["key"] == "DATABASE_URL":
            ev["value"] = database_url
            replaced = True
            break
    if not replaced:
        env_vars.append({"key": "DATABASE_URL", "value": database_url})
    set_env_vars(service_id, env_vars)


def env_contains_old_host(service_id: str) -> bool:
    for ev in get_env_vars(service_id):
        val = ev.get("value") or ""
        if OLD_DB_HOST_PREFIX in val:
            return True
    return False


def trigger_deploy(service_id: str) -> dict:
    return _request("POST", f"/services/{service_id}/deploys", {"clearCache": "do_not_clear"})


def poll_readiness(base_url: str, timeout_sec: int = 900) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url.rstrip('/')}/readiness", timeout=30) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code == 200:
                return True
        except Exception as exc:
            print(f"readiness poll error: {exc}")
        print("readiness=503 waiting for migrations...")
        time.sleep(20)
    return False


def pick_existing_postgres(databases: list[dict], preferred_name: str) -> dict | None:
    active = [
        pg
        for pg in databases
        if (pg.get("status") or "").lower() in ("available", "creating", "restoring")
    ]
    if not active:
        return None
    for pg in active:
        if pg.get("name") == preferred_name:
            return pg
    if len(active) == 1:
        return active[0]
    print("WARN: multiple active postgres instances found:")
    print(json.dumps(active, indent=2))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision Render Postgres for DEMO bot")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--use-existing",
        action="store_true",
        help="Use an existing available postgres instead of creating a new one",
    )
    args = parser.parse_args()

    db_name = (os.environ.get("RENDER_DB_NAME") or DEFAULT_DB_NAME).strip()
    region = (os.environ.get("RENDER_DB_REGION") or DEFAULT_REGION).strip()
    plan = (os.environ.get("RENDER_DB_PLAN") or DEFAULT_PLAN).strip()
    version = (os.environ.get("RENDER_DB_VERSION") or DEFAULT_VERSION).strip()
    database_name = (os.environ.get("RENDER_DB_DATABASE_NAME") or DEFAULT_DATABASE_NAME).strip()
    owner_id = (os.environ.get("RENDER_OWNER_ID") or "").strip()
    public_url = (os.environ.get("RENDER_PUBLIC_URL") or DEFAULT_PUBLIC_URL).strip()

    owners = list_owners()
    if not owners:
        raise SystemExit("No Render owners found for this API key")
    if not owner_id:
        owner_id = owners[0]["id"] or ""
    print("owners:", json.dumps(owners, indent=2))
    print(f"using ownerId={owner_id}")

    services = find_services()
    print("services:", json.dumps(services, indent=2))
    if WEB_NAME not in services:
        raise SystemExit(f"Service {WEB_NAME} not found in workspace")

    web_id = services[WEB_NAME]
    region = get_service_region(web_id) or region
    print(f"backend region={region}")

    databases = list_postgres()
    print("postgres:", json.dumps(databases, indent=2))

    active = [pg for pg in databases if (pg.get("status") or "").lower() == "available"]
    if active and not args.use_existing and any(pg.get("name") != db_name for pg in active):
        print(
            "WARN: workspace already has active postgres — re-run with --use-existing "
            "or delete/rename manually before creating another."
        )

    if args.dry_run:
        print("dry-run OK — re-run without --dry-run to provision")
        return 0

    postgres_id = ""
    if args.use_existing or active:
        chosen = pick_existing_postgres(databases, db_name)
        if chosen:
            postgres_id = chosen.get("id") or ""
            print(f"using existing postgres id={postgres_id} name={chosen.get('name')}")

    if not postgres_id:
        print(
            f"Creating postgres name={db_name} region={region} plan={plan} "
            f"version={version} databaseName={database_name}"
        )
        postgres_id = create_postgres(
            name=db_name,
            owner_id=owner_id,
            region=region,
            plan=plan,
            version=version,
            database_name=database_name,
        )
        print(f"postgres_id={postgres_id}")

    wait_postgres_available(postgres_id)
    database_url = get_internal_database_url(postgres_id)
    if OLD_DB_HOST_PREFIX in database_url:
        raise SystemExit("Refusing to use connection string pointing at deleted host")
    print(f"internal DATABASE_URL={redact_url(database_url)}")

    upsert_database_url(web_id, database_url)
    print(f"updated DATABASE_URL on {WEB_NAME} ({web_id})")
    if env_contains_old_host(web_id):
        raise SystemExit(f"{WEB_NAME} still references old DB host after update")

    worker_id = services.get(WORKER_NAME)
    if worker_id:
        upsert_database_url(worker_id, database_url)
        print(f"updated DATABASE_URL on {WORKER_NAME} ({worker_id})")
        if env_contains_old_host(worker_id):
            raise SystemExit(f"{WORKER_NAME} still references old DB host after update")
    else:
        print(f"WARN: {WORKER_NAME} not found — set DATABASE_URL manually")

    print("trigger deploy web (migrations run on startup)...")
    print(json.dumps(trigger_deploy(web_id), indent=2))

    if poll_readiness(public_url):
        print("readiness=200 — backend DB bootstrap OK")
    else:
        print("WARN: readiness still not 200 — check DEMO-backend logs before worker deploy")
        return 1

    if worker_id:
        print("trigger deploy worker...")
        print(json.dumps(trigger_deploy(worker_id), indent=2))

    print("\nDone.")
    print(f"  {public_url}/health")
    print(f"  {public_url}/readiness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
