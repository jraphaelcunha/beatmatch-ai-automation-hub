"""
Unit tests for the reconciliation engine.
Verifies SQL query construction with psycopg2.sql, column allowlist validation, and temporary ID generation.
"""

from unittest.mock import MagicMock, patch

from src.reconciler import (
    get_pending_queue,
    insert_discovered_lead,
    transition_status,
)


def test_transition_status_rejects_disallowed_column():
    """Attempting to update an arbitrary/injected column must fail validation."""
    with patch("src.reconciler.get_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor

        result = transition_status(
            spotify_id="test_id_123",
            new_status="ready_for_sipa",
            processed_fields={"malicious_column; DROP TABLE users;--": "exploit"}
        )
        assert result is False
        mock_cursor.execute.assert_not_called()


def test_transition_status_accepts_allowed_columns():
    """Updating columns that belong to the allowlist must succeed and execute parameterized query."""
    with patch("src.reconciler.get_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor

        result = transition_status(
            spotify_id="test_id_123",
            new_status="ready_for_sipa",
            new_spotify_id="spotify_real_456",
            processed_fields={
                "name": "Updated Artist",
                "followers": 1500,
                "instagram_url": "https://www.instagram.com/artist"
            }
        )
        assert result is True
        assert mock_cursor.execute.called
        query_executed = mock_cursor.execute.call_args[0][0]
        assert "UPDATE public.artists" in str(query_executed)
        params = mock_cursor.execute.call_args[0][1]
        assert "ready_for_sipa" in params
        assert "spotify_real_456" in params
        assert "test_id_123" in params


def test_insert_discovered_lead_generates_sha256_temp_id():
    """When spotify_id is missing, lead insert must generate deterministic sha256-based temp ID."""
    with patch("src.reconciler.get_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("temp_abc123", "pending_spotify")
        mock_conn.return_value.cursor.return_value = mock_cursor

        result = insert_discovered_lead(name="Underground Artist", source="youtube_comment")
        assert result is not None
        assert mock_cursor.execute.called
        executed_params = mock_cursor.execute.call_args[0][1]
        temp_id = executed_params[0]
        assert temp_id.startswith("temp_")
        assert len(temp_id) == 17  # 'temp_' (5) + 12 hex chars


def test_get_pending_queue_retrieves_records():
    """get_pending_queue retrieves rows matching status filter."""
    with patch("src.reconciler.get_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [("id1", "Artist 1", None, None, None, None, "test")]
        mock_conn.return_value.cursor.return_value = mock_cursor

        leads = get_pending_queue("pending_spotify", limit=5)
        assert len(leads) == 1
        assert leads[0][1] == "Artist 1"
        mock_cursor.execute.assert_called_once()


def test_transition_status_handles_duplicate_key_collision():
    """When a duplicate key error occurs, reconciler rolls back and removes duplicate temporary lead."""
    with patch("src.reconciler.get_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = [
            Exception("duplicate key value violates unique constraint"),
            None
        ]
        mock_conn.return_value.cursor.return_value = mock_cursor

        result = transition_status(
            spotify_id="temp_old_lead",
            new_status="ready_for_sipa",
            new_spotify_id="spotify_existing_artist"
        )
        assert result is True
        mock_conn.return_value.rollback.assert_called_once()
