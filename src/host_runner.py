"""
Host execution daemon for BeatMatch AI Automation Hub.
Coordinates out-of-container background jobs managed by systemd, preventing memory leaks
and providing deterministic job lifecycle tracking for n8n orchestrator webhooks.
"""

from datetime import datetime, timezone
import logging
import os
import subprocess
import sys
import threading
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from src.models.schemas import HostRunnerJob

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(PROJECT_DIR, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [host_runner] - %(message)s"
)
logger = logging.getLogger("host_runner")

app = Flask(__name__)

API_TOKEN = os.getenv("HOST_RUNNER_TOKEN", "")
if not API_TOKEN:
    logger.warning("HOST_RUNNER_TOKEN unconfigured in environment. Defaulting to development token.")
    API_TOKEN = "UNCONFIGURED_TOKEN_CHANGE_ME"

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

_active_lock = threading.Lock()
ACTIVE_PROCESSES: dict[str, dict[str, Any]] = {}


def _cleanup_dead_processes() -> None:
    with _active_lock:
        to_delete = []
        for script_name, info in ACTIVE_PROCESSES.items():
            proc = info["process"]
            ret = proc.poll()
            if ret is not None:
                to_delete.append(script_name)
        for s in to_delete:
            del ACTIVE_PROCESSES[s]


def run_script_async(script_name: str, script_path: str, args: list[str] | None = None) -> None:
    cmd = [PYTHON_BIN, script_path]
    if args:
        cmd.extend(args)

    log_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, f"{script_name}.log")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        with open(log_file_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- Execution triggered at {timestamp} ---\n")
            log_file.flush()

            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                cwd=PROJECT_DIR
            )

            with _active_lock:
                ACTIVE_PROCESSES[script_name] = {
                    "process": proc,
                    "started_at": timestamp,
                    "pid": proc.pid
                }

            logger.info("Spawned background process '%s' (PID %d)", script_name, proc.pid)
            proc.wait(timeout=3600)

    except subprocess.TimeoutExpired:
        logger.error("Process '%s' exceeded timeout of 3600s. Terminating forcefully.", script_name)
        proc.kill()
    except Exception as exc:
        logger.error("Error executing script '%s': %s", script_name, exc)
    finally:
        with _active_lock:
            if script_name in ACTIVE_PROCESSES:
                del ACTIVE_PROCESSES[script_name]


@app.before_request
def verify_token():
    if request.path == "/status":
        return None

    token = request.headers.get("Authorization")
    if not token or token != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401


@app.route("/status", methods=["GET"])
def status():
    _cleanup_dead_processes()
    with _active_lock:
        active_jobs = {
            k: {"pid": v["pid"], "started_at": v["started_at"]}
            for k, v in ACTIVE_PROCESSES.items()
        }
    return jsonify({
        "status": "running",
        "project_dir": PROJECT_DIR,
        "python_bin": PYTHON_BIN,
        "active_jobs_count": len(active_jobs),
        "active_jobs": active_jobs
    }), 200


@app.route("/run/<script_name>", methods=["POST"])
def run_script(script_name: str):
    if script_name not in SCRIPTS:
        return jsonify({
            "error": "Script not found",
            "available_scripts": list(SCRIPTS.keys())
        }), 400

    script_path = SCRIPTS[script_name]
    if not os.path.exists(script_path):
        return jsonify({
            "error": f"Script file does not exist locally at {script_path}"
        }), 500

    _cleanup_dead_processes()
    with _active_lock:
        if script_name in ACTIVE_PROCESSES:
            return jsonify({
                "error": f"Job '{script_name}' is already running with PID {ACTIVE_PROCESSES[script_name]['pid']}"
            }), 409

    req_data = request.get_json(silent=True) or {}
    args = req_data.get("args", [])

    job = HostRunnerJob(
        job_id=f"job_{script_name}_{int(datetime.now(timezone.utc).timestamp())}",
        task_name=script_name,
        status="RUNNING"
    )

    worker_thread = threading.Thread(
        target=run_script_async,
        args=(script_name, script_path, args),
        daemon=True
    )
    worker_thread.start()

    return jsonify({
        "status": "queued",
        "job_id": job.job_id,
        "script": script_name,
        "log_file": f"logs/{script_name}.log"
    }), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
