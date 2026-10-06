"""
Unit tests for SIPA quality cleaning heuristics, fake detection, and dataset cleansing.
Verifies production is_fake_profile and run_sipa_engine from src.quality.sipa_cleaner.
"""

from unittest.mock import MagicMock, patch

import pandas as pd

from src.quality.sipa_cleaner import is_fake_profile, run_sipa_engine


def test_detects_generic_inactive_fake():
    """Generic music terms with 0 popularity and negligible followers must be flagged as fake."""
    assert is_fake_profile("Trap Type Beat", popularity=0, followers=1) is True
    assert is_fake_profile("Pop Music", popularity=0, followers=0) is True


def test_detects_short_inactive_name():
    """Single-character or empty names with zero popularity must be flagged as fake."""
    assert is_fake_profile("X", popularity=0, followers=2) is True
    assert is_fake_profile(" ", popularity=0, followers=0) is True


def test_preserves_real_artist_with_generic_name():
    """Legitimate established artists with generic words must NOT be flagged as fake."""
    assert is_fake_profile("Pop Smoke", popularity=80, followers=5000000) is False
    assert is_fake_profile("Trap Manny", popularity=45, followers=120000) is False


def test_run_sipa_engine_dry_run():
    """Dry run identifies fakes and duplicates without executing database write mutations."""
    sample_data = pd.DataFrame([
        {"spotify_id": "id1", "name": "Trap Type Beat", "popularity": 0, "followers": 1, "status": "pending_enrichment"},
        {"spotify_id": "id2", "name": "Real Artist", "popularity": 30, "followers": 500, "status": "pending_enrichment"},
        {"spotify_id": "id3", "name": "Real Artist", "popularity": 25, "followers": 400, "status": "pending_enrichment"},
    ])

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = [3]
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.quality.sipa_cleaner.get_connection", return_value=mock_conn), \
         patch("pandas.read_sql_query", return_value=sample_data):
        summary = run_sipa_engine(action="dry-run", fake_action="mark")

        assert summary["initial_count"] == 3
        assert summary["fakes_count"] == 1
        assert summary["duplicates_count"] == 1
        assert summary["action"] == "dry-run"
        assert mock_conn.commit.call_count == 0


def test_run_sipa_engine_apply_mark():
    """Apply mode marks fake records as garbage and commits updates."""
    sample_data = pd.DataFrame([
        {"spotify_id": "fake_1", "name": "Trap Type Beat", "popularity": 0, "followers": 0, "status": "pending_enrichment"},
        {"spotify_id": "artist_1", "name": "Valid Artist", "popularity": 40, "followers": 2000, "status": "pending_enrichment"},
    ])

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = [2]
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.quality.sipa_cleaner.get_connection", return_value=mock_conn), \
         patch("pandas.read_sql_query", return_value=sample_data):
        summary = run_sipa_engine(action="apply", fake_action="mark")

        assert summary["fakes_count"] == 1
        assert summary["action"] == "apply"
        assert mock_conn.commit.called
