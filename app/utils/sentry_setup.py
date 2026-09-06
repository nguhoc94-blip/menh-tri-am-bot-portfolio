"""
Sentry initialization — no-op safe when SENTRY_DSN is not set.
Slice 1 · U-05 decision: Sentry only; code must not crash in local/dev.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def init_sentry() -> bool:
    """
    Initialize Sentry SDK if SENTRY_DSN is configured.
    Returns True if Sentry was initialized, False if skipped (no DSN = no-op).
    Never raises — a Sentry init failure must not crash the application.
    """
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.debug("sentry_skipped reason=no_dsn")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_logging = LoggingIntegration(
            level=logging.INFO,
            event_level=logging.ERROR,
        )

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=float(
                (os.environ.get("SENTRY_TRACES_SAMPLE_RATE") or "0.1").strip()
            ),
            integrations=[FastApiIntegration(), sentry_logging],
            # Do not send PII — privacy rule P.1 rule 5
            send_default_pii=False,
        )
        logger.info(
            "sentry_initialized env=%s",
            os.environ.get("SENTRY_ENVIRONMENT", "production"),
        )
        return True
    except ImportError:
        logger.warning(
            "sentry_skipped reason=sdk_not_installed "
            "hint=add sentry-sdk[fastapi]==2.14.0 to requirements.txt"
        )
        return False
    except Exception as exc:
        logger.warning("sentry_init_failed err=%s", exc)
        return False
