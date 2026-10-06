"""
Unit tests for database connection pooling and cursor management.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.utils.db import (
    _format_db_url,
    execute_query,
    get_connection,
    get_db_cursor,
    release_connection,
)


def test_format_db_url():
    """Verify URL rewriting replaces postgres:// and rewrites default port to pooling port."""
    raw = "postgres://user:pass@ep-test.pooler.supabase.com:5432/postgres"
    formatted = _format_db_url(raw)
    assert formatted.startswith("postgresql://")
    assert ":6543" in formatted


def test_get_connection_raises_when_no_env():
    """get_connection must raise ValueError if DATABASE_URL is not configured."""
    with patch("src.utils.db.DATABASE_URL", None):
        with pytest.raises(ValueError, match="DATABASE_URL environment variable is not configured"):
            get_connection()


def test_release_connection_pool_active():
    """When pool is active, release_connection returns connection to pool."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    with patch("src.utils.db._connection_pool", mock_pool):
        release_connection(mock_conn)
        mock_pool.putconn.assert_called_once_with(mock_conn)


def test_release_connection_standalone():
    """When pool is not active, release_connection closes the connection."""
    mock_conn = MagicMock()
    with patch("src.utils.db._connection_pool", None):
        release_connection(mock_conn)
        mock_conn.close.assert_called_once()


def test_get_db_cursor_context_manager_commits():
    """get_db_cursor commits transaction when commit=True and closes connection."""
    mock_conn = MagicMock()
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
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [{"id": 1, "name": "Artist A"}]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("src.utils.db.get_connection", return_value=mock_conn), \
         patch("src.utils.db.release_connection"):
        results = execute_query("SELECT * FROM artists WHERE id = %s", (1,), fetch=True)
        assert results == [{"id": 1, "name": "Artist A"}]
        mock_cursor.execute.assert_called_once_with("SELECT * FROM artists WHERE id = %s", (1,))
