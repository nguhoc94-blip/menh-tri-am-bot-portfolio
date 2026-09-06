from __future__ import annotations

import logging
import re
from pathlib import Path

from psycopg import errors

from app.db import get_connection

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_SQL_DIR = _BACKEND_ROOT / "sql"
_MIGRATIONS_DIR = _SQL_DIR / "migrations"

_MIGRATION_FILE_RE = re.compile(r"^(\d{3})_.*\.sql$")

_MIGRATION_LOCK_KEY: int = 5_800_001


def _migration_paths() -> list[Path]:
    if not _MIGRATIONS_DIR.is_dir():
        return []
    paths = sorted(_MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return [p for p in paths if _MIGRATION_FILE_RE.match(p.name)]


def _migration_version(path: Path) -> str:
    m = _MIGRATION_FILE_RE.match(path.name)
    if not m:
        raise ValueError(f"Invalid migration filename: {path.name}")
    return m.group(1)


def _legacy_tables_exist(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'messenger_sessions'
            )
            """
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _apply_legacy_schema_locked(lock_conn) -> None:
    """Creates messenger_sessions + readings on empty DB. Uses lock_conn."""
    if _legacy_tables_exist(lock_conn):
        return
    for name in ("init_messenger_sessions.sql", "init_readings.sql"):
        path = _SQL_DIR / name
        if not path.is_file():
            logger.warning("legacy_sql_missing path=%s", path)
            continue
        sql_text = path.read_text(encoding="utf-8")
        lock_conn.execute(sql_text)
        lock_conn.commit()
        logger.info("legacy_schema_applied file=%s", name)


def apply_legacy_schema_if_needed() -> None:
    """Creates messenger_sessions + readings on empty DB (additive; does not drop)."""
    with get_connection() as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        lock_conn.commit()
        try:
            _apply_legacy_schema_locked(lock_conn)
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
            lock_conn.commit()


def _is_applied(conn, version: str) -> bool:
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s",
                (version,),
            )
            return cur.fetchone() is not None
        except errors.UndefinedTable:
            conn.rollback()
            return False


def _run_migrations_locked(lock_conn) -> list[str]:
    """
    Applies migrations on lock_conn (which holds the advisory lock).
    Each migration is committed individually on lock_conn.
    Caller must hold session-level pg_advisory_lock on lock_conn.
    """
    applied: list[str] = []
    paths = _migration_paths()
    if not paths:
        logger.warning("no_migration_files dir=%s", _MIGRATIONS_DIR)
        return applied

    for path in paths:
        version = _migration_version(path)
        if _is_applied(lock_conn, version):
            continue
        sql_text = path.read_text(encoding="utf-8")
        try:
            lock_conn.execute(sql_text)
            with lock_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            lock_conn.commit()
            applied.append(version)
            logger.info("migration_applied version=%s file=%s", version, path.name)
        except Exception as exc:
            lock_conn.rollback()
            logger.error("migration_failed version=%s err=%s", version, exc)
            raise

    return applied


def run_migrations() -> list[str]:
    """
    Applies sql/migrations/001_*.sql … in order. Returns list of newly applied versions.
    Each migration runs in its own transaction to avoid aborting subsequent ones.
    """
    with get_connection() as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        lock_conn.commit()
        try:
            return _run_migrations_locked(lock_conn)
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
            lock_conn.commit()


def backfill_user_profiles_minimal() -> None:
    """
    Technical backfill: PSIDs from messenger_sessions and readings into user_profiles.
    Does not invent Product copy or payload semantics.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO user_profiles (
                        sender_id, first_seen_at, last_seen_at, last_activity_at, updated_at
                    )
                    SELECT
                        sender_id,
                        created_at,
                        updated_at,
                        updated_at,
                        NOW()
                    FROM messenger_sessions
                    ON CONFLICT (sender_id) DO UPDATE SET
                        last_seen_at = GREATEST(
                            user_profiles.last_seen_at,
                            EXCLUDED.last_seen_at
                        ),
                        last_activity_at = GREATEST(
                            COALESCE(user_profiles.last_activity_at, EXCLUDED.last_activity_at),
                            EXCLUDED.last_activity_at
                        ),
                        updated_at = NOW()
                    """
                )
            except errors.UndefinedTable:
                logger.warning("backfill_skipped messenger_sessions_or_user_profiles")

        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO user_profiles (
                        sender_id, first_seen_at, last_seen_at, last_activity_at, updated_at
                    )
                    SELECT
                        sender_id,
                        MIN(created_at),
                        MAX(created_at),
                        MAX(created_at),
                        NOW()
                    FROM readings
                    GROUP BY sender_id
                    ON CONFLICT (sender_id) DO UPDATE SET
                        first_seen_at = LEAST(
                            user_profiles.first_seen_at,
                            EXCLUDED.first_seen_at
                        ),
                        last_seen_at = GREATEST(
                            user_profiles.last_seen_at,
                            EXCLUDED.last_seen_at
                        ),
                        last_activity_at = GREATEST(
                            COALESCE(user_profiles.last_activity_at, EXCLUDED.last_activity_at),
                            EXCLUDED.last_activity_at
                        ),
                        updated_at = NOW()
                    """
                )
            except errors.UndefinedTable:
                logger.warning("backfill_skipped readings_or_user_profiles")


def run_schema_bootstrap() -> None:
    """Legacy tables (if empty DB) → migrations → backfill. Call after pool init."""
    apply_legacy_schema_if_needed()
    applied = run_migrations()
    if applied:
        logger.info("migrations_batch_applied count=%s versions=%s", len(applied), applied)
    backfill_user_profiles_minimal()
