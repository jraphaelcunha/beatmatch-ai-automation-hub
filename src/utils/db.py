"""
Database connection pool and query execution layer for Supabase PostgreSQL.
Provides thread-safe connection pooling to prevent socket starvation under concurrent workloads.
"""

import logging
import os
import threading
import time
import urllib.parse
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extensions
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from psycopg2.pool import PoolError, ThreadedConnectionPool

load_dotenv()

logger = logging.getLogger("db_pool")
DATABASE_URL = os.getenv("DATABASE_URL")

_pool_lock = threading.Lock()
_connection_pool: ThreadedConnectionPool | None = None


def _format_db_url(raw_url: str) -> str:
    """
    Normalizes PostgreSQL connection URL.
    - Replaces legacy 'postgres://' scheme with 'postgresql://'.
    - Preserves user-declared host, port, credentials, and parameters by default.
    - For Supabase pooler endpoints (*.pooler.supabase.com) targeting default port 5432,
      routes to transaction pooling port 6543 if explicitly matching the pooler domain.
    """
    if not raw_url:
        return ""

    if raw_url.startswith("postgres://"):
        raw_url = "postgresql://" + raw_url[len("postgres://"):]

    try:
        parsed = urllib.parse.urlsplit(raw_url)
        hostname = (parsed.hostname or "").lower()
        is_supabase_pooler = hostname == "pooler.supabase.com" or hostname.endswith(".pooler.supabase.com")
        if is_supabase_pooler and parsed.port == 5432:
            auth_prefix = ""
            if parsed.username:
                auth_prefix = parsed.username
                if parsed.password:
                    auth_prefix += f":{parsed.password}"
                auth_prefix += "@"
            new_netloc = f"{auth_prefix}{parsed.hostname}:6543"
            return urllib.parse.urlunsplit((
                parsed.scheme,
                new_netloc,
                parsed.path,
                parsed.query,
                parsed.fragment
            ))
    except Exception as exc:
        logger.debug("Database URL rewrite evaluation skipped: %s", exc)

    return raw_url


def is_database_configured(url: str | None = None) -> bool:
    """
    Determines whether a valid PostgreSQL database connection URL is configured.
    Distinguishes absent or placeholder configurations from valid PostgreSQL endpoints.
    Accepts arbitrary PostgreSQL hosts (local Docker, Supabase, AWS RDS, GCP Cloud SQL).
    Does not leak secrets or credentials.
    """
    raw_url = url if url is not None else (os.getenv("DATABASE_URL") or DATABASE_URL)
    if not raw_url or not isinstance(raw_url, str) or not raw_url.strip():
        return False

    trimmed = raw_url.strip()
    lowered = trimmed.lower()
    placeholders = ["[your-password]", "your_password", "<password>", "your_postgres"]
    if any(p in lowered for p in placeholders):
        return False

    try:
        formatted = _format_db_url(trimmed)
        parsed = urllib.parse.urlsplit(formatted)
        if parsed.scheme.lower() not in ("postgresql", "postgres"):
            return False
        if not parsed.hostname:
            return False
        return True
    except Exception:
        return False


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


def close_pool() -> None:
    """Closes all connections in the pool and resets the singleton."""
    global _connection_pool
    with _pool_lock:
        if _connection_pool is not None:
            try:
                _connection_pool.closeall()
            except Exception as err:
                logger.warning("Error closing connection pool: %s", err)
            finally:
                _connection_pool = None


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


def release_connection(conn: psycopg2.extensions.connection | None) -> None:
    """Safely returns a connection to the pool or closes direct connection."""
    if conn is None:
        return

    should_close_in_pool = False
    if not getattr(conn, "closed", False):
        try:
            status = conn.get_transaction_status()
            if status in (
                psycopg2.extensions.TRANSACTION_STATUS_INTRANS,
                psycopg2.extensions.TRANSACTION_STATUS_INERROR,
            ):
                conn.rollback()
        except Exception as rb_err:
            logger.warning("Error rolling back uncommitted transaction before release: %s", rb_err)
            should_close_in_pool = True
    else:
        should_close_in_pool = True

    if _connection_pool is not None:
        try:
            _connection_pool.putconn(conn, close=should_close_in_pool)
            return
        except PoolError as pool_err:
            logger.debug(
                "Connection not managed by active pool (%s). Closing as standalone connection.",
                pool_err
            )
        except psycopg2.Error as err:
            logger.warning("Error putting connection back into pool: %s", err)

    try:
        if not getattr(conn, "closed", False):
            conn.close()
    except psycopg2.Error as err:
        logger.warning("Error closing standalone connection: %s", err)


@contextmanager
def get_db_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """Context manager providing a transactional connection and ensuring its return to the pool."""
    conn = get_connection()
    try:
        yield conn
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except psycopg2.Error as rb_err:
                logger.warning("Error rolling back connection on exception: %s", rb_err)
        raise
    finally:
        release_connection(conn)


@contextmanager
def get_db_cursor(commit: bool = False) -> Generator[Any, None, None]:
    """Context manager providing a transactional cursor and ensuring connection release."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
            if commit:
                conn.commit()


def execute_query(query: str, params: tuple | list | None = None, fetch: bool = False) -> Any:
    """Executes a query safely utilizing the managed connection pool."""
    commit_action = not fetch
    with get_db_cursor(commit=commit_action) as cur:
        cur.execute(query, params)
        if fetch:
            return cur.fetchall()
        return None
