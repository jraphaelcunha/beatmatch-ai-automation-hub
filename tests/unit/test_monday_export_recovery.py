"""
Unit tests for Monday.com Work OS export worker recovery and ambiguous state retention.
Validates P1-E FIX-5 requirements:
1. Socket timeout retains 'processing' state.
2. Network transport failure retains 'processing' state without automated queue rollback.
3. GraphQL error responses retain 'processing' state under conservative policy.
4. Confirmed remote creation with local DB persistence failure retains 'processing' state.
5. Pre-dispatch failure prior to network call safely reverts to 'ready_for_sipa'.
6. Optimistic CAS concurrency guards prevent collisions between concurrent workers.
"""

import io
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src.utils.export_to_monday import run_export


def _make_mock_records():
    return [
        (
            "spotify_101",
            "Artist Alpha",
            ["trap", "hip-hop"],
            1500,
            45,
            "https://open.spotify.com/artist/spotify_101",
            "https://instagram.com/alpha",
            None,
            None,
            "scout_bot",
            5000,
            12000
        )
    ]


def _make_mock_batch_records():
    return [
        (
            "spotify_101",
            "Artist Alpha",
            ["trap", "hip-hop"],
            1500,
            45,
            "https://open.spotify.com/artist/spotify_101",
            "https://instagram.com/alpha",
            None,
            None,
            "scout_bot",
            5000,
            12000,
        ),
        (
            "spotify_102",
            "Artist Beta",
            ["r&b"],
            2500,
            55,
            "https://open.spotify.com/artist/spotify_102",
            "https://instagram.com/beta",
            None,
            None,
            "scout_bot",
            8000,
            15000,
        ),
    ]


def test_export_monday_timeout_retains_processing_state():
    """Socket timeout during create_item leaves lead in 'processing' to prevent duplicates."""
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", side_effect=RuntimeError("Monday API connection failure: timed out")), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        result = run_export()
        assert result is True

        # Lead transitioned to processing
        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions

        # Crucial: Under timeout, lead was NOT reverted back to ready_for_sipa
        assert ("spotify_101", "ready_for_sipa", "processing") not in status_transitions
        assert ("spotify_101", "processed", "processing") not in status_transitions


def test_export_monday_transport_error_does_not_revert_to_queue():
    """Transport failure (e.g. HTTP 502/503) retains 'processing' state without rollback."""
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", side_effect=RuntimeError("Monday API HTTP Error 502: Bad Gateway")), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        result = run_export()
        assert result is True

        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions
        assert ("spotify_101", "ready_for_sipa", "processing") not in status_transitions


def test_export_monday_graphql_errors_retains_processing_state():
    """GraphQL errors retain 'processing' state under conservative policy."""
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", return_value={"errors": [{"message": "Column value format error"}]}), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        result = run_export()
        assert result is True

        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions
        # Conservative policy: do NOT revert to ready_for_sipa
        assert ("spotify_101", "ready_for_sipa", "processing") not in status_transitions


def test_export_monday_confirmed_creation_db_failure_retains_processing():
    """Confirmed remote creation followed by PostgreSQL update failure retains 'processing' state."""
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        if new_status == "processed":
            # Simulate local database failure
            return False
        return True

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", return_value={"data": {"create_item": {"id": "monday_item_555"}}}), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        result = run_export()
        assert result is True

        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions
        assert ("spotify_101", "processed", "processing") in status_transitions
        # Not reverted to ready_for_sipa: lead remains in processing
        assert ("spotify_101", "ready_for_sipa", "processing") not in status_transitions


def test_export_monday_pre_dispatch_error_safely_reverts_to_ready_for_sipa():
    """Pre-dispatch failure prior to network transmission safely returns lead to 'ready_for_sipa'."""
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    # Simulate serialization failure during local payload preparation (before any network call)
    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday.json.dumps", side_effect=TypeError("Non-serializable object")), \
         patch("src.utils.export_to_monday._call_api") as mock_api, \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        result = run_export()
        assert result is True

        # Network API was NEVER called
        mock_api.assert_not_called()

        # Proven pre-dispatch failure: safely returned to ready_for_sipa
        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions
        assert ("spotify_101", "ready_for_sipa", "processing") in status_transitions


def test_export_monday_concurrent_workers_protected_by_cas():
    """Two concurrent workers competing for the same lead are protected by CAS."""
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    # Simulate Worker 2 attempting to claim lead already locked by Worker 1 (rowcount=0)
    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday.transition_status", return_value=False) as mock_cas, \
         patch("src.utils.export_to_monday._call_api") as mock_api:

        result = run_export()
        assert result is True

        # CAS rejected lock acquisition
        mock_cas.assert_called_once_with("spotify_101", "processing", current_status="ready_for_sipa")
        # Worker 2 never made external API calls
        mock_api.assert_not_called()


