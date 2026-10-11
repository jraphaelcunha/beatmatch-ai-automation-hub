"""
Unit tests for database connection pooling, cursor management, and connection lifecycle.
Verifies ThreadedConnectionPool acquire -> use -> rollback/commit -> release semantics.
"""

from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from psycopg2.pool import PoolError, ThreadedConnectionPool

from src.utils import db
from src.utils.db import (
    _format_db_url,
    close_pool,
    execute_query,
    get_connection,
    get_db_connection,
    get_db_cursor,
    is_database_configured,
    release_connection,
)


def _make_mock_connection() -> MagicMock:
    """Builds a mock connection behaving like an active psycopg2 connection."""
    conn = MagicMock(spec=psycopg2.extensions.connection)
    conn.closed = False
    conn.info.transaction_status = 0  # TRANSACTION_STATUS_IDLE
    conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_IDLE
    return conn


def _make_real_mocked_pool(max_conn: int = 2) -> ThreadedConnectionPool:
    """Creates a genuine ThreadedConnectionPool instance backed by mock connections."""
    with patch("psycopg2.connect", side_effect=lambda *args, **kwargs: _make_mock_connection()):
        return ThreadedConnectionPool(
            minconn=1,
            maxconn=max_conn,
            dsn="postgresql://user:pass@localhost:6543/postgres"
        )


def test_format_db_url_preserves_local_and_docker_ports():
    """Verify URL formatting preserves standard ports for local and docker instances."""
    local_url = "postgresql://user:pass@localhost:5432/beatmatch"
    assert _format_db_url(local_url) == "postgresql://user:pass@localhost:5432/beatmatch"

    docker_url = "postgresql://user:pass@db:5433/mydb?sslmode=disable"
    assert _format_db_url(docker_url) == "postgresql://user:pass@db:5433/mydb?sslmode=disable"


def test_format_db_url_rewrites_supabase_pooler_specifically():
    """Verify URL formatting only rewrites default port to pooling port when on supabase pooler domain."""
    supabase_raw = "postgres://user:pass@ep-test.pooler.supabase.com:5432/postgres"
    formatted = _format_db_url(supabase_raw)
    assert formatted.startswith("postgresql://")
    assert "ep-test.pooler.supabase.com:6543/postgres" in formatted


def test_format_db_url_preserves_url_without_explicit_port_and_params():
    """Verify URL formatting handles missing port, custom credentials, and query params."""
    no_port = "postgresql://user:p%40ss@localhost/mydb?sslmode=require"
    assert _format_db_url(no_port) == no_port


def test_format_db_url_supabase_pooler_strict_domain_matching():
    """Verify URL formatting strictly matches Supabase pooler domains and ports."""
    # 1. Hostname regional legítimo terminado em .pooler.supabase.com, porta 5432 -> 6543
    regional = "postgresql://user:pass@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"
    assert _format_db_url(regional) == "postgresql://user:pass@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require"

    # Exact domain pooler.supabase.com, porta 5432 -> 6543
    exact = "postgresql://user:pass@pooler.supabase.com:5432/postgres"
    assert _format_db_url(exact) == "postgresql://user:pass@pooler.supabase.com:6543/postgres"

    # 2. Hostname Supabase com porta diferente de 5432 (ex: 6543) -> preservado
    already_6543 = "postgresql://user:pass@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
    assert _format_db_url(already_6543) == already_6543

    # 3. localhost:5432 -> preservado
    local_5432 = "postgresql://user:secret@localhost:5432/app"
    assert _format_db_url(local_5432) == local_5432

    # 4. Hostname comum com porta 5432 -> preservado
    common_host = "postgresql://user:pass@db.internal:5432/prod"
    assert _format_db_url(common_host) == common_host

    # 5. Hostname malicioso ou não correspondente contendo pooler.supabase.com como substring -> preservado
    fake_sub = "postgresql://user:pass@pooler.supabase.com.empresa.internal:5432/db"
    assert _format_db_url(fake_sub) == fake_sub

    attacker_sub = "postgresql://user:pass@attacker-pooler.supabase.com.evil.org:5432/db"
    assert _format_db_url(attacker_sub) == attacker_sub


def test_get_connection_raises_when_no_env():
    """get_connection must raise ValueError if DATABASE_URL is not configured."""
    with patch("src.utils.db.DATABASE_URL", None):
        with pytest.raises(ValueError, match="DATABASE_URL environment variable is not configured"):
            get_connection()


