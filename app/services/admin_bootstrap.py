"""Tạo admin đầu tiên khi bảng trống + env ADMIN_BOOTSTRAP_* (Nhịp 2)."""

from __future__ import annotations

import logging
import os

from app.db import get_connection
from app.services.admin_password import hash_password

logger = logging.getLogger(__name__)


def ensure_bootstrap_admin() -> None:
    email = (os.environ.get("ADMIN_BOOTSTRAP_EMAIL") or "").strip()
    password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD") or ""
    if not email or not password.strip():
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM admin_users")
                n = cur.fetchone()[0]
                if n and n > 0:
                    return
                cur.execute(
                    """
                    INSERT INTO admin_users (email, password_hash, role, is_active)
                    VALUES (%s, %s, 'admin', TRUE)
                    """,
                    (email, hash_password(password)),
                )
        logger.info("admin_bootstrap_created email=%s", email)
    except Exception:
        logger.warning("admin_bootstrap_failed", exc_info=True)
