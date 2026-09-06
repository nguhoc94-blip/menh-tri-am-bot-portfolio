from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

_env = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env)

from app.api.admin_portal import router as admin_portal_router
from app.api.admin_v9 import router as admin_v9_router
from app.api.messenger import router as messenger_router
from app.db import DatabaseUnavailableError, check_connection_ok, close_pool, init_pool
from app.db_init import run_schema_bootstrap
from app.services.admin_bootstrap import ensure_bootstrap_admin
from app.utils.correlation import attach_correlation_filter_to_root
from app.utils.log_redact import attach_redacting_formatter_to_root
from app.utils.sentry_setup import init_sentry
from app.utils.trace_context import set_trace_id

# NOTE (PR-010): correlation fields (request_id/sender_id/job_id/generation_id)
# are intentionally NOT added to this format string. attach_correlation_filter_to_root()
# below makes them available on every LogRecord, but hundreds of existing call
# sites already embed request_id=%s / sender_id=%s explicitly in their message —
# changing the global format was judged higher-risk than additive value for this
# PR. See backend/docs/baseline_v1.md for the full design-decision writeup.
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
    attach_redacting_formatter_to_root(fmt=LOG_FORMAT)
    attach_correlation_filter_to_root()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    init_sentry()  # no-op if SENTRY_DSN not set

    log = logging.getLogger(__name__)
    skip_bootstrap = (os.environ.get("SKIP_DB_BOOTSTRAP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        init_pool()
        if not skip_bootstrap:
            run_schema_bootstrap()
            ensure_bootstrap_admin()
        else:
            log.warning("skip_db_bootstrap_enabled event=skip_db_bootstrap_enabled")
    except DatabaseUnavailableError as e:
        log.error("db_unavailable_at_startup event=db_unavailable_at_startup detail=%s", e)
        if not skip_bootstrap:
            raise
        log.warning("skip_db_bootstrap_enabled event=skip_db_bootstrap_enabled")

    # Co-process worker: start if RUN_WORKER=1 (local/dev only — U-16)
    worker_task = None
    if (os.environ.get("RUN_WORKER") or "").strip() == "1":
        try:
            import app.workers.handlers  # noqa: F401 — registers handlers
            from app.workers.runner import run_worker_async
            worker_task = asyncio.create_task(run_worker_async())
            log.info("worker_coprocess_started event=worker_coprocess_started")
        except Exception as exc:
            log.warning("worker_coprocess_start_failed err=%s", exc)

    # PR-010: periodic baseline metrics sampler (web process only — the
    # standalone `python -m app.workers.runner` process does not start this,
    # to avoid double-counting/double-sampling from two processes).
    metrics_task = None
    from app.services.runtime_metrics import (
        _enabled as runtime_metrics_enabled,
        runtime_metrics_sampler_loop,
        validate_metrics_interval_sec,
    )

    if runtime_metrics_enabled():
        try:
            interval_sec = validate_metrics_interval_sec(
                os.environ.get("RUNTIME_METRICS_INTERVAL_SEC")
            )
            metrics_task = asyncio.create_task(runtime_metrics_sampler_loop(interval_sec))
            log.info("runtime_metrics_sampler_started interval_sec=%s", interval_sec)
        except Exception as exc:
            log.warning("runtime_metrics_sampler_start_failed err=%s", exc)

    yield

    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        log.info("worker_coprocess_stopped event=worker_coprocess_stopped")

    if metrics_task is not None:
        metrics_task.cancel()
        try:
            await metrics_task
        except asyncio.CancelledError:
            pass
        log.info("runtime_metrics_sampler_stopped event=runtime_metrics_sampler_stopped")

    close_pool()


_configure_logging()

app = FastAPI(
    title="Demo Bot — Bot Tử Vi Messenger",
    version="0.9.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context_and_unhandled_errors(request: Request, call_next):
    rid = (request.headers.get("x-request-id") or "").strip() or str(uuid.uuid4())
    request.state.request_id = rid
    set_trace_id(rid)  # propagate trace_id to all code in this request context
    try:
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", rid)
        return response
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "unhandled_http_error path=%s request_id=%s event=internal_error",
            request.url.path,
            rid,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_error", "request_id": rid},
            headers={"X-Request-ID": rid},
        )
app.include_router(messenger_router)
app.include_router(admin_portal_router)
app.include_router(admin_v9_router)

if os.environ.get("DEBUG_CHAT_ENABLED", "").strip() == "1":
    logger.info(
        "debug_routers_mounted event=debug_chat_enabled routers=chat,render",
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness for Render platform — no DB check (Engineering lock Nhịp 3)."""
    out: dict[str, str] = {"status": "ok", "service": "DEMO-backend"}
    sha = (
        os.environ.get("GIT_SHA")
        or os.environ.get("RENDER_GIT_COMMIT")
        or ""
    ).strip()
    if sha:
        out["git_sha"] = sha[:40]
    bt = (os.environ.get("BUILD_TIME") or "").strip()
    if bt:
        out["build_time"] = bt[:64]
    return out


@app.get("/readiness")
def readiness() -> JSONResponse:
    if check_connection_ok():
        return JSONResponse(content={"status": "ready"}, status_code=200)
    return JSONResponse(content={"status": "not_ready"}, status_code=503)