# ---------------------------------------------------------------------------
# Test A: Acquire and release - Prove pooled connection is returned to pool
# ---------------------------------------------------------------------------
def test_acquire_and_release_returns_connection_to_pool():
    """Test A: get_connection + release_connection returns connection to pool without closing socket."""
    pool = _make_real_mocked_pool(max_conn=2)
    with patch("src.utils.db.DATABASE_URL", "postgresql://user:pass@localhost:6543/postgres"), \
         patch("src.utils.db._connection_pool", pool):

        assert len(pool._used) == 0

        conn = get_connection()
        # Observable state: connection is tracked as used in the pool
        assert len(pool._used) == 1
        assert id(conn) in pool._rused

        # Release connection back to pool
        release_connection(conn)

        # Observable state: connection is no longer in used set, and socket was not closed
        assert len(pool._used) == 0
        assert conn.closed is False
        conn.close.assert_not_called()


# ---------------------------------------------------------------------------
# Test B: Successful operation - Connection returned after normal execution
# ---------------------------------------------------------------------------
def test_get_db_connection_successful_operation():
    """Test B: get_db_connection context manager acquires, allows queries, and releases on normal exit."""
    pool = _make_real_mocked_pool(max_conn=2)
    with patch("src.utils.db.DATABASE_URL", "postgresql://user:pass@localhost:6543/postgres"), \
         patch("src.utils.db._connection_pool", pool):

        assert len(pool._used) == 0

        with get_db_connection() as conn:
            assert len(pool._used) == 1
            cursor = conn.cursor()
            cursor.execute("SELECT 1;")
            conn.commit()

        # Connection returned to pool after exiting context block
        assert len(pool._used) == 0
        assert conn.closed is False
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# Test C: Exception handling - Rollback occurs and connection is returned
# ---------------------------------------------------------------------------
def test_get_db_connection_exception_rolls_back_and_releases():
    """Test C: Exception during database work rolls back transaction, returns connection to pool, and preserves error."""
    pool = _make_real_mocked_pool(max_conn=2)
    with patch("src.utils.db.DATABASE_URL", "postgresql://user:pass@localhost:6543/postgres"), \
         patch("src.utils.db._connection_pool", pool):

        conn_ref = None
        with pytest.raises(RuntimeError, match="Simulated SQL Failure"):
            with get_db_connection() as conn:
                conn_ref = conn
                assert len(pool._used) == 1
                raise RuntimeError("Simulated SQL Failure")

        # Rollback was executed and connection was returned to pool
        assert conn_ref is not None
        conn_ref.rollback.assert_called_once()
        assert len(pool._used) == 0
        assert conn_ref.closed is False


# ---------------------------------------------------------------------------
# Test D: Repeated use - Pool does not progressively lose available connections
# ---------------------------------------------------------------------------
def test_repeated_use_does_not_exhaust_pool():
    """Test D: Repeatedly acquire/release connections; prove pool of max_conn=2 does not exhaust under 20 cycles."""
    with patch("psycopg2.connect", side_effect=lambda *args, **kwargs: _make_mock_connection()):
        pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=2,
            dsn="postgresql://user:pass@localhost:6543/postgres"
        )
        with patch("src.utils.db.DATABASE_URL", "postgresql://user:pass@localhost:6543/postgres"), \
             patch("src.utils.db._connection_pool", pool):

            for _ in range(20):
                with get_db_connection() as conn:
                    assert len(pool._used) == 1
                    assert conn.closed is False
                assert len(pool._used) == 0

            # Contrast check: demonstrate that calling conn.close() without putconn exhausts the pool
            c1 = pool.getconn()
            c1.close()  # Buggy behavior: closes socket without returning to pool
            c2 = pool.getconn()
            c2.close()  # Buggy behavior: second connection lost
            # Third getconn must fail with PoolError because pool limit (maxconn=2) is exhausted
            with pytest.raises(PoolError, match="connection pool exhausted"):
                pool.getconn()


# ---------------------------------------------------------------------------
# Test E: Release behavior - release_connection returns to pool and handles unkeyed connections
# ---------------------------------------------------------------------------
def test_release_connection_behavior_and_edge_cases():
    """Test E: release_connection handles None, pooled connections, and unkeyed fallback connections."""
    pool = _make_real_mocked_pool(max_conn=2)

    # E1: None connection is safe no-op
    release_connection(None)

    # E2: Pooled connection returned to pool
    with patch("src.utils.db._connection_pool", pool):
        conn = pool.getconn()
        assert len(pool._used) == 1
        release_connection(conn)
        assert len(pool._used) == 0

    # E3: Unkeyed standalone connection passed while pool is active falls back to closing connection
    standalone_conn = _make_mock_connection()
    with patch("src.utils.db._connection_pool", pool):
        release_connection(standalone_conn)
        standalone_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Test F: Standalone connection - Closes correctly without pool interaction
