"""
Database connection pool and query execution layer for Supabase PostgreSQL.
Provides thread-safe connection pooling to prevent socket starvation under concurrent workloads.
"""

import logging
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

load_dotenv()

logger = logging.getLogger("db_pool")
DATABASE_URL = os.getenv("DATABASE_URL")

_pool_lock = threading.Lock()
_connection_pool: ThreadedConnectionPool | None = None


def _format_db_url(raw_url: str) -> str:
    pg_url = raw_url.replace("postgres://", "postgresql://")
    if ":5432" in pg_url:
        pg_url = pg_url.replace(":5432", ":6543")
    return pg_url


def get_pool(min_conn: int = 1, max_conn: int = 10) -> ThreadedConnectionPool:
    """Initializes or returns the singleton ThreadedConnectionPool."""
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                if not DATABASE_URL:
                    raise ValueError("CRITICAL: DATABASE_URL environment variable is not configured.")
                pg_url = _format_db_url(DATABASE_URL)
                _connection_pool = ThreadedConnectionPool(
                    minconn=min_conn,
                    maxconn=max_conn,
                    dsn=pg_url,
                    connect_timeout=10
                )
                logger.info("Initialized PostgreSQL ThreadedConnectionPool (min=%d, max=%d)", min_conn, max_conn)
    return _connection_pool


def get_connection(retries: int = 3, delay: float = 2.0) -> psycopg2.extensions.connection:
    """Acquires a pooled connection or creates a fallback direct connection with backoff."""
    if not DATABASE_URL:
        raise ValueError("CRITICAL: DATABASE_URL environment variable is not configured.")

    try:
        pool = get_pool()
        return pool.getconn()
    except (psycopg2.Error, ValueError, RuntimeError) as pool_err:
        logger.warning("Connection pool acquisition failed (%s). Attempting direct connection with backoff.", pool_err)
        pg_url = _format_db_url(DATABASE_URL)
        for attempt in range(1, retries + 1):
            try:
                return psycopg2.connect(pg_url, connect_timeout=10)
            except psycopg2.OperationalError as err:
                if attempt == retries:
                    logger.error("Exhausted connection attempts (%d/%d): %s", attempt, retries, err)
                    raise
                time.sleep(delay)
        raise RuntimeError("Unreachable connection attempt state.") from pool_err


def release_connection(conn: psycopg2.extensions.connection) -> None:
    """Safely returns a connection to the pool or closes direct connection."""
    if _connection_pool is not None:
        try:
            _connection_pool.putconn(conn)
            return
        except psycopg2.Error as err:
            logger.debug("Error putting connection back into pool: %s", err)
    try:
        conn.close()
    except psycopg2.Error as err:
        logger.debug("Error closing standalone connection: %s", err)


@contextmanager
def get_db_cursor(commit: bool = False) -> Generator[Any, None, None]:
    """Context manager providing a transactional cursor and ensuring connection release."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
            if commit:
                conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def execute_query(query: str, params: tuple | list | None = None, fetch: bool = False) -> Any:
    """Executes a query safely utilizing the managed connection pool."""
    commit_action = not fetch
    with get_db_cursor(commit=commit_action) as cur:
        cur.execute(query, params)
        if fetch:
            return cur.fetchall()
        return None
