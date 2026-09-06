# Baseline v1 — Correlation IDs & Runtime Metrics (PR-010)

_PR-010 · Plan v1 Phase 0 (Baseline). Additive-only observability — no product
behavior change. Written after code + tests were complete._

---

## 1. Purpose

Phase -1 truncated the production database to remove pre-refactor noise (see
`refactor/PHASE_MINUS_1_GATE_REPORT.md`). Plan v1's Phase 1 gate requires a
**clean baseline** of a small set of operational SLOs — job backlog, DB
connection budget, threadpool saturation, sender-lock contention, and AI
calls per session — measured against real production traffic on that clean
data, *not* backfilled or estimated from pre-truncation history.

PR-010 builds the **instrumentation** required to take that measurement. It
does not, and cannot, produce the baseline numbers themselves — see §4.

---

## 2. Correlation ID fields and propagation

Four fields make up a correlation context. Each is a `contextvars.ContextVar`
so it survives across `await` points and nested function calls without being
threaded through every function signature.

| Field | Home module | Set by |
|---|---|---|
| `request_id` | `app.utils.trace_context` (`trace_id` contextvar, reused — not duplicated) | `main.py`'s `request_context_and_unhandled_errors` middleware for every HTTP request; `app.utils.correlation.correlation_scope(request_id=...)` for background/worker contexts |
| `sender_id` | `app.utils.correlation` (new) | `correlation_scope(sender_id=...)` |
| `job_id` | `app.utils.correlation` (new) | `correlation_scope(job_id=...)` in `app/workers/runner.py::_process_one` |
| `generation_id` | `app.utils.correlation` (new) | `correlation_scope(generation_id=...)` in `_process_one`, when the job payload carries one |

`app/utils/correlation.py` re-exports `get_request_id`/`set_request_id` from
`trace_context` rather than introducing a second request id — there is
exactly one `request_id` concept in the codebase.

### Propagation path: webhook → batcher → worker

1. **Webhook** (`app/api/messenger.py::receive_webhook`): reuses
   `request.state.request_id` (set once by the middleware) instead of minting
   a second `uuid.uuid4()` — this was the duplication the plan called out.
   Falls back to a fresh uuid only if `request.state` doesn't carry one (e.g.
   a test client bypassing the middleware).
2. **Batcher** (`app/services/inbound_batcher.py`): `request_id` is carried
   per-message on `PendingInbound.request_id` (unchanged — this was already
   explicit parameter passing, not contextvars).
3. **Pipeline** (`app/services/messenger_handler.py::_do_process_messaging_pipeline`):
   opens `correlation.correlation_scope(sender_id=sender_id, request_id=...)`
   around the entire pipeline call, so any log emitted anywhere in the call
   stack underneath it (conversation_bridge, funnel bridge, abuse detector,
   …) can read `correlation.get_sender_id()` / `get_request_id()` without
   those functions taking new parameters.
4. **Worker** (`app/workers/runner.py::_process_one`): after
   `claim_next_job`, opens `correlation.correlation_scope(job_id=str(job_id),
   sender_id=payload.get("sender_id"), generation_id=payload.get("generation_id"))`
   around the handler dispatch (including the complete/fail/cancel branches).

### Logging filter

`CorrelationLogFilter` (in `app/utils/correlation.py`) attaches
`record.request_id` / `record.sender_id` / `record.job_id` /
`record.generation_id` to every `LogRecord` on the root logger (idempotent —
`attach_correlation_filter_to_root()` skips handlers that already carry the
filter). Values default to `""` when unset; the filter never raises.

