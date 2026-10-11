"""
Unit tests for concurrency control, optimistic locking (CAS), idempotency,
and race-condition protection in BeatMatch AI Automation Hub.
"""

from unittest.mock import MagicMock, patch

import psycopg2

from src.models.schemas import LeadStatus, validate_transition
from src.reconciler import transition_status
from src.utils.export_to_monday import run_export


def _make_mock_connection() -> MagicMock:
    """Builds a mock connection behaving like an active, open psycopg2 connection."""
    conn = MagicMock(spec=psycopg2.extensions.connection)
    conn.closed = False
    conn.info.transaction_status = 0
    return conn


def test_conditional_transition_succeeds_when_state_matches():
    """
    Test 1: Conditional transition succeeds.
    When expected current_status matches the database record, update affects 1 row,
    commits the transaction, and returns True.
    """
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.reconciler.get_connection", return_value=mock_conn), \
         patch("src.reconciler.release_connection") as mock_release:

        success = transition_status(
            spotify_id="spot_123",
            new_status=LeadStatus.PROCESSING,
            current_status=LeadStatus.READY_FOR_SIPA
        )

        assert success is True
        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()
        mock_release.assert_called_once_with(mock_conn)

        # Verify SQL query verified current_status atomically
        query_executed, params = mock_cursor.execute.call_args[0]
        assert "status = %s" in str(query_executed)
        assert LeadStatus.READY_FOR_SIPA.value in params
        assert LeadStatus.PROCESSING.value in params


def test_conditional_transition_rejected_after_concurrent_change():
    """
    Test 2: Conditional transition rejected after concurrent change (TOCTOU protection).
    Worker A assumes lead is in ready_for_sipa, but Worker B concurrently altered it.
    The conditional WHERE status = 'ready_for_sipa' matches 0 rows (rowcount == 0).
    Worker A must roll back, return False, and NOT overwrite Worker B's state.
    """
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 0  # 0 rows affected because status already changed in DB
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.reconciler.get_connection", return_value=mock_conn), \
         patch("src.reconciler.release_connection") as mock_release:

        success = transition_status(
            spotify_id="spot_123",
            new_status=LeadStatus.PROCESSED,
            current_status=LeadStatus.READY_FOR_SIPA
        )

        assert success is False
        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
        mock_release.assert_called_once_with(mock_conn)


def test_concurrent_claim_winner_and_loser():
    """
    Test 5: Concurrent claim race condition.
    Worker 1 and Worker 2 both attempt to claim the same lead (ready_for_sipa -> processing).
    Worker 1 executes first (rowcount == 1, returns True).
    Worker 2 executes second on the now-processing row (rowcount == 0, returns False).
    Only one worker wins the claim.
    """
    row_state = {"status": LeadStatus.READY_FOR_SIPA.value}

    def simulate_worker_claim(worker_id: str) -> bool:
        mock_conn = _make_mock_connection()
        mock_cursor = MagicMock()

        def fake_execute(query, params=None):
            # Check if WHERE status = expected matches row_state
            if params and LeadStatus.READY_FOR_SIPA.value in params:
                if row_state["status"] == LeadStatus.READY_FOR_SIPA.value:
                    row_state["status"] = LeadStatus.PROCESSING.value
                    mock_cursor.rowcount = 1
                else:
                    mock_cursor.rowcount = 0
            else:
                mock_cursor.rowcount = 0

        mock_cursor.execute.side_effect = fake_execute
        mock_conn.cursor.return_value = mock_cursor

        with patch("src.reconciler.get_connection", return_value=mock_conn):
            return transition_status(
                spotify_id="contested_lead_1",
                new_status=LeadStatus.PROCESSING,
                current_status=LeadStatus.READY_FOR_SIPA
            )

    worker_1_result = simulate_worker_claim("Worker 1")
    worker_2_result = simulate_worker_claim("Worker 2")

    # Worker 1 claimed the lead, Worker 2 lost and was rejected
    assert worker_1_result is True
    assert worker_2_result is False
    assert row_state["status"] == LeadStatus.PROCESSING.value


def test_idempotent_self_transition():
    """
    Test 3: Idempotent self-transition.
    Re-executing a transition to the same status (e.g. processing -> processing)
    is valid in the domain transition map and updates idempotently without state corruption.
    """
    assert validate_transition(LeadStatus.PROCESSING, LeadStatus.PROCESSING) is True

    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.reconciler.get_connection", return_value=mock_conn):
        success = transition_status(
            spotify_id="spot_123",
            new_status=LeadStatus.PROCESSING,
            current_status=LeadStatus.PROCESSING
        )
        assert success is True


