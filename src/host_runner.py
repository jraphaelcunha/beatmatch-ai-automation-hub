"""
Host execution daemon for BeatMatch AI Automation Hub.
Coordinates out-of-container background jobs managed by systemd, preventing memory leaks
and providing deterministic job lifecycle tracking for n8n orchestrator webhooks.
"""

import hmac
import logging
import os
import signal
import subprocess  # nosec B404  # required for isolated process lifecycle management
import sys
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(PROJECT_DIR, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [host_runner] - %(message)s"
)
logger = logging.getLogger("host_runner")


def get_api_token() -> str:
    """Retrieves and validates API authentication token with fail-closed semantics."""
    token = os.getenv("HOST_RUNNER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CRITICAL: HOST_RUNNER_TOKEN must be configured. Exiting.")
    return token


# Fail closed immediately upon import/startup
API_TOKEN = get_api_token()

HOST_RUNNER_HOST = os.getenv("HOST_RUNNER_HOST", "127.0.0.1")
HOST_RUNNER_PORT = int(os.getenv("HOST_RUNNER_PORT", "5000"))

PYTHON_BIN = os.path.join(PROJECT_DIR, "venv", "bin", "python")
if not os.path.exists(PYTHON_BIN):
    PYTHON_BIN = sys.executable

SCRIPTS = {
    "youtube_scraper": os.path.join(PROJECT_DIR, "src", "scrapers", "youtube_scraper.py"),
    "apify_twitter": os.path.join(PROJECT_DIR, "src", "scrapers", "apify_twitter.py"),
    "spotify_miner": os.path.join(PROJECT_DIR, "src", "scrapers", "spotify_miner.py"),
    "spotify_resolver": os.path.join(PROJECT_DIR, "src", "enrichers", "spotify_resolver.py"),
    "instagram_finder": os.path.join(PROJECT_DIR, "src", "enrichers", "instagram_finder.py"),
    "sipa_cleaner": os.path.join(PROJECT_DIR, "src", "quality", "sipa_cleaner.py"),
    "export_to_monday": os.path.join(PROJECT_DIR, "src", "utils", "export_to_monday.py"),
    "reconciler": os.path.join(PROJECT_DIR, "src", "reconciler.py"),
}

ALLOWED_SCRIPT_FLAGS: dict[str, set[str]] = {
    "youtube_scraper": {"--limit", "--query", "--genre", "--headless"},
    "apify_twitter": {"--limit", "--query", "--hashtag"},
    "spotify_miner": {"--limit", "--genre", "--market"},
    "spotify_resolver": {"--batch-size", "--retry"},
    "instagram_finder": {"--batch-size", "--concurrency"},
    "sipa_cleaner": {"--dry-run", "--threshold"},
    "export_to_monday": {"--board-id", "--batch-size"},
    "reconciler": {"--dry-run", "--limit"},
}

MAX_LOG_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB log rotation limit
MAX_BACKUP_LOGS = 3

_active_lock = threading.Lock()
ACTIVE_PROCESSES: dict[str, dict[str, Any]] = {}

# Fallback/in-memory store for unit testing and offline execution
_job_store_lock = threading.Lock()
_IN_MEMORY_JOB_STORE: dict[str, dict[str, Any]] = {}

app = Flask(__name__)


class RunScriptPayload(BaseModel):
    """Pydantic model validating external run requests."""
    model_config = ConfigDict(strict=True, extra="forbid")
    args: list[str] = Field(default_factory=list, max_length=20)


def _validate_script_args(script_name: str, args: list[str]) -> str | None:
    """Validates command-line arguments against script allowlists and length limits."""
    allowed_flags = ALLOWED_SCRIPT_FLAGS.get(script_name, set())
    for arg in args:
        if len(arg) > 100:
            return "Argument exceeds maximum allowed length of 100 characters."
        if arg.startswith("-") and arg not in allowed_flags:
            return f"Disallowed argument '{arg}' for script '{script_name}'."
    return None


def _rotate_log_file(log_file_path: str) -> None:
    """Rotates log file if it exceeds MAX_LOG_SIZE_BYTES."""
    try:
        if os.path.exists(log_file_path) and os.path.getsize(log_file_path) >= MAX_LOG_SIZE_BYTES:
            for i in range(MAX_BACKUP_LOGS - 1, 0, -1):
                sfn = f"{log_file_path}.{i}"
                dfn = f"{log_file_path}.{i + 1}"
                if os.path.exists(sfn):
                    os.replace(sfn, dfn)
            os.replace(log_file_path, f"{log_file_path}.1")
    except OSError as err:
        logger.warning("Failed log rotation for %s: %s", log_file_path, err)


