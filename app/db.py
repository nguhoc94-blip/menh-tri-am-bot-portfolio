from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection, OperationalError, connect
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


class DatabaseUnavailableError(RuntimeError):
    pass


_pool: ConnectionPool | None = None


def get_database_url() -> str:
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise DatabaseUnavailableError("DATABASE_URL is missing")
    return database_url


def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    url = get_database_url()
    _pool = ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=int((os.environ.get("DB_POOL_MAX") or "10").strip() or "10"),
        open=True,
    )
    logger.info("db_pool_open max_size=%s", _pool.max_size)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("db_pool_closed")


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise DatabaseUnavailableError("Database pool is not initialized")
    return _pool


@contextmanager
def get_connection() -> Iterator[Connection]:
    """
    Pooled connection. Commits on success, rolls back on error.
    Falls back to single connect if pool not initialized (tests / legacy callers).
    """
    if _pool is None:
        try:
            conn = connect(get_database_url())
        except (OperationalError, DatabaseUnavailableError) as e:
            raise DatabaseUnavailableError(str(e)) from e
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    with _pool.connection() as conn:
        yield conn


def check_connection_ok() -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False
