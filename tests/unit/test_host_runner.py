"""
Unit tests for the host_runner daemon.
Tests security hardening, authentication, argument validation, concurrency, and process management.
"""

import os
import signal
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Ensure HOST_RUNNER_TOKEN is set for importing host_runner in test environment
os.environ["HOST_RUNNER_TOKEN"] = "test-secret-token-12345"

from src.host_runner import (
    app,
    get_api_token,
    persist_job_finish,
    persist_job_start,
    run_script_async,
)


@pytest.fixture
def client():
    """Provides a Flask test client configured with testing mode."""
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def auth_headers():
    """Provides valid Bearer authentication headers."""
    return {"Authorization": "Bearer test-secret-token-12345"}


def test_auth_missing_token_returns_401(client):
    """Requests without Authorization header must be rejected with 401."""
    response = client.post("/run/sipa_cleaner")
    assert response.status_code == 401
    data = response.get_json()
    assert data["error"] == "Unauthorized"


def test_auth_invalid_token_returns_401(client):
    """Requests with incorrect Bearer token must be rejected with 401."""
    response = client.post(
        "/run/sipa_cleaner",
        headers={"Authorization": "Bearer invalid-wrong-token"}
    )
    assert response.status_code == 401
    data = response.get_json()
    assert data["error"] == "Unauthorized"


def test_token_fail_closed_when_env_missing():
    """If HOST_RUNNER_TOKEN is empty or missing, token acquisition must fail closed."""
    with patch.dict(os.environ, {"HOST_RUNNER_TOKEN": ""}):
        with pytest.raises(RuntimeError) as exc_info:
            get_api_token()
        assert "HOST_RUNNER_TOKEN must be configured" in str(exc_info.value)


def test_status_endpoint_does_not_leak_internals(client):
    """The /status endpoint must return bare status without leaking project paths or PIDs."""
    response = client.get("/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data == {"status": "ok"}
    assert "project_dir" not in data
    assert "python_bin" not in data
    assert "active_jobs" not in data


def test_run_unknown_script_returns_400(client, auth_headers):
    """Calling an unknown script must return 400 Bad Request."""
    response = client.post("/run/non_existent_script", headers=auth_headers)
    assert response.status_code == 400
    data = response.get_json()
    assert "Script not found" in data["error"]


def test_run_invalid_args_type_returns_400(client, auth_headers):
    """Sending non-list args or malformed payload must return 400."""
    response = client.post(
        "/run/sipa_cleaner",
        headers=auth_headers,
        json={"args": "not-a-list-string"}
    )
    assert response.status_code == 400
    data = response.get_json()
    assert "Invalid args payload" in data["error"]


def test_run_disallowed_flag_returns_400(client, auth_headers):
    """Passing a flag not in the script allowlist must return 400."""
    response = client.post(
        "/run/sipa_cleaner",
        headers=auth_headers,
        json={"args": ["--malicious-injection-flag"]}
    )
    assert response.status_code == 400
    data = response.get_json()
    assert "Disallowed argument" in data["error"]


def test_run_excessive_arg_length_returns_400(client, auth_headers):
    """Passing excessively long arguments must return 400."""
    response = client.post(
        "/run/sipa_cleaner",
        headers=auth_headers,
        json={"args": ["a" * 200]}
    )
    assert response.status_code == 400
    data = response.get_json()
    assert "Argument exceeds maximum allowed length" in data["error"]


def test_run_happy_path_returns_202(client, auth_headers):
    """Valid script execution must return 202 Accepted with uuid4 job_id and log path."""
    with patch("src.host_runner.threading.Thread") as mock_thread, \
         patch("os.path.exists", return_value=True):
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        response = client.post(
            "/run/sipa_cleaner",
            headers=auth_headers,
            json={"args": ["--dry-run"]}
        )
        assert response.status_code == 202
        data = response.get_json()
        assert data["status"] == "queued"
        assert data["script"] == "sipa_cleaner"
        assert data["log_file"] == "logs/sipa_cleaner.log"
        assert "job_id" in data
        assert len(data["job_id"]) > 10


def test_concurrent_run_guard_returns_409(client, auth_headers):
    """Duplicate/concurrent execution of the same script must return 409 Conflict."""
    with patch("src.host_runner.ACTIVE_PROCESSES", {"sipa_cleaner": {"status": "QUEUED", "pid": 1234}}), \
         patch("os.path.exists", return_value=True):
        response = client.post("/run/sipa_cleaner", headers=auth_headers)
        assert response.status_code == 409
        data = response.get_json()
        assert "already running" in data["error"]


def test_error_500_does_not_leak_local_path(client, auth_headers):
    """Internal errors when file is missing must return generic 500 without leaking local paths."""
    with patch("os.path.exists", return_value=False):
        response = client.post("/run/sipa_cleaner", headers=auth_headers)
        assert response.status_code == 500
        data = response.get_json()
        assert "C:\\" not in data["error"]
        assert "/home/" not in data["error"]
        assert "Script file unavailable" in data["error"]


def test_get_job_not_found_returns_404(client, auth_headers):
    """Querying a non-existent job must return 404."""
    response = client.get("/jobs/non_existent_uuid_job", headers=auth_headers)
    assert response.status_code == 404
    data = response.get_json()
    assert "not found" in data["error"]


def test_job_persistence_lifecycle(client, auth_headers):
    """Job lifecycle must be persisted and retrievable via GET /jobs/<id>."""
    test_job_id = "job_test_persistence_uuid_123"
    started = datetime.now(timezone.utc)
    persist_job_start(test_job_id, "sipa_cleaner", started)

    response = client.get(f"/jobs/{test_job_id}", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["job_id"] == test_job_id
    assert data["status"] == "RUNNING"
    assert data["script_name"] == "sipa_cleaner"

    finished = datetime.now(timezone.utc)
    persist_job_finish(test_job_id, "COMPLETED", 0, finished)

    response_finished = client.get(f"/jobs/{test_job_id}", headers=auth_headers)
    assert response_finished.status_code == 200
    data_finished = response_finished.get_json()
    assert data_finished["status"] == "COMPLETED"
    assert data_finished["exit_code"] == 0
    assert data_finished["finished_at"] is not None


def test_timeout_kills_process_group():
    """Timeout during execution must invoke process termination on process group."""
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="test", timeout=3600), 0]
    expected_sig = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 9))

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("builtins.open", MagicMock()), \
         patch("src.host_runner._rotate_log_file"), \
         patch.object(os, "killpg", create=True) as mock_killpg, \
         patch.object(os, "getpgid", create=True, return_value=9999):
        run_script_async("job_timeout_test", "sipa_cleaner", "dummy_path.py", [])
        mock_killpg.assert_called_once_with(9999, expected_sig)
        assert mock_proc.wait.call_count == 2


def test_timeout_kills_process_fallback_when_no_killpg():
    """When os.killpg is unavailable, fallback to proc.kill()."""
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="test", timeout=3600), 0]

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("builtins.open", MagicMock()), \
         patch("os.path.exists", return_value=True), \
         patch("os.killpg", None, create=True):
        run_script_async("job_timeout_test", "sipa_cleaner", "dummy_path.py", [])
        mock_proc.kill.assert_called_once()
        assert mock_proc.wait.call_count == 2

