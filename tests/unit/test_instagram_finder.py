"""
Unit tests for Instagram Finder enrichment flow, connection management,
and mock fallback policies in BeatMatch AI Automation Hub.
"""

from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from src.enrichers.instagram_finder import resolve_instagram_for_pending_artists


def _make_mock_connection() -> MagicMock:
    """Builds a mock connection behaving like an active, open psycopg2 connection."""
    conn = MagicMock(spec=psycopg2.extensions.connection)
    conn.closed = False
    conn.info.transaction_status = 0
    return conn


def test_instagram_finder_unconfigured_runs_mock_when_allowed():
    """When database is unconfigured, resolve_instagram_for_pending_artists runs mock queue without DB connection."""
    with patch("src.enrichers.instagram_finder.is_database_configured", return_value=False), \
         patch("src.enrichers.instagram_finder.get_connection") as mock_get_conn, \
         patch("src.enrichers.instagram_finder.extract_instagram_from_spotify", return_value={"instagram_url": "https://instagram.com/mock", "monthly_listeners": 500, "max_song_streams": 1000}), \
         patch("src.enrichers.instagram_finder.find_instagram_via_search", return_value="https://instagram.com/mock"), \
         patch("src.enrichers.instagram_finder.get_max_youtube_views", return_value=500), \
         patch("src.enrichers.instagram_finder.time.sleep"):

        count = resolve_instagram_for_pending_artists(batch_size=2, allow_mock=True)
        mock_get_conn.assert_not_called()
        assert count > 0


def test_instagram_finder_unconfigured_raises_when_mock_disallowed():
    """When database is unconfigured and allow_mock=False, raises RuntimeError without attempting DB connection."""
    with patch("src.enrichers.instagram_finder.is_database_configured", return_value=False), \
         patch("src.enrichers.instagram_finder.get_connection") as mock_get_conn:

        with pytest.raises(RuntimeError, match="mock mode is not allowed"):
            resolve_instagram_for_pending_artists(batch_size=2, allow_mock=False)

        mock_get_conn.assert_not_called()


def test_instagram_finder_configured_successful_flow():
    """When database is configured and accessible, queries pending queue and transitions qualified lead."""
    mock_conn = _make_mock_connection()
    mock_records = [
        ("spot_real_1", "Real Indie Artist", "https://open.spotify.com/artist/spot_real_1", None, None, None, "spotify_miner")
    ]

    with patch("src.enrichers.instagram_finder.is_database_configured", return_value=True), \
         patch("src.enrichers.instagram_finder.get_connection", return_value=mock_conn), \
         patch("src.enrichers.instagram_finder.get_pending_queue", return_value=mock_records), \
         patch("src.enrichers.instagram_finder.release_connection") as mock_release, \
         patch("src.enrichers.instagram_finder.extract_instagram_from_spotify", return_value={"instagram_url": "https://instagram.com/real_indie", "monthly_listeners": 2500, "max_song_streams": 3000}), \
         patch("src.enrichers.instagram_finder.get_max_youtube_views", return_value=1200), \
         patch("src.enrichers.instagram_finder.transition_status", return_value=True) as mock_trans, \
         patch("src.enrichers.instagram_finder.time.sleep"):

        count = resolve_instagram_for_pending_artists(batch_size=1)
        assert count == 1
        mock_release.assert_called_once_with(mock_conn)
        mock_trans.assert_called_once_with(
            spotify_id="spot_real_1",
            new_status="ready_for_sipa",
            processed_fields={
                "instagram_url": "https://instagram.com/real_indie",
                "monthly_listeners": 2500,
                "max_song_views": 3000
            },
            current_status="pending_instagram"
        )


def test_instagram_finder_configured_connection_failure_raises():
    """When database is configured but connection fails, raises error and does NOT fall back to mock."""
    with patch("src.enrichers.instagram_finder.is_database_configured", return_value=True), \
         patch("src.enrichers.instagram_finder.get_connection", side_effect=psycopg2.OperationalError("Connection refused")), \
         patch("src.enrichers.instagram_finder.extract_instagram_from_spotify") as mock_extract:

        with pytest.raises(psycopg2.OperationalError, match="Connection refused"):
            resolve_instagram_for_pending_artists(batch_size=2)

        mock_extract.assert_not_called()


def test_instagram_finder_releases_connection_on_queue_query_error():
    """Guarantees connection release back to pool even when get_pending_queue raises."""
    mock_conn = _make_mock_connection()

    with patch("src.enrichers.instagram_finder.is_database_configured", return_value=True), \
         patch("src.enrichers.instagram_finder.get_connection", return_value=mock_conn), \
         patch("src.enrichers.instagram_finder.get_pending_queue", side_effect=RuntimeError("Query failure")), \
         patch("src.enrichers.instagram_finder.release_connection") as mock_release:

        count = resolve_instagram_for_pending_artists(batch_size=5)
        assert count == 0
        mock_release.assert_called_once_with(mock_conn)


def test_instagram_finder_blacklist_skips_without_crawling():
    """Artists matching blacklist are skipped directly without invoking external web crawlers."""
    mock_conn = _make_mock_connection()
    mock_records = [
        ("spot_famous_1", "Duquesa", "https://open.spotify.com/artist/spot_famous_1", None, None, None, "miner")
    ]

    with patch("src.enrichers.instagram_finder.is_database_configured", return_value=True), \
         patch("src.enrichers.instagram_finder.get_connection", return_value=mock_conn), \
         patch("src.enrichers.instagram_finder.get_pending_queue", return_value=mock_records), \
         patch("src.enrichers.instagram_finder.release_connection") as mock_release, \
         patch("src.enrichers.instagram_finder.extract_instagram_from_spotify") as mock_crawl, \
         patch("src.enrichers.instagram_finder.transition_status", return_value=True) as mock_trans, \
         patch("src.enrichers.instagram_finder.time.sleep"):

        count = resolve_instagram_for_pending_artists(batch_size=1)
        assert count == 0
        mock_crawl.assert_not_called()
        mock_trans.assert_called_once_with("spot_famous_1", "skipped_too_famous", current_status="pending_instagram")
        mock_release.assert_called_once_with(mock_conn)