def persist_job_start(job_id: str, script_name: str, started_at: datetime) -> None:
    """Records job initiation in Postgres if available, with in-memory persistence."""
    record = {
        "job_id": job_id,
        "script_name": script_name,
        "status": "RUNNING",
        "exit_code": None,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "log_file": f"logs/{script_name}.log"
    }
    with _job_store_lock:
        _IN_MEMORY_JOB_STORE[job_id] = record

    if os.getenv("DATABASE_URL"):
        try:
            from src.utils.db import execute_query
            query = """
                INSERT INTO host_runner_jobs (job_id, script_name, status, started_at, log_file)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET status = EXCLUDED.status;
            """
            execute_query(query, (job_id, script_name, "RUNNING", started_at, f"logs/{script_name}.log"))
        except Exception:
            logger.exception("Failed to persist job start to PostgreSQL for job %s", job_id)


def persist_job_finish(job_id: str, status: str, exit_code: int | None, finished_at: datetime) -> None:
    """Updates job completion state in Postgres if available, with in-memory persistence."""
    with _job_store_lock:
        if job_id in _IN_MEMORY_JOB_STORE:
            _IN_MEMORY_JOB_STORE[job_id]["status"] = status
            _IN_MEMORY_JOB_STORE[job_id]["exit_code"] = exit_code
            _IN_MEMORY_JOB_STORE[job_id]["finished_at"] = finished_at.isoformat()

    if os.getenv("DATABASE_URL"):
        try:
            from src.utils.db import execute_query
            query = """
                UPDATE host_runner_jobs
                SET status = %s, exit_code = %s, finished_at = %s
                WHERE job_id = %s;
            """
            execute_query(query, (status, exit_code, finished_at, job_id))
        except Exception:
            logger.exception("Failed to persist job finish to PostgreSQL for job %s", job_id)


def get_job_state(job_id: str) -> dict[str, Any] | None:
    """Retrieves job state by ID from PostgreSQL or local store."""
    if os.getenv("DATABASE_URL"):
        try:
            from src.utils.db import execute_query
            query = "SELECT job_id, script_name, status, exit_code, started_at, finished_at, log_file FROM host_runner_jobs WHERE job_id = %s;"
            results = execute_query(query, (job_id,), fetch=True)
            if results and len(results) > 0:
                row = dict(results[0])
                if isinstance(row.get("started_at"), datetime):
                    row["started_at"] = row["started_at"].isoformat()
                if isinstance(row.get("finished_at"), datetime):
                    row["finished_at"] = row["finished_at"].isoformat()
                return row
        except Exception:
            logger.exception("Failed to query job state from PostgreSQL for job %s", job_id)

    with _job_store_lock:
        return _IN_MEMORY_JOB_STORE.get(job_id)


def _cleanup_dead_processes() -> None:
    """Cleans up completed processes from ACTIVE_PROCESSES dictionary."""
    with _active_lock:
        to_delete = []
        for script_name, info in ACTIVE_PROCESSES.items():
            proc = info.get("process")
            if proc is not None:
                ret = proc.poll()
                if ret is not None:
                    to_delete.append(script_name)
        for s in to_delete:
            del ACTIVE_PROCESSES[s]


