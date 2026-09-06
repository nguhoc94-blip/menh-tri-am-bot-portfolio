#!/usr/bin/env bash
# Render web entrypoint: wait for Postgres, run schema bootstrap, start uvicorn.
set -Eeuo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is missing" >&2
  exit 1
fi

PORT="${PORT:-8000}"
WAIT_TIMEOUT_SEC="${DB_WAIT_TIMEOUT_SEC:-120}"

python - <<'PY'
import os
import sys
import time

from psycopg import OperationalError, connect

url = os.environ["DATABASE_URL"]
deadline = time.time() + int(os.environ.get("DB_WAIT_TIMEOUT_SEC", "120"))
last_err = ""
while time.time() < deadline:
    try:
        with connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        sys.exit(0)
    except OperationalError as exc:
        last_err = str(exc)
        time.sleep(2)
print("postgres_wait_timeout", file=sys.stderr)
if last_err:
    print(last_err, file=sys.stderr)
sys.exit(1)
PY

skip="${SKIP_DB_BOOTSTRAP:-0}"
case "${skip,,}" in
  1 | true | yes)
    echo "skip_db_bootstrap=1 — migrations deferred to app lifespan or external job"
    ;;
  *)
    python - <<'PY'
from app.db import close_pool, init_pool
from app.db_init import run_schema_bootstrap
from app.services.admin_bootstrap import ensure_bootstrap_admin

init_pool()
try:
    run_schema_bootstrap()
    ensure_bootstrap_admin()
finally:
    close_pool()
PY
    ;;
esac

python - <<'PY'
import json
import logging

logging.basicConfig(level=logging.INFO)
try:
    from app.db import close_pool, init_pool
    from app.services.db_storage_stats import get_db_storage_stats

    init_pool()
    stats = get_db_storage_stats(table_limit=8)
    top = [
        {"table": row["table_name"], "size": row["total_pretty"]}
        for row in stats.get("tables") or []
    ]
    logging.info(
        "db_storage_startup db_size=%s top_tables=%s assets_bytes=%s",
        stats.get("db_size"),
        json.dumps(top, ensure_ascii=False),
        (stats.get("metrics") or {}).get("assets_bytes_pretty"),
    )
finally:
    close_pool()
PY

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