**Design decision — global `LOG_FORMAT` left unchanged.** `main.py`'s
`LOG_FORMAT` was **not** changed to reference `%(request_id)s` etc. Hundreds
of existing call sites already embed `request_id=%s sender_id=%s` explicitly
in their message text (e.g. `webhook_background_started request_id=%s
sender_id=%s ...`). Changing the global format string to also print those
same fields from the filter would have been redundant for those call sites,
and risked `KeyError`/format mismatches on any of the many pre-existing
`logger.info(...)` calls if the interaction between explicit `%`-args and new
implicit record attributes was not exhaustively re-verified across the whole
codebase — a much larger blast radius than this PR's stated scope. The safer
option, used here: the filter still runs (so `record.request_id` etc. exist
and are available to any *future* formatter that wants them, e.g. a
structured JSON log handler), and all **new** PR-010 instrumentation
(`runtime_metrics.log_metric`, sender-lock timing, job duration) reads the
correlation context directly and embeds it as an explicit tag in its own
message — the same pattern the rest of the codebase already uses. This
keeps 100% of existing log output byte-for-byte unchanged.
`test_pr010_log_redaction_preserved.py` asserts the filter is attached
alongside the (unchanged) `RedactingFormatter` and that both coexist without
breaking redaction. `test_pr010_correlation_propagation.py` proves
`request_id`/`sender_id`/`job_id` are readable from `app.utils.correlation`
during pipeline and worker execution, and that `job_duration_ms` /
`pipeline_duration_ms` log lines show the correct ids.

---

## 3. Metrics implemented

All metrics are emitted via `app.services.runtime_metrics.log_metric()` as a
single structured line:

```
runtime_metric metric=<name> value=<value> key=val key=val ...
```

An operator can extract a time series from Render logs with
`grep 'metric=<name>'`. Gated by `RUNTIME_METRICS_ENABLED` (default `"0"`) —
set to `"1"` to enable; when off, `log_metric()`/`log_snapshot()` are no-ops:
no DB query, no log line, at all.

The web-process periodic sampler offloads DB queries via `asyncio.to_thread`
so the FastAPI event loop is never blocked. `RUNTIME_METRICS_INTERVAL_SEC`
must be numeric and ≥ 5 seconds; invalid or missing values fall back to 60 s
with a warning log.

**PII rule:** `sender_id`, if passed as a tag to `log_metric()`, is always
hashed via `app.utils.sender_hash.hash_sender_id()` before the line is
emitted — a raw PSID never appears in a `runtime_metric` line. See
`test_pr010_log_redaction_preserved.py::test_log_metric_hashes_raw_sender_id`.