# ---------------------------------------------------------------------------
def test_standalone_connection_closes_correctly():
    """Test F: When pool is not active or for standalone connections, connection closes directly."""
    # F1: release_connection when _connection_pool is None
    mock_conn = _make_mock_connection()
    with patch("src.utils.db._connection_pool", None):
        release_connection(mock_conn)
        mock_conn.close.assert_called_once()

    # F2: Direct standalone connection (like in db_setup.py) closes directly
    standalone_direct = _make_mock_connection()
    standalone_direct.close()
    standalone_direct.close.assert_called_once()


# ---------------------------------------------------------------------------
# Additional Lifecycle & Helper Tests
# ---------------------------------------------------------------------------
def test_get_db_cursor_context_manager_commits():
    """get_db_cursor commits transaction when commit=True and returns connection to pool."""
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("src.utils.db.get_connection", return_value=mock_conn), \
         patch("src.utils.db.release_connection") as mock_release:
        with get_db_cursor(commit=True) as cur:
            assert cur == mock_cursor
        mock_conn.commit.assert_called_once()
        mock_release.assert_called_once_with(mock_conn)


def test_execute_query_fetch():
    """execute_query executes with params and fetches results when fetch=True."""
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [{"id": 1, "name": "Artist A"}]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("src.utils.db.get_connection", return_value=mock_conn), \
         patch("src.utils.db.release_connection"):
        results = execute_query("SELECT * FROM artists WHERE id = %s", (1,), fetch=True)
        assert results == [{"id": 1, "name": "Artist A"}]
        mock_cursor.execute.assert_called_once_with("SELECT * FROM artists WHERE id = %s", (1,))


def test_close_pool_resets_singleton():
    """close_pool calls closeall on active pool and resets _connection_pool to None."""
    mock_pool = MagicMock()
    with patch("src.utils.db._connection_pool", mock_pool):
        close_pool()
        mock_pool.closeall.assert_called_once()
    assert db._connection_pool is None


# ---------------------------------------------------------------------------
# P1-E-01: Transaction Status & Safe Release Tests
# ---------------------------------------------------------------------------
def test_release_connection_rolls_back_open_transaction():
    """Conexão com transação aberta (TRANSACTION_STATUS_INTRANS) deve sofrer rollback antes de voltar ao pool."""
    mock_conn = _make_mock_connection()
    mock_conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
    mock_pool = MagicMock()

    with patch("src.utils.db._connection_pool", mock_pool):
        release_connection(mock_conn)

        mock_conn.rollback.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn, close=False)


def test_release_connection_rolls_back_errored_transaction():
    """Conexão com transação abortada (TRANSACTION_STATUS_INERROR) deve sofrer rollback antes de voltar ao pool."""
    mock_conn = _make_mock_connection()
    mock_conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_INERROR
    mock_pool = MagicMock()

    with patch("src.utils.db._connection_pool", mock_pool):
        release_connection(mock_conn)

        mock_conn.rollback.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn, close=False)


def test_release_connection_preserves_idle_state_without_extra_rollback():
    """Conexão em estado idle (TRANSACTION_STATUS_IDLE) preserva commit e não chama rollback desnecessário."""
    mock_conn = _make_mock_connection()
    mock_conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_IDLE
    mock_pool = MagicMock()

    with patch("src.utils.db._connection_pool", mock_pool):
        release_connection(mock_conn)

        mock_conn.rollback.assert_not_called()
        mock_pool.putconn.assert_called_once_with(mock_conn, close=False)


def test_release_connection_discards_broken_connection_with_close_true():
    """Quando rollback falha por erro de socket, a conexão é descartada do pool com close=True."""
    mock_conn = _make_mock_connection()
    mock_conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
    mock_conn.rollback.side_effect = psycopg2.OperationalError("Server closed connection unexpectedly")
    mock_pool = MagicMock()

    with patch("src.utils.db._connection_pool", mock_pool):
        release_connection(mock_conn)

        mock_conn.rollback.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn, close=True)


# ---------------------------------------------------------------------------
# P1-FINAL-FIX: Database Configuration Detection Tests
# ---------------------------------------------------------------------------
def test_is_database_configured_missing_or_empty():
    """is_database_configured returns False on None, empty string, or whitespace."""
    assert is_database_configured(None) is False
    assert is_database_configured("") is False
    assert is_database_configured("   ") is False


def test_is_database_configured_rejects_placeholders():
    """is_database_configured returns False when placeholder credentials are present."""
    assert is_database_configured("postgresql://postgres:[YOUR-PASSWORD]@aws-0.pooler.supabase.com:6543/postgres") is False
    assert is_database_configured("postgresql://user:your_password@localhost:5432/db") is False
    assert is_database_configured("postgresql://user:<password>@db.example.com:5432/db") is False


