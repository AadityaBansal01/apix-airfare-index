"""PostgreSQL access.

psycopg3 is imported lazily so that the index engine, the ethics layer and their
tests all work in an environment with no database driver installed. Only code
that actually touches the database pays for the import.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any, Iterator, Sequence

from apix.settings import settings

log = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    pass


def _psycopg():
    try:
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise DatabaseUnavailable(
            "psycopg is not installed. Run `make init` or "
            "`pip install 'psycopg[binary,pool]'`."
        ) from exc
    return psycopg, dict_row


def _pool_disabled() -> bool:
    """Serverless runtimes must not pool in-process.

    On Vercel or Lambda each invocation is its own short-lived process, so a
    per-process pool multiplies open connections by concurrency and exhausts
    Postgres `max_connections` under a modest spike. There the correct pooling
    layer is external: Neon's `-pooler` endpoint, or PgBouncer in transaction
    mode, named in DATABASE_URL.
    """
    import os

    return os.getenv("APIX_DISABLE_POOL", "").strip().lower() in (
        "1", "true", "yes", "on")


def get_pool():
    """Lazily construct a connection pool, falling back to plain connections."""
    global _pool
    if _pool_disabled():
        return None
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            from psycopg_pool import ConnectionPool  # noqa: PLC0415
            from psycopg.rows import dict_row  # noqa: PLC0415
        except ModuleNotFoundError:
            log.debug("psycopg_pool unavailable; using unpooled connections")
            return None
        # `timeout` and `connect_timeout` both matter here.
        #
        # ConnectionPool(open=True) blocks up to `timeout` (30s by default)
        # waiting for the pool to fill. With no database running that is 30
        # seconds burned per importing process, which made `make test` take
        # five minutes before skipping the integration tests instead of a few
        # seconds. Failing fast is right for both a test run and a CLI: if
        # Postgres is not there, saying so immediately is more useful than
        # waiting half a minute to say the same thing.
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=8,
            timeout=settings.db_connect_timeout,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "connect_timeout": int(settings.db_connect_timeout),
            },
            open=True,
        )
        return _pool


@contextlib.contextmanager
def connection() -> Iterator[Any]:
    """A transactional connection. Commits on clean exit, rolls back on error."""
    pool = get_pool()
    if pool is not None:
        with pool.connection() as conn:
            yield conn
        return
    psycopg, dict_row = _psycopg()
    conn = psycopg.connect(settings.database_url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