@pytest.mark.parametrize(
    "malformed_payload",
    [
        {"data": None},
        {"data": {}},
        {"data": {"create_item": None}},
        {"data": {"create_item": {}}},
        {"data": {"create_item": {"id": ""}}},
        "unexpected_non_dict_string",
        None,
    ],
)
def test_export_monday_unexpected_graphql_payload_retains_processing_and_continues_batch(malformed_payload):
    """
    P1-E FIX-7 Requirement 1:
    Unexpected, malformed, or missing-ID GraphQL responses must NOT crash the batch.
    The ambiguous lead must retain 'processing' state (never reverted to ready_for_sipa),
    and subsequent records in the batch must continue processing normally.
    """
    records = _make_mock_batch_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    # Record 1 receives malformed_payload; Record 2 receives valid create_item and create_update
    api_responses = [
        malformed_payload,
        {"data": {"create_item": {"id": "monday_valid_202"}}},
        {"data": {"create_update": {"id": "update_999"}}},
    ]

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", side_effect=api_responses), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        result = run_export()
        assert result is True

        # Lead 1 transitioned to processing but NOT reverted to ready_for_sipa and NOT processed
        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions
        assert ("spotify_101", "ready_for_sipa", "processing") not in status_transitions
        assert ("spotify_101", "processed", "processing") not in status_transitions

        # Lead 2 was still processed in the same batch (loop did not crash)
        assert ("spotify_102", "processing", "ready_for_sipa") in status_transitions
        assert ("spotify_102", "processed", "processing") in status_transitions


def test_export_monday_create_update_failure_still_transitions_to_processed(caplog):
    """
    P1-E FIX-7 Requirement 2:
    When create_item succeeds with valid ID, but secondary create_update mutation fails,
    the lead must NOT revert to ready_for_sipa, the failure must be logged without leaking
    credentials, and local state must advance to 'processed'.
    """
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    secret_token = "monday_test_token_update_leak_check_999"

    # Call 1 (create_item): succeeds with ID
    # Call 2 (create_update): fails with transport error
    api_side_effects = [
        {"data": {"create_item": {"id": "monday_item_777"}}},
        RuntimeError("Monday API connection failure: socket hangup during update"),
    ]

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.MONDAY_API_TOKEN", secret_token), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", side_effect=api_side_effects), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        with caplog.at_level(logging.DEBUG):
            result = run_export()
            assert result is True

        # State transitions: ready_for_sipa -> processing -> processed
        assert ("spotify_101", "processing", "ready_for_sipa") in status_transitions
        assert ("spotify_101", "processed", "processing") in status_transitions

        # Crucial: NOT reverted to ready_for_sipa
        assert ("spotify_101", "ready_for_sipa", "processing") not in status_transitions

        # Logs verification: warning was recorded, secret was not leaked
        assert "Error creating update block for artist 'Artist Alpha'" in caplog.text
        assert secret_token not in caplog.text


def test_export_monday_transport_error_does_not_leak_api_token(caplog):
    """
    P1-E FIX-7 Requirement 3:
    pytest caplog test confirming MONDAY_API_TOKEN does not leak into log messages
    when transport failures occur during Monday.com API calls.
    """
    records = _make_mock_records()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = records
    mock_conn.cursor.return_value = mock_cursor

    secret_token = "monday_super_secret_token_xyz_88888"

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        return True

    # Case A: URLError (Network failure)
    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.MONDAY_API_TOKEN", secret_token), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused by Monday gateway")):

        with caplog.at_level(logging.DEBUG):
            result = run_export()
            assert result is True

        assert "Transport failure or timeout during Monday.com item creation" in caplog.text
        assert secret_token not in caplog.text

    caplog.clear()

    # Case B: HTTPError (HTTP 502 Bad Gateway)
    http_error_resp = urllib.error.HTTPError(
        "https://api.monday.com/v2",
        502,
        "Bad Gateway",
        {},
        io.BytesIO(b'{"errors": ["Bad gateway upstream"]}')
    )

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.MONDAY_API_TOKEN", secret_token), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_sp"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status), \
         patch("urllib.request.urlopen", side_effect=http_error_resp):

        with caplog.at_level(logging.DEBUG):
            result = run_export()
            assert result is True

        assert "Transport failure or timeout during Monday.com item creation" in caplog.text
        assert secret_token not in caplog.text