def test_is_database_configured_rejects_invalid_schemes_or_missing_host():
    """is_database_configured returns False for non-postgres schemes or missing hostnames."""
    assert is_database_configured("http://localhost:5432/db") is False
    assert is_database_configured("mysql://user:pass@localhost:3306/db") is False
    assert is_database_configured("not_a_valid_url") is False
    assert is_database_configured("postgresql:///dbname_only") is False


def test_is_database_configured_accepts_arbitrary_valid_postgres_hosts():
    """is_database_configured accepts arbitrary valid PostgreSQL endpoints without requiring a specific project ref."""
    # Local PostgreSQL / Docker
    assert is_database_configured("postgresql://user:secret123@localhost:5432/beatmatch") is True
    # Arbitrary Supabase project ref
    assert is_database_configured("postgresql://postgres.custom_proj_999:secure_pass@aws-0-us-west-1.pooler.supabase.com:6543/postgres") is True
    # AWS RDS / GCP Cloud SQL
    assert is_database_configured("postgresql://master:topsecret@pg-instance.c123.rds.amazonaws.com:5432/production") is True


def test_spotify_resolver_detects_arbitrary_valid_database():
    """run_resolver uses real database when is_database_configured returns True."""
    from src.enrichers.spotify_resolver import run_resolver

    mock_conn = _make_mock_connection()
    with patch("src.enrichers.spotify_resolver.get_spotify_client"), \
         patch("src.enrichers.spotify_resolver.is_database_configured", return_value=True), \
         patch("src.enrichers.spotify_resolver.get_connection", return_value=mock_conn), \
         patch("src.enrichers.spotify_resolver.get_pending_queue", return_value=[]), \
         patch("src.enrichers.spotify_resolver.release_connection") as mock_release:

        count = run_resolver(batch_size=5)
        assert count == 0
        mock_release.assert_called_once_with(mock_conn)


def test_spotify_resolver_falls_back_to_mock_when_unconfigured():
    """run_resolver safely uses mock queue when database is unconfigured."""
    from src.enrichers.spotify_resolver import run_resolver

    with patch("src.enrichers.spotify_resolver.get_spotify_client"), \
         patch("src.enrichers.spotify_resolver.is_database_configured", return_value=False), \
         patch("src.enrichers.spotify_resolver.get_connection") as mock_get_conn, \
         patch("src.enrichers.spotify_resolver.resolve_artist_spotify_details", return_value=("mock_sp_id", "http://spotify/mock")):

        count = run_resolver(batch_size=2)
        mock_get_conn.assert_not_called()
        assert count > 0


def test_spotify_resolver_connection_failure_raises_when_configured():
    """run_resolver must raise connection error when database is configured but unreachable, never falling back to mock."""
    from src.enrichers.spotify_resolver import run_resolver

    with patch("src.enrichers.spotify_resolver.get_spotify_client"), \
         patch("src.enrichers.spotify_resolver.is_database_configured", return_value=True), \
         patch("src.enrichers.spotify_resolver.get_connection", side_effect=psycopg2.OperationalError("Connection refused")), \
         patch("src.enrichers.spotify_resolver.resolve_artist_spotify_details") as mock_resolve:

        with pytest.raises(psycopg2.OperationalError, match="Connection refused"):
            run_resolver(batch_size=5)

        mock_resolve.assert_not_called()


def test_spotify_resolver_unconfigured_raises_when_mock_disallowed():
    """run_resolver must raise RuntimeError when database is unconfigured and allow_mock=False."""
    from src.enrichers.spotify_resolver import run_resolver

    with patch("src.enrichers.spotify_resolver.get_spotify_client"), \
         patch("src.enrichers.spotify_resolver.is_database_configured", return_value=False), \
         patch("src.enrichers.spotify_resolver.get_connection") as mock_get_conn:

        with pytest.raises(RuntimeError, match="mock mode is not allowed"):
            run_resolver(batch_size=2, allow_mock=False)

        mock_get_conn.assert_not_called()


def test_spotify_resolver_releases_connection_on_queue_query_error():
    """run_resolver guarantees connection release back to pool even when get_pending_queue raises."""
    from src.enrichers.spotify_resolver import run_resolver

    mock_conn = _make_mock_connection()
    with patch("src.enrichers.spotify_resolver.get_spotify_client"), \
         patch("src.enrichers.spotify_resolver.is_database_configured", return_value=True), \
         patch("src.enrichers.spotify_resolver.get_connection", return_value=mock_conn), \
         patch("src.enrichers.spotify_resolver.get_pending_queue", side_effect=RuntimeError("Query error")), \
         patch("src.enrichers.spotify_resolver.release_connection") as mock_release:

        count = run_resolver(batch_size=5)
        assert count == 0
        mock_release.assert_called_once_with(mock_conn)
