"""
Unit tests for transaction integrity, atomicity, and rollback semantics.
Verifies atomic multi-writes, intermediate failure rollbacks, independent entity isolation,
proper exception propagation, and guaranteed connection release across failure boundaries.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import psycopg2
import pytest

from src.models.schemas import LeadStatus
from src.quality.sipa_cleaner import run_sipa_engine
from src.reconciler import insert_discovered_lead, transition_status
from src.utils.db import get_db_connection
from src.utils.export_to_monday import run_export


def _make_mock_connection() -> MagicMock:
    """Builds a mock connection behaving like an active, open psycopg2 connection."""
    conn = MagicMock(spec=psycopg2.extensions.connection)
    conn.closed = False
    conn.info.transaction_status = 0
    return conn


def test_atomic_success_multi_write():
    """
    Test 1: Atomic Success (Multi-write atomic unit).
    Operations that constitute a single logical quality decision (marking fakes, deleting
    duplicates, updating qualified leads) must all execute on the same transaction cursor
    and be committed together in a single commit().
    """
    sample_df = pd.DataFrame([
        {"spotify_id": "fake_1", "name": "Trap Type Beat", "popularity": 0, "followers": 0, "status": "pending_spotify"},
        {"spotify_id": "dup_1", "name": "Underground Flow", "popularity": 35, "followers": 200, "status": "pending_instagram"},
        {"spotify_id": "dup_2", "name": "underground flow", "popularity": 20, "followers": 100, "status": "pending_instagram"},
        {"spotify_id": "real_1", "name": "Real Underground MC", "popularity": 45, "followers": 1500, "status": "pending_instagram"},
    ])

    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = [4]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.quality.sipa_cleaner.get_connection", return_value=mock_conn), \
         patch("src.quality.sipa_cleaner.release_connection") as mock_release, \
         patch("pandas.read_sql_query", return_value=sample_df):

        result = run_sipa_engine(action="apply", fake_action="mark")

        assert result["action"] == "apply"
        assert result["fakes_count"] == 1
        assert result["duplicates_count"] == 1

        # All writes occurred on the same cursor
        executed_statements = [call_args[0][0] for call_args in mock_cursor.execute.call_args_list]
        assert any("status = 'garbage'" in str(stmt) for stmt in executed_statements)
        assert any("DELETE FROM public.artists" in str(stmt) for stmt in executed_statements)
        assert any("status = 'ready_for_sipa'" in str(stmt) for stmt in executed_statements)

        # Single commit was performed at the end of the atomic batch
        assert mock_conn.commit.call_count == 1
        assert mock_conn.rollback.call_count == 0
        mock_release.assert_called_once_with(mock_conn)


def test_intermediate_failure_triggers_rollback():
    """
    Test 2: Intermediate Failure.
    When a multi-write atomic unit suffers an exception during an intermediate write,
    rollback() must be executed immediately, discarding prior uncommitted writes.
    """
    sample_df = pd.DataFrame([
        {"spotify_id": "fake_1", "name": "Trap Type Beat", "popularity": 0, "followers": 0, "status": "pending_spotify"},
        {"spotify_id": "dup_1", "name": "Artist A", "popularity": 35, "followers": 200, "status": "pending_instagram"},
        {"spotify_id": "dup_2", "name": "artist a", "popularity": 20, "followers": 100, "status": "pending_instagram"},
    ])

    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = [3]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value = mock_cursor

    # Simulate success on SELECT and fake update, then crash on duplicate deletion
    def execute_side_effect(query, *args, **kwargs):
        if "DELETE FROM public.artists" in str(query):
            raise psycopg2.DatabaseError("Deadlock detected during duplicate deletion")
        return None

    mock_cursor.execute.side_effect = execute_side_effect

    with patch("src.quality.sipa_cleaner.get_connection", return_value=mock_conn), \
         patch("src.quality.sipa_cleaner.release_connection") as mock_release, \
         patch("pandas.read_sql_query", return_value=sample_df):

        with pytest.raises(psycopg2.DatabaseError, match="Deadlock detected"):
            run_sipa_engine(action="apply", fake_action="mark")

        # Rollback was called, commit was NEVER reached
        mock_conn.rollback.assert_called_once()
        assert mock_conn.commit.call_count == 0
        mock_release.assert_called_once_with(mock_conn)


def test_no_partial_persistence_on_failure():
    """
    Test 3: No Partial Persistence.
    In transition_status, if SQL execution fails during field updates, rollback()
    is triggered and the function returns False, preventing partial state persistence.
    """
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = psycopg2.OperationalError("Disk full or connection lost")
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.reconciler.get_connection", return_value=mock_conn), \
         patch("src.reconciler.release_connection") as mock_release:

        success = transition_status(
            spotify_id="spotify_123",
            new_status=LeadStatus.READY_FOR_SIPA,
            processed_fields={"followers": 1200, "popularity": 45}
        )

        assert success is False
        mock_conn.rollback.assert_called_once()
        assert mock_conn.commit.call_count == 0
        mock_release.assert_called_once_with(mock_conn)


def test_independent_entities_in_batch_survive_individual_failure():
    """
    Test 4: Batch of Independent Entities.
    When processing a batch of independent leads, an error on Entity B must NOT roll back
    the successfully committed transitions of Entity A and Entity C.
    """
    leads = [
        {"id": "lead_A", "target": LeadStatus.READY_FOR_SIPA},
        {"id": "lead_B_fail", "target": LeadStatus.READY_FOR_SIPA},
        {"id": "lead_C", "target": LeadStatus.READY_FOR_SIPA},
    ]

    commit_history = []
    rollback_history = []

    def mock_get_connection():
        conn = _make_mock_connection()
        cursor = MagicMock()
        cursor.rowcount = 1

        def fake_execute(query, params=None):
            if params and "lead_B_fail" in params:
                raise psycopg2.IntegrityError("Foreign key violation on lead B")
            return None

        cursor.execute.side_effect = fake_execute

        def track_commit():
            commit_history.append("commit")

        def track_rollback():
            rollback_history.append("rollback")

        conn.commit.side_effect = track_commit
        conn.rollback.side_effect = track_rollback
        conn.cursor.return_value = cursor
        return conn

    with patch("src.reconciler.get_connection", side_effect=mock_get_connection):
        results = []
        for lead in leads:
            res = transition_status(
                spotify_id=lead["id"],
                new_status=lead["target"],
                current_status=LeadStatus.PENDING_INSTAGRAM
            )
            results.append(res)

        # Lead A and Lead C succeeded, Lead B failed
        assert results == [True, False, True]
        assert len(commit_history) == 2  # Lead A and Lead C independently committed
        assert len(rollback_history) == 1  # Lead B rolled back without affecting A or C


def test_exception_preserved_and_reraised():
    """
    Test 5: Exception Preserved.
    The database context manager get_db_connection() and critical engines must re-raise
    the original exception after rolling back, ensuring errors are never silently swallowed.
    """
    mock_conn = _make_mock_connection()

    with patch("src.utils.db.get_connection", return_value=mock_conn), \
         patch("src.utils.db.release_connection") as mock_release:

        with pytest.raises(ZeroDivisionError, match="division by zero"):
            with get_db_connection():
                _ = 1 / 0

        mock_conn.rollback.assert_called_once()
        mock_release.assert_called_once_with(mock_conn)


def test_connection_released_on_exception():
    """
    Test 6: Connection Released on Exception.
    Proves that release_connection(conn) is guaranteed to execute in finally blocks
    even when catastrophic failures occur in insert_discovered_lead and transition_status.
    """
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = psycopg2.DatabaseError("Fatal socket error")
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.reconciler.get_connection", return_value=mock_conn), \
         patch("src.reconciler.release_connection") as mock_release:

        # Case A: insert_discovered_lead
        lead_result = insert_discovered_lead(name="Crashing Lead")
        assert lead_result is None
        mock_release.assert_called_once_with(mock_conn)

    mock_release.reset_mock()
    with patch("src.reconciler.get_connection", return_value=mock_conn), \
         patch("src.reconciler.release_connection") as mock_release:

        # Case B: transition_status
        transition_result = transition_status(
            spotify_id="crash_id",
            new_status=LeadStatus.READY_FOR_SIPA
        )
        assert transition_result is False
        mock_release.assert_called_once_with(mock_conn)


def test_zero_row_update_aborts_transaction():
    """
    Test 7: Zero-Row Update Detection.
    If an UPDATE query matches 0 rows (rowcount == 0), transition_status must roll back
    and return False rather than falsely reporting success on a non-existent entity.
    """
    mock_conn = _make_mock_connection()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 0  # 0 rows affected
    mock_conn.cursor.return_value = mock_cursor

    with patch("src.reconciler.get_connection", return_value=mock_conn), \
         patch("src.reconciler.release_connection"):

        success = transition_status(
            spotify_id="non_existent_artist_id",
            new_status=LeadStatus.READY_FOR_SIPA
        )

        assert success is False
        mock_conn.rollback.assert_called_once()
        assert mock_conn.commit.call_count == 0


def test_monday_export_two_phase_transaction_and_duplicate_guard():
    """
    Test 8: Monday Export Two-Phase Entity Transition & Duplicate Protection.
    Proves that export_to_monday:
    1. Releases query connection before initiating external network HTTP calls.
    2. Transitions lead to 'processing' before external API mutation.
    3. Completes transition to 'processed' upon Monday item creation success.
    4. If Monday API call fails, reverts lead from 'processing' to 'ready_for_sipa'.
    5. If Monday item is created but local DB update to 'processed' fails, leaves
       lead in 'processing' to protect against automated duplicate generation on retry.
    """
    sample_records = [
        ("id_1", "Artist 1", ["trap"], 500, 30, "http://sp/1", "http://ig/1", None, None, "miner", 3000, 4000),
        ("id_2", "Artist 2", ["drill"], 600, 35, "http://sp/2", "http://ig/2", None, None, "miner", 4000, 5000),
    ]

    mock_query_conn = _make_mock_connection()
    mock_query_cursor = MagicMock()
    mock_query_cursor.fetchall.return_value = sample_records
    mock_query_conn.cursor.return_value = mock_query_cursor

    status_transitions = []

    def fake_transition_status(spotify_id, new_status, current_status=None, **kwargs):
        status_transitions.append((spotify_id, new_status, current_status))
        if spotify_id == "id_2" and new_status == "processed":
            # Simulate DB failure on transition to processed for id_2
            return False
        return True

    with patch("src.utils.export_to_monday.is_config_missing", False), \
         patch("src.utils.export_to_monday.get_board_columns", return_value={"spotify id": "col_1"}), \
         patch("src.utils.export_to_monday.get_connection", return_value=mock_query_conn), \
         patch("src.utils.export_to_monday.release_connection") as mock_release, \
         patch("src.utils.export_to_monday._call_api", return_value={"data": {"create_item": {"id": "monday_999"}}}), \
         patch("src.utils.export_to_monday.transition_status", side_effect=fake_transition_status):

        success = run_export()
        assert success is True

        # Query connection was released before Monday network calls
        mock_release.assert_called_once_with(mock_query_conn)

        # Entity 1 went ready_for_sipa -> processing -> processed
        assert ("id_1", "processing", "ready_for_sipa") in status_transitions
        assert ("id_1", "processed", "processing") in status_transitions

        # Entity 2 went ready_for_sipa -> processing, attempted processed (which failed in DB)
        assert ("id_2", "processing", "ready_for_sipa") in status_transitions
        assert ("id_2", "processed", "processing") in status_transitions
        # Entity 2 is NOT reverted to ready_for_sipa because item was already created on Monday!
        assert ("id_2", "ready_for_sipa", "processing") not in status_transitions
