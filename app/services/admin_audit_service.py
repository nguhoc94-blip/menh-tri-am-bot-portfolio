from __future__ import annotations

import json
import logging
from typing import Any

from app.db import get_connection

logger = logging.getLogger(__name__)


def write_audit_log(
    *,
    admin_user_id: int | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_audit_log
                        (admin_user_id, action, resource_type, resource_id, detail_json)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        admin_user_id,
                        action,
                        resource_type,
                        resource_id,
                        json.dumps(detail or {}),
                    ),
                )
    except Exception:
        logger.warning("admin_audit_write_failed action=%s", action, exc_info=True)