def run_script_async(job_id: str, script_name: str, script_path: str, args: list[str]) -> None:
    """Executes target script in isolated process group with log rotation and timeout tracking."""
    cmd = [PYTHON_BIN, script_path]
    if args:
        cmd.extend(args)

    log_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, f"{script_name}.log")
    _rotate_log_file(log_file_path)

    started_at = datetime.now(UTC)
    persist_job_start(job_id, script_name, started_at)
    exit_code = None
    proc = None

    try:
        with open(log_file_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- Execution [{job_id}] triggered at {started_at.isoformat()} ---\n")
            log_file.flush()

            popen_kwargs: dict[str, Any] = {
                "stdout": log_file,
                "stderr": log_file,
                "cwd": PROJECT_DIR,
            }
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **popen_kwargs)  # nosec B603  # cmd strictly validated with allowlist and shell=False

            with _active_lock:
                if script_name in ACTIVE_PROCESSES:
                    ACTIVE_PROCESSES[script_name]["process"] = proc
                    ACTIVE_PROCESSES[script_name]["pid"] = proc.pid
                    ACTIVE_PROCESSES[script_name]["status"] = "RUNNING"

            logger.info("Spawned process '%s' [PID %d, Job %s]", script_name, proc.pid, job_id)
            exit_code = proc.wait(timeout=3600)

    except subprocess.TimeoutExpired:
        logger.error("Process '%s' (Job %s) exceeded timeout. Killing process group.", script_name, job_id)
        if proc:
            if getattr(os, "killpg", None) is not None:
                try:
                    pgid = os.getpgid(proc.pid) if hasattr(os, "getpgid") else proc.pid
                    sig = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 9))
                    os.killpg(pgid, sig)
                except (ProcessLookupError, OSError):
                    pass
            else:
                proc.kill()
            proc.wait()
        exit_code = -9
    except Exception:
        logger.exception("Unexpected execution failure in script '%s' (Job %s)", script_name, job_id)
        exit_code = -1
    finally:
        with _active_lock:
            ACTIVE_PROCESSES.pop(script_name, None)

        finished_at = datetime.now(UTC)
        final_status = "COMPLETED" if exit_code == 0 else "FAILED"
        persist_job_finish(job_id, final_status, exit_code, finished_at)


@app.before_request
def verify_token():
    """Validates authorization token for all protected endpoints using constant-time comparison."""
    if request.path == "/status":
        return None

    token = request.headers.get("Authorization", "")
    expected = f"Bearer {API_TOKEN}"
    if not hmac.compare_digest(token, expected):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/status", methods=["GET"])
def status():
    """Unauthenticated healthcheck returning bare operational status without leaking system paths."""
    return jsonify({"status": "ok"}), 200


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    """Retrieves persisted execution state of a specific job."""
    job_data = get_job_state(job_id)
    if not job_data:
        return jsonify({"error": f"Job '{job_id}' not found"}), 404
    return jsonify(job_data), 200


@app.route("/run/<script_name>", methods=["POST"])
def run_script(script_name: str):
    """Schedules allowlisted script execution with validated arguments and concurrency locks."""
    if script_name not in SCRIPTS:
        return jsonify({
            "error": "Script not found",
            "available_scripts": list(SCRIPTS.keys())
        }), 400

    script_path = SCRIPTS[script_name]
    if not os.path.exists(script_path):
        return jsonify({
            "error": "Script file unavailable"
        }), 500

    # Parse and validate request JSON with Pydantic
    raw_json = request.get_json(silent=True)
    if raw_json is None:
        raw_json = {}
    elif not isinstance(raw_json, dict):
        return jsonify({"error": "Invalid args payload: body must be a JSON object"}), 400

    try:
        payload = RunScriptPayload.model_validate(raw_json)
    except ValidationError as val_err:
        return jsonify({"error": f"Invalid args payload: {val_err}"}), 400

    # Validate individual arguments against allowlist and length constraints
    validation_err = _validate_script_args(script_name, payload.args)
    if validation_err:
        return jsonify({"error": validation_err}), 400

    _cleanup_dead_processes()

    # Reserve the job slot inside mutex lock to eliminate race conditions
    job_id = f"job_{uuid.uuid4().hex}"
    with _active_lock:
        if script_name in ACTIVE_PROCESSES:
            active_info = ACTIVE_PROCESSES[script_name]
            pid = active_info.get("pid", "pending")
            return jsonify({
                "error": f"Job '{script_name}' is already running with PID {pid}"
            }), 409

        ACTIVE_PROCESSES[script_name] = {
            "job_id": job_id,
            "status": "QUEUED",
            "started_at": datetime.now(UTC).isoformat(),
            "pid": None,
            "process": None,
        }

    worker_thread = threading.Thread(
        target=run_script_async,
        args=(job_id, script_name, script_path, payload.args),
        daemon=True
    )
    worker_thread.start()

    return jsonify({
        "status": "queued",
        "job_id": job_id,
        "script": script_name,
        "log_file": f"logs/{script_name}.log"
    }), 202


if __name__ == "__main__":
    app.run(host=HOST_RUNNER_HOST, port=HOST_RUNNER_PORT, debug=False)
