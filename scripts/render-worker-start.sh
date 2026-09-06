#!/usr/bin/env bash
# Render worker entrypoint: wait for Postgres, start queue consumer.
set -Eeuo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is missing" >&2
  exit 1
fi

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

exec python -m app.workers.runner
