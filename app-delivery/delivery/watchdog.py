from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .loop_reporting import status
from .runtime_config import load_project_metadata, resolve_project_root
from .state import ensure_runtime_dirs, load_json, utc_now_iso, write_json


WATCHDOG_STATE_FILE = Path(".app-delivery-runtime") / "watchdog-state.json"


def watchdog_state_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    ensure_runtime_dirs(project_dir)
    return project_dir / WATCHDOG_STATE_FILE


def load_watchdog_state(project_root: Path | str) -> dict[str, Any]:
    return load_json(watchdog_state_path(project_root), {})


def save_watchdog_state(project_root: Path | str, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body["updated_at"] = utc_now_iso()
    write_json(watchdog_state_path(project_root), body)


def watchdog_pid(project_root: Path | str) -> int:
    payload = load_watchdog_state(project_root)
    try:
        return int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def watchdog_running(project_root: Path | str) -> bool:
    return process_alive(watchdog_pid(project_root))


def watchdog_enabled(project_root: Path | str) -> bool:
    metadata = load_project_metadata(project_root)
    value = metadata.get("watchdog_enabled")
    return bool(value is True)


def should_watchdog_resume(project_root: Path | str) -> bool:
    payload = status(project_root)
    if payload.get("paused"):
        return False
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    if not counts:
        return False
    if payload.get("final_verify_ready"):
        return False
    if counts.get("blocked", 0):
        return False
    if counts.get("review_pending", 0):
        return False
    active_task = payload.get("active_task") if isinstance(payload.get("active_task"), dict) else None
    if active_task is not None:
        return False
    next_task = payload.get("next_task") if isinstance(payload.get("next_task"), dict) else None
    if next_task is not None:
        return True
    stale = payload.get("stale_active_tasks") if isinstance(payload.get("stale_active_tasks"), list) else []
    if stale and counts.get("pending", 0):
        return True
    return False


def spawn_watchdog(project_root: Path | str) -> int:
    project_dir = resolve_project_root(project_root)
    if not watchdog_enabled(project_dir):
        return 0
    if watchdog_running(project_dir):
        return watchdog_pid(project_dir)
    command = [
        sys.executable,
        "-m",
        "delivery",
        "watchdog-run",
        "--project",
        str(project_dir),
    ]
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    save_watchdog_state(
        project_dir,
        {
            "pid": process.pid,
            "project": str(project_dir),
            "status": "running",
            "started_at": utc_now_iso(),
            "last_action": "spawned",
        },
    )
    return process.pid