def test_retry_idempotency_second_attempt_rejected():
    """
    Test 4: Retry idempotency.
    When a retry (failed_spotify -> pending_spotify) is executed by Worker A, it succeeds.
    If Worker B simultaneously or subsequently attempts the same retry, the conditional
    check (WHERE status = 'failed_spotify') returns 0 rows, preventing duplicate retry actions.
    """
    # 1. Domain transition is valid
    assert validate_transition(LeadStatus.FAILED_SPOTIFY, LeadStatus.PENDING_SPOTIFY) is True

    # 2. First retry succeeds
    mock_conn_1 = _make_mock_connection()
    mock_cursor_1 = MagicMock()
    mock_cursor_1.rowcount = 1
    mock_conn_1.cursor.return_value = mock_cursor_1

    with patch("src.reconciler.get_connection", return_value=mock_conn_1):
        retry_1 = transition_status(
            spotify_id="retry_lead_1",
            new_status=LeadStatus.PENDING_SPOTIFY,
            current_status=LeadStatus.FAILED_SPOTIFY
        )
        assert retry_1 is True

    # 3. Duplicate concurrent retry sees rowcount == 0 and fails cleanly
    mock_conn_2 = _make_mock_connection()
    mock_cursor_2 = MagicMock()
    mock_cursor_2.rowcount = 0  # Row already moved to pending_spotify
    mock_conn_2.cursor.return_value = mock_cursor_2

    with patch("src.reconciler.get_connection", return_value=mock_conn_2):
        retry_2 = transition_status(
            spotify_id="retry_lead_1",
            new_status=LeadStatus.PENDING_SPOTIFY,
            current_status=LeadStatus.FAILED_SPOTIFY
        )
        assert retry_2 is False


def test_monday_crash_window_lead_retained_in_processing():
    """
    Test 6 & 7: Monday crash window and protection against automatic re-processing.
    When Monday item creation succeeds but the database transition to 'processed' fails:
    1. The lead remains in 'processing'.
    2. Because run_export() queries 'WHERE status = ready_for_sipa', the lead in
       'processing' is NOT selected again by normal export jobs, preventing automated duplicate creation.
    """
    records_ready = [
        ("lead_crash", "Test Artist", ["hip hop"], 500, 20, "url", "url", None, None, "youtube", 2000, 3000)
    ]

    mock_query_conn = _make_mock_connection()
    mock_query_cursor = MagicMock()
    mock_query_cursor.fetchall.return_value = records_ready
    mock_query_conn.cursor.return_value = mock_query_cursor

    transitions_log = []

    def mock_transition(spotify_id, new_status, current_status=None, **kwargs):
        transitions_log.append((new_status, current_status))
        if new_status == "processed":
            # Simulate DB connection drop / crash during the final transition to processed
            return False
        return True

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_1"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_query_conn), \
         patch("src.utils.export_to_monday.release_connection"), \
         patch("src.utils.export_to_monday._call_api", return_value={"data": {"create_item": {"id": "monday_item_555"}}}), \
         patch("src.utils.export_to_monday.transition_status", side_effect=mock_transition):

        export_result = run_export()
        assert export_result is True

        # Verified transitions:
        # 1. Lead claimed: ready_for_sipa -> processing
        assert ("processing", "ready_for_sipa") in transitions_log
        # 2. Attempted completion: processing -> processed (which failed in DB)
        assert ("processed", "processing") in transitions_log
        # 3. Crucial guarantee: Lead was NOT reverted to ready_for_sipa
        assert ("ready_for_sipa", "processing") not in transitions_log


def test_lead_in_processing_is_not_queried_by_export():
    """
    Test 7 (Part 2): No duplicate automatic processing.
    Verify that the export SQL query explicitly requires status = 'ready_for_sipa',
    ensuring leads in 'processing' are immune to automated re-export.
    """
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_1"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_conn), \
         patch("src.utils.export_to_monday.release_connection"):

        run_export()

        # The query executed MUST filter WHERE status = 'ready_for_sipa'
        query_executed = mock_cursor.execute.call_args[0][0]
        assert "WHERE status = 'ready_for_sipa'" in query_executed
        assert "processing" not in query_executed
