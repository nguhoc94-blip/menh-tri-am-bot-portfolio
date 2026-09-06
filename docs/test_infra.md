# Test Infrastructure

_Last updated: 2026-08-04 (PR-00h — postgres-test-harness)_

---

## Running the standard suite

Run from the `backend/` directory:

```
python -m pytest tests/ -q
```

Recommended flags to suppress cache noise:

```
python -m pytest tests/ -q --no-header -p no:cacheprovider
```

### Prerequisites

- Python 3.10+
- Dependencies installed: `pip install -r requirements.txt`
- No database connection is required for the standard suite (see below).

### `--timeout` flag

`pytest-timeout` is **not installed**. Do not pass `--timeout`; pytest will
reject it as an unrecognised option.

### Expected result (after PR-001)

```
720 passed, 0 failed
```

---

## How tests interact with PostgreSQL today

The standard suite (`707` tests) is **fully isolated** — no real database is
needed. All DB calls inside production code are mocked at the service or
repository layer.

The one exception is `conftest.py`'s autouse fixture
`clear_webhook_dedupe_for_tests`, which attempts to delete test-scoped rows
from `webhook_dedupe` before each test. It imports `app.db.get_connection` and
wraps the whole block in a bare `except Exception: pass`, so if `DATABASE_URL`
is unset or Postgres is unreachable the fixture silently skips. This means the
standard suite can run without a live database — but dedupe rows are only
cleaned up when a real Postgres is present.

---

## Tests that need a real PostgreSQL — harness status

| What you need | Status |
|---|---|
| `@pytest.mark.requires_postgres` marker | **EXISTS** — `backend/pytest.ini` |
| `TEST_DATABASE_URL` env var | **EXISTS** — fixture skips when unset |
| `conftest_postgres.py` with session fixtures | **EXISTS** — loaded via `pytest_plugins` |
| Production-DB guard (blocklist + opt-in rule) | **EXISTS** — `backend/tests/postgres_harness.py` |
| Isolation and cleanup (truncation per test) | **EXISTS** — `postgres_clean_db` fixture |
| Smoke test | **EXISTS** — `test_postgres_harness.py::test_postgres_migrations_apply_and_schema_exists` |
| Smoke test verified against real PostgreSQL | **BLOCKED — BLK-03**: no local PostgreSQL available on this machine (`psql`/`pg_isready` not on PATH). The smoke test is correctly written and will skip (not error) when `TEST_DATABASE_URL` is unset. It has not been run against a real database. **PR-005 may not merge until this test runs successfully on a real PostgreSQL.** |

---

## How to use the harness

### Mark a test as requiring PostgreSQL

```python
@pytest.mark.requires_postgres
def test_something(postgres_clean_db):
    # Invariant: <state what this test proves>
    get_connection = postgres_clean_db
    with get_connection() as conn:
        ...
```

The `postgres_clean_db` fixture:
- Skips automatically when `TEST_DATABASE_URL` is not set.
- Applies all SQL migrations once per session.
- Truncates all application tables (keeps `schema_migrations`) before and after each test.
- Never connects to production — the guard rejects any URL whose host contains `render.com` or whose database name is `DEMO` or `DEMO_prod`, AND requires the name to contain `test`.

### Run only requires_postgres tests

```powershell
$env:TEST_DATABASE_URL = "postgresql://<user>:<pass>@localhost:5432/DEMO_test"
python -m pytest tests/ -m requires_postgres -v
```

### Run the suite excluding requires_postgres tests (default CI)

```
python -m pytest tests/ -q --no-header -p no:cacheprovider -m "not requires_postgres"
```

This produces the standard baseline: **720 passed, 0 failed**.

### What the guard rejects (before any connection)

- URL equal to `DATABASE_URL` (same target as production/dev)
- Host containing `render.com`
- Database name `DEMO` or `DEMO_prod`
- Database name that does not contain `test` (opt-in requirement)

---

## Running tests against a local PostgreSQL (non-Docker)

Docker is not available on this development machine. Use a locally installed
PostgreSQL instead.

### Step 1 — install PostgreSQL locally

Download and install PostgreSQL 14+ from <https://www.postgresql.org/download/>.
On Windows the installer includes the `psql` CLI and the service manager.

### Step 2 — create a test database

```sql
-- run as the postgres superuser (psql -U postgres)
CREATE DATABASE DEMO_test;
```

### Step 3 — apply migrations

This project does not use Alembic. Migrations are 34 raw SQL files in
`sql/migrations/` (`001_schema_migrations.sql` through
`034_user_daily_tokens.sql`). They are applied by `run_migrations()` in
`app/db_init.py` (lines 76–107), which iterates the files in numeric order,
executes each in its own transaction, and records the applied version in the
`schema_migrations` table so re-runs are safely skipped.

Run from `backend/` with `DATABASE_URL` already pointing at the test database
(set it in your shell before this command, as shown in Step 4):

```
python -c "from app.db_init import run_migrations; run_migrations()"
```

This is **idempotent** — already-applied versions are skipped, so it is safe
to re-run against a partially-migrated database.

**Alternative — apply manually with `psql`** (must be run in numeric order
from `backend/`):

```
psql -U postgres -d DEMO_test -f sql/migrations/001_schema_migrations.sql
psql -U postgres -d DEMO_test -f sql/migrations/002_webhook_dedupe.sql
# ... continue for all 34 files in numeric order
```

The Python runner is preferred: it handles ordering and idempotency
automatically, and it is the same path the web service uses at startup.

### Step 4 — set environment variables

Create (or extend) a `.env.test` file — **never commit real credentials**:

```
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/DEMO_test
TEST_DATABASE_URL=postgresql://<user>:<password>@localhost:5432/DEMO_test
```

Replace `<user>` and `<password>` with your local Postgres credentials.

### Step 5 — run only the requires_postgres-marked tests

```
TEST_DATABASE_URL=postgresql://<user>:<password>@localhost:5432/DEMO_test \
  python -m pytest tests/ -m requires_postgres -v
```

On PowerShell:

```powershell
$env:TEST_DATABASE_URL = "postgresql://<user>:<password>@localhost:5432/DEMO_test"
python -m pytest tests/ -m requires_postgres -v
```

### Step 6 — run the full suite including postgres tests

```powershell
$env:DATABASE_URL    = "postgresql://<user>:<password>@localhost:5432/DEMO_test"
$env:TEST_DATABASE_URL = "postgresql://<user>:<password>@localhost:5432/DEMO_test"
python -m pytest tests/ -q --no-header -p no:cacheprovider
```

---

## Summary

- The **standard suite** (707 tests) runs without any database. Use
  `python -m pytest tests/ -q` from `backend/`.
- A Postgres test harness **now exists** (PR-00h). The smoke test has not yet
  been run against a real database (BLK-03); this is a prerequisite for merging
  PR-005.
- **Never put real credentials in this file or in any committed file.** Use
  `.env` / `.env.test` (both git-ignored) or environment variables set in your
  shell session.