| Metric name | Source | Meaning |
|---|---|---|
| `jobs_backlog` | `jobs_by_kind_and_status()` | Row count of `jobs`, one line per `(kind, status)` pair |
| `jobs_stale_running` | `stale_running_jobs_count()` | Jobs stuck `running` past their `locked_until` lease |
| `db_connections_in_use` | `connection_usage()` | Live `SELECT count(*) FROM pg_stat_activity` |
| `db_connections_max` | `connection_usage()` | Live `SHOW max_connections` |
| `threadpool_total_tokens` / `threadpool_borrowed_tokens` | `threadpool_snapshot()` | AnyIO default thread limiter capacity/usage (uvicorn's sync-code offload pool). `None`/skipped outside a running event loop |
| `session_model_calls_sessions_with_calls` / `_avg` / `_max` / `_p50` / `_p95` | `session_model_calls_stats()` | Proxy for "AI calls per completed reading" — counted **per session** (`messenger_sessions.session_model_calls`, PR-002), not per completed reading. Only meaningful when `SESSION_V9_PERSIST_ENABLED=1`; all-zero otherwise |
| `sender_lock_wait_ms` / `sender_lock_hold_ms` | `_timed_sender_lock()` in `messenger_handler.py` | Time blocked waiting for the per-sender lock, and time held once acquired |
| `pipeline_duration_ms` | `_do_process_messaging_pipeline()` wrapper in `messenger_handler.py` | **Proxy** for "time to first outbound" — see limitation below |
| `job_duration_ms` | `_process_one()` in `runner.py` | Wall time from job claim to completion, tagged `kind`, `job_id`, `outcome` (`succeeded` / `failed` / `failed_terminal` / `cancelled_stale` / `no_handler`) |

### Known limitation: `pipeline_duration_ms` is a proxy, not exact "time to first outbound"

The plan asked for "time to first outbound" — the delay between a message
arriving and the bot's first reply being sent. `_do_process_messaging_pipeline_impl`
is long and branchy (spam check, referral routing, structured-payload
routing, multiple `send_outbound_user_text` call sites across different
branches). Pinpointing and instrumenting the *exact* first send call in every
branch was judged higher risk (chance of missing a branch, or subtly
changing control flow) than the value of a precisely correct number for a
Phase 0 baseline PR. Instead, `pipeline_duration_ms` measures the **entire**
`_do_process_messaging_pipeline` call (correlation scope entry to exit,
including early returns) as a conservative upper-bound proxy. This is
documented here and in the wrapper's docstring in
`app/services/messenger_handler.py`.

### Real log line examples (captured from a local PostgreSQL smoke run — see §5)

```
2026-08-05 19:06:29,660 | INFO | app.services.runtime_metrics | runtime_metric metric=jobs_backlog value=2 kind=render_reading status=pending
2026-08-05 19:06:29,660 | INFO | app.services.runtime_metrics | runtime_metric metric=jobs_backlog value=1 kind=send_asset status=pending
2026-08-05 19:06:29,674 | INFO | app.services.runtime_metrics | runtime_metric metric=jobs_stale_running value=0
2026-08-05 19:06:29,693 | INFO | app.services.runtime_metrics | runtime_metric metric=db_connections_in_use value=6
2026-08-05 19:06:29,693 | INFO | app.services.runtime_metrics | runtime_metric metric=db_connections_max value=100
2026-08-05 19:06:29,738 | INFO | app.services.runtime_metrics | runtime_metric metric=session_model_calls_sessions_with_calls value=5
2026-08-05 19:06:29,739 | INFO | app.services.runtime_metrics | runtime_metric metric=session_model_calls_avg value=5.0
2026-08-05 19:06:29,739 | INFO | app.services.runtime_metrics | runtime_metric metric=session_model_calls_max value=9
2026-08-05 19:06:29,739 | INFO | app.services.runtime_metrics | runtime_metric metric=session_model_calls_p50 value=5.0
2026-08-05 19:06:29,739 | INFO | app.services.runtime_metrics | runtime_metric metric=session_model_calls_p95 value=8.6
2026-08-05 19:06:29,739 | INFO | app.services.runtime_metrics | runtime_metric metric=sender_lock_wait_ms value=12.34 sender_id=7a51d064a1a2
2026-08-05 19:06:29,739 | INFO | app.services.runtime_metrics | runtime_metric metric=pipeline_duration_ms value=245.11 sender_id=7a51d064a1a2
```

Note `sender_id=7a51d064a1a2` above — the hashed form of a fake 16-digit PSID
(`1234567890123456`), never the raw value. `threadpool_snapshot()` was
skipped in this particular line group because the smoke script ran outside
an event loop (logged as
`runtime_metrics_threadpool_snapshot_unavailable err=Not currently running
on any asynchronous event loop`) — see §5 for the in-event-loop case, which
does produce a real `{total_tokens, borrowed_tokens}` reading.

### Operator recipe (Render logs)

```bash
# Job backlog trend for one kind
grep 'metric=jobs_backlog' render.log | grep 'kind=render_reading'

# p95 sender-lock wait over a window
grep 'metric=sender_lock_wait_ms' render.log

# Connection budget vs. the max_connections ceiling from infra_constants.md
grep -E 'metric=db_connections_(in_use|max)' render.log
```

---

## 4. Baseline production — CHƯA CÓ SỐ THẬT

**This PR does not, and cannot, contain real production baseline numbers.**

Collecting a real baseline requires:
1. Merging this PR to `main`.
2. Deploying it to Render (`DEMO-backend` web service + `DEMO-worker`).
3. Observing real user traffic for a meaningful window.
4. Recording the actual metric values from Render logs into this document.

**Deploying is explicitly out of scope for this build session** (per the
task instructions — no deploy, no production DB access). The tables in §3
and §5 below describe *what* is instrumented and prove it works
end-to-end against a local test database; they are not production evidence.

### Recommended next step for the technical owner

1. Review and merge this PR.
2. Deploy to Render.
3. Observe **a minimum of 48–72 hours** of real traffic (per
   `refactor/PLAN_V1_CONVERSATION_ORCHESTRATOR.md`, PR-010 §10 — long enough
   to smooth over daily traffic cycles and catch at least one full job-queue
   drain/refill cycle).
4. Fill in the table below with real numbers pulled from Render logs
   (`grep metric=...`, aggregate manually or pipe into a spreadsheet).
5. Only once this table has real numbers should Phase 1 gate be considered
   open, per the plan's explicit gating condition.

```
| Metric                              | p50 | p95 | max | Window observed |
|--------------------------------------|-----|-----|-----|------------------|
| jobs_backlog (per kind/status)       | TBD | TBD | TBD | TBD              |
| jobs_stale_running                   | TBD | TBD | TBD | TBD              |
| db_connections_in_use                | TBD | TBD | TBD | TBD              |
| threadpool_borrowed_tokens           | TBD | TBD | TBD | TBD              |
| sender_lock_wait_ms                  | TBD | TBD | TBD | TBD              |
| pipeline_duration_ms (proxy)         | TBD | TBD | TBD | TBD              |
| job_duration_ms (per kind)           | TBD | TBD | TBD | TBD              |
| session_model_calls (per session)    | TBD | TBD | TBD | TBD              |
```

---

## 5. Local/synthetic smoke sample — NOT PRODUCTION

The numbers below come from running the PR-010 test suite
(`test_pr010_runtime_metrics.py`) and a one-off manual smoke script against a
local, throwaway PostgreSQL instance (`TEST_DATABASE_URL`, never production).
They exist **only** to prove the instrumentation is wired correctly
end-to-end (webhook → pipeline → worker → metrics query → structured log
line) — they carry no signal about real traffic and must not be used for any
Phase 1 decision.

| Metric | Sample value (synthetic) | Setup |
|---|---|---|
| `jobs_backlog` | `kind=render_reading status=pending n=2`, `kind=send_asset status=pending n=1` | 3 jobs enqueued via `app.workers.queue.enqueue()` with fake idempotency keys |
| `db_connections_in_use` / `db_connections_max` | `6` / `100` | Live query against the local test Postgres instance (started via the `pgserver` pip package, matching this instance's own connection-pool defaults — not a Render number, see `infra_constants.md` for the real provisional `max_connections=100` figure from the Render dashboard, coincidentally the same default) |
| `session_model_calls_stats` | `sessions_with_calls=5 avg=5.0 max=9 p50=5.0 p95=8.6` | 5 sessions saved via `DbMessengerStateStore` with `session_model_calls` = 1/3/5/7/9, `SESSION_V9_PERSIST_ENABLED=1` |
| `threadpool_snapshot()` (inside an asyncio event loop) | `{'total_tokens': 40, 'borrowed_tokens': 0}` | `asyncio.run()` around a call to `threadpool_snapshot()` |
| `sender_lock_wait_ms` | logged, ~300ms in the deliberate-contention test | Two threads contend for the same per-sender lock in `test_timed_sender_lock_logs_wait_and_hold_metrics` |

---

## 6. Known gaps / explicit non-goals of PR-010

- **GPT/OpenAI call latency is not instrumented.** `openai_paid.py`,
  `openai_teaser.py`, `conversation_bridge.py`, `analyzer.py`, and the image
  analysis modules are all outside PR-010's declared file scope (per the
  plan). Per-call-type (paid/teaser/multimodal) latency and cost
  instrumentation is a natural follow-up PR, but does not block this PR's
  merge or Phase 1 gating on its own — the plan's Phase 1 gate criteria for
  PR-010 concern the metrics listed in §3, not GPT call latency.
- **"Time to first outbound" is a proxy** (`pipeline_duration_ms`), not an
  exact measurement of the first send call — see §3.
- **No historical backfill.** Everything measured starts from the moment
  this code is deployed; Phase -1's DB truncation means there is no way to
  reconstruct pre-PR-010 history even if desired.
