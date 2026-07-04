from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

from .loop_reporting import status
from .review_artifacts import code_review_request_path, final_review_request_path
from .runtime_config import load_project_metadata, resolve_project_root
from .state import ensure_runtime_dirs, load_json, load_task_runtime_state, utc_now_iso, write_json


WATCHDOG_STATE_FILE = Path(".app-delivery-runtime") / "watchdog-state.json"
HOST_HANDOFF_FILE = Path(".app-delivery-runtime") / "host-handoff.json"


def _framework_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _framework_signature() -> str:
    paths = [Path(__file__).resolve(), Path(__file__).with_name("__main__.py").resolve(), Path(__file__).with_name("control_plane.py").resolve()]
    parts: list[str] = []
    for path in paths:
        try:
            parts.append(f"{path.name}:{path.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{path.name}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def watchdog_state_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    ensure_runtime_dirs(project_dir)
    return project_dir / WATCHDOG_STATE_FILE


def load_watchdog_state(project_root: Path | str) -> dict[str, Any]:
    return load_json(watchdog_state_path(project_root), {})


def save_watchdog_state(project_root: Path | str, payload: dict[str, Any]) -> None:
    path = watchdog_state_path(project_root)
    existing = load_json(path, {})
    body = dict(payload)
    if isinstance(existing, dict):
        for key in ("started_at", "stdout_log", "stderr_log"):
            if key not in body and existing.get(key):
                body[key] = existing[key]
    body["framework_root"] = str(_framework_root())
    body["framework_signature"] = _framework_signature()
    body["updated_at"] = utc_now_iso()
    write_json(path, body)


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
    payload = load_watchdog_state(project_root)
    if not isinstance(payload, dict):
        return False
    return process_alive(watchdog_pid(project_root)) and str(payload.get("framework_signature") or "") == _framework_signature()


def _watchdog_process_command(pid: int) -> str:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-ww", "-o", "command="], capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip()


def _process_looks_like_project_watchdog(pid: int, project_root: Path) -> bool:
    command = _watchdog_process_command(pid)
    return "watchdog-run" in command and str(project_root) in command


def _terminate_stale_watchdog(project_root: Path, pid: int) -> None:
    if pid <= 0 or not process_alive(pid):
        return
    if not _process_looks_like_project_watchdog(pid, project_root):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def _watchdog_log_paths(project_root: Path) -> tuple[Path, Path]:
    logs_dir = project_root / ".app-delivery-runtime" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    return logs_dir / f"watchdog-{stamp}.out.log", logs_dir / f"watchdog-{stamp}.err.log"


def watchdog_enabled(project_root: Path | str) -> bool:
    metadata = load_project_metadata(project_root)
    value = metadata.get("watchdog_enabled")
    return bool(value is True)


def _waiting_host_input_ready(project_root: Path | str) -> bool:
    project_dir = resolve_project_root(project_root)
    payload = load_json(project_dir / HOST_HANDOFF_FILE, {})
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "").strip() != "waiting_for_host":
        return False
    expected_input = str(payload.get("expected_input_path") or "").strip()
    if not expected_input:
        return False
    input_path = Path(expected_input).expanduser()
    if not input_path.exists():
        return False

    skill = str(payload.get("skill") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if skill == "code-review" and task_id:
        request_path = code_review_request_path(project_dir, task_id)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns:
            runtime_state = load_task_runtime_state(project_dir, task_id)
            completed_at = _parse_iso_datetime(runtime_state.get("completed_at"))
            if completed_at is None:
                return False
            try:
                input_mtime = dt.datetime.fromtimestamp(input_path.stat().st_mtime, tz=dt.timezone.utc)
            except OSError:
                return False
            return input_mtime > completed_at
    if skill == "final-review":
        request_path = final_review_request_path(project_dir)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns:
            return False
    return True


def _parse_iso_datetime(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


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
    if counts.get("review_pending", 0) and not _waiting_host_input_ready(project_root):
        return False
    active_task = payload.get("active_task") if isinstance(payload.get("active_task"), dict) else None
    if active_task is not None:
        runtime_attention = payload.get("runtime_attention") if isinstance(payload.get("runtime_attention"), dict) else None
        if runtime_attention and runtime_attention.get("suspected"):
            return True
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
    previous_pid = watchdog_pid(project_dir)
    _terminate_stale_watchdog(project_dir, previous_pid)
    command = [
        sys.executable,
        "-m",
        "delivery",
        "watchdog-run",
        "--project",
        str(project_dir),
    ]
    stdout_path, stderr_path = _watchdog_log_paths(project_dir)
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=_framework_root(),
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    save_watchdog_state(
        project_dir,
        {
            "pid": process.pid,
            "project": str(project_dir),
            "status": "running",
            "started_at": utc_now_iso(),
            "last_action": "spawned",
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        },
    )
    return process.pid
