#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]
if str(FRAMEWORK_ROOT) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_ROOT))

from delivery.runtime_liveness import wrapper_should_interrupt
from delivery.token_usage import append_token_usage_record, extract_opencode_usage


PARENT_WATCH_INTERVAL_SECONDS = 1.0
MUTATION_TOOL_NAMES = {
    "write",
    "edit",
    "str_replace",
    "insert",
    "delete",
    "rename",
    "create_file",
    "apply_patch",
}
READ_ONLY_TOOL_NAMES = {
    "read",
    "grep",
    "glob",
    "ls",
    "find",
    "view",
}


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _runtime_dir(project_root: Path | str) -> Path:
    return Path(project_root).expanduser().resolve() / ".app-delivery-runtime"


def _task_log_path(project_root: Path | str) -> Path:
    return _runtime_dir(project_root) / "task-log.jsonl"


def _active_tasks_dir(project_root: Path | str) -> Path:
    return _runtime_dir(project_root) / "active-tasks"


def _task_runtime_dir(project_root: Path | str) -> Path:
    return _runtime_dir(project_root) / "task-runtime"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._") or "task"


def _derive_task_identity(task_id: str, task_title: str) -> tuple[str, str]:
    normalized_id = str(task_id or "").strip()
    normalized_title = str(task_title or "").strip()
    if normalized_id:
        return normalized_id, "implementation"
    if normalized_title.startswith("review-"):
        review_target = normalized_title[len("review-") :].strip()
        if review_target:
            return review_target, "review"
    return "", "runtime"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_task_log_event(
    project_root: Path | str,
    *,
    runtime: str,
    level: str,
    message: str,
    task_id: str = "",
    task_title: str = "",
    session_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ts": utc_now_iso(),
        "runtime": runtime,
        "level": level,
        "message": message,
    }
    if task_id:
        payload["task_id"] = task_id
    if task_title:
        payload["task_title"] = task_title
    if session_id:
        payload["session_id"] = session_id
    if extra:
        payload.update(extra)
    _append_jsonl(_task_log_path(project_root), payload)


def bootstrap_log_file(
    log_file: str | None,
    *,
    runtime: str,
    session_id: str,
    task_id: str,
    task_title: str,
    command: list[str],
) -> Path | None:
    if not log_file:
        return None
    path = Path(log_file)
    _append_jsonl(
        path,
        {
            "type": "app-delivery-wrapper-start",
            "ts": utc_now_iso(),
            "runtime": runtime,
            "session_id": session_id or None,
            "task_id": task_id or None,
            "task_title": task_title or None,
            "command": command,
        },
    )
    return path


def active_task_record_path(project_root: Path | str, *, runtime: str, session_id: str, task_id: str) -> Path:
    record_name = "-".join(part for part in [_slug(runtime), _slug(task_id), _slug(session_id or str(os.getpid()))] if part)
    return _active_tasks_dir(project_root) / f"{record_name}.json"


def task_runtime_state_path(project_root: Path | str, task_id: str) -> Path:
    return _task_runtime_dir(project_root) / f"{_slug(task_id)}.json"


def write_active_task_record(path: Path, payload: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    body = {**existing, **dict(payload)}
    body["updated_at"] = utc_now_iso()
    body.setdefault("created_at", body["updated_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_task_runtime_state(path: Path, payload: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    body = {**existing, **dict(payload)}
    body["updated_at"] = utc_now_iso()
    body.setdefault("created_at", body["updated_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def remove_active_task_record(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _emit_runtime_heartbeat(
    *,
    project_root: Path,
    runtime: str,
    phase: str,
    task_id: str,
    task_title: str,
    session_id: str,
    elapsed_seconds: int,
) -> None:
    label = task_id or task_title or phase
    message = f"[app-delivery] {phase} {label}: still running ({elapsed_seconds}s elapsed)"
    print(message, file=sys.stderr, flush=True)
    append_task_log_event(
        project_root,
        runtime=runtime,
        level="INFO",
        message=f"Heartbeat {phase} {label}",
        task_id=task_id,
        task_title=task_title,
        session_id=session_id,
        extra={"phase": phase, "elapsed_seconds": elapsed_seconds},
    )


def _tool_activity_kind(tool_name: str) -> str:
    normalized = str(tool_name or "").strip()
    if normalized in MUTATION_TOOL_NAMES:
        return "mutation"
    if normalized in READ_ONLY_TOOL_NAMES:
        return "read_only"
    return "other"


def run_observed_process(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    project_root: Path,
    runtime: str,
    session_id: str,
    task_id: str,
    task_title: str,
    log_file: str | None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_task_id, phase = _derive_task_identity(task_id, task_title)
    observed_session_id = session_id
    parent_pid = os.getppid()
    log_path = bootstrap_log_file(
        log_file,
        runtime=runtime,
        session_id=observed_session_id,
        task_id=effective_task_id,
        task_title=task_title,
        command=command,
    )
    record_path: Path | None = None
    if effective_task_id:
        runtime_state_path = task_runtime_state_path(project_root, effective_task_id)
        append_task_log_event(
            project_root,
            runtime=runtime,
            level="INFO",
            message=f"Started {phase} {effective_task_id}",
            task_id=effective_task_id,
            task_title=task_title,
            session_id=session_id,
            extra={"phase": phase},
        )
        record_path = active_task_record_path(project_root, runtime=runtime, session_id=session_id, task_id=effective_task_id)
        write_active_task_record(
            record_path,
            {
                "pid": os.getpid(),
                "runtime": runtime,
                "session_id": session_id or None,
                "task_id": effective_task_id,
                "task_title": task_title or None,
                "phase": phase,
                "project_root": str(project_root),
                "log_file": str(log_path) if log_path else None,
                "status": "running",
            },
        )
        write_task_runtime_state(
            runtime_state_path,
            {
                "task_id": effective_task_id,
                "task_title": task_title or None,
                "runtime": runtime,
                "session_id": session_id or None,
                "phase": phase,
                "status": "running",
                "started_at": utc_now_iso(),
                "completed_at": None,
                "exit_code": None,
                "project_root": str(project_root),
                "log_file": str(log_path) if log_path else None,
            },
        )
    else:
        runtime_state_path = None

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    if input_text is not None and process.stdin is not None:
        process.stdin.write(input_text)
        process.stdin.flush()
        process.stdin.close()
    if record_path is not None:
        write_active_task_record(record_path, {"runtime_pid": process.pid})
    if runtime_state_path is not None:
        write_task_runtime_state(runtime_state_path, {"runtime_pid": process.pid, "wrapper_pid": os.getpid()})

    heartbeat_stop = threading.Event()
    parent_watch_stop = threading.Event()
    activity_lock = threading.Lock()
    activity = {
        "started_at": time.monotonic(),
        "last_output_at": time.monotonic(),
        "last_report_at": 0.0,
        "last_persist_at": 0.0,
        "last_tool_name": "",
        "last_tool_at": "",
        "last_activity_kind": "",
        "last_mutation_at": "",
        "read_only_streak": 0,
    }

    termination_lock = threading.Lock()
    termination_requested = {"value": False}
    liveness_stop = {"value": False, "kind": "", "message": ""}

    def terminate_runtime_process_group() -> None:
        with termination_lock:
            if termination_requested["value"]:
                return
            termination_requested["value"] = True
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except Exception:
                return

    def _activity_snapshot() -> dict[str, Any]:
        with activity_lock:
            return {
                "last_tool_name": activity["last_tool_name"] or None,
                "last_tool_at": activity["last_tool_at"] or None,
                "last_activity_kind": activity["last_activity_kind"] or None,
                "last_mutation_at": activity["last_mutation_at"] or None,
                "read_only_streak": int(activity["read_only_streak"] or 0),
            }

    previous_handlers: dict[int, Any] = {}

    def handle_wrapper_termination(signum: int, frame: Any) -> None:
        terminate_runtime_process_group()
        raise SystemExit(128 + int(signum))

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, handle_wrapper_termination)

    def heartbeat_worker() -> None:
        while not heartbeat_stop.wait(5):
            label_available = bool(effective_task_id or task_title)
            if not label_available:
                continue
            with activity_lock:
                now = time.monotonic()
                if now - activity["last_output_at"] < 30:
                    continue
                if now - activity["last_report_at"] < 30:
                    continue
                activity["last_report_at"] = now
                elapsed_seconds = int(now - activity["started_at"])
            if record_path is not None:
                write_active_task_record(record_path, {"status": "running", "runtime_pid": process.pid, **_activity_snapshot()})
            if runtime_state_path is not None:
                write_task_runtime_state(runtime_state_path, {"status": "running", "runtime_pid": process.pid, **_activity_snapshot()})
            _emit_runtime_heartbeat(
                project_root=project_root,
                runtime=runtime,
                phase=phase,
                task_id=effective_task_id,
                task_title=task_title,
                session_id=session_id,
                elapsed_seconds=elapsed_seconds,
            )
            if effective_task_id and not liveness_stop["value"]:
                decision = wrapper_should_interrupt(
                    project_root,
                    effective_task_id,
                    _activity_snapshot(),
                    started_monotonic=float(activity["started_at"]),
                    now_monotonic=time.monotonic(),
                )
                if decision.suspected:
                    liveness_stop.update({"value": True, "kind": decision.kind, "message": decision.message})
                    message = f"stalled_runtime: {decision.message}"
                    print(f"[app-delivery] {message}", file=sys.stderr, flush=True)
                    if runtime_state_path is not None:
                        write_task_runtime_state(
                            runtime_state_path,
                            {"status": "stalled", "failure_kind": "stalled_runtime", "failure_message": message, **_activity_snapshot()},
                        )
                    append_task_log_event(
                        project_root,
                        runtime=runtime,
                        level="ERROR",
                        message=f"Stalled {phase} {effective_task_id}",
                        task_id=effective_task_id,
                        task_title=task_title,
                        session_id=observed_session_id or session_id,
                        extra={"phase": phase, "failure_kind": "stalled_runtime", "liveness_kind": decision.kind},
                    )
                    terminate_runtime_process_group()

    def parent_watch_worker() -> None:
        while not parent_watch_stop.wait(PARENT_WATCH_INTERVAL_SECONDS):
            if process.poll() is not None:
                return
            if os.getppid() == parent_pid:
                continue
            terminate_runtime_process_group()
            return

    heartbeat_thread = threading.Thread(target=heartbeat_worker, name="app-delivery-heartbeat", daemon=True)
    heartbeat_thread.start()
    parent_watch_thread = threading.Thread(target=parent_watch_worker, name="app-delivery-parent-watch", daemon=True)
    parent_watch_thread.start()

    chunks: list[str] = []
    log_handle = log_path.open("a", encoding="utf-8") if log_path else None
    log_has_newline = True
    try:
        if process.stdout is not None:
            for line in process.stdout:
                chunks.append(line)
                now = time.monotonic()
                with activity_lock:
                    activity["last_output_at"] = now
                stripped = line.strip()
                if stripped:
                    try:
                        payload = json.loads(stripped)
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict):
                        candidate_session_id = str(payload.get("sessionID") or "").strip()
                        if candidate_session_id and candidate_session_id != observed_session_id:
                            observed_session_id = candidate_session_id
                            if record_path is not None:
                                write_active_task_record(record_path, {"session_id": observed_session_id, "status": "running", "runtime_pid": process.pid, **_activity_snapshot()})
                            if runtime_state_path is not None:
                                write_task_runtime_state(runtime_state_path, {"session_id": observed_session_id, "status": "running", "runtime_pid": process.pid, **_activity_snapshot()})
                        if payload.get("type") == "tool_use":
                            part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
                            tool_name = str(part.get("tool") or "").strip()
                            if tool_name:
                                now_iso = utc_now_iso()
                                kind = _tool_activity_kind(tool_name)
                                with activity_lock:
                                    activity["last_tool_name"] = tool_name
                                    activity["last_tool_at"] = now_iso
                                    activity["last_activity_kind"] = kind
                                    if kind == "mutation":
                                        activity["last_mutation_at"] = now_iso
                                        activity["read_only_streak"] = 0
                                    elif kind == "read_only":
                                        activity["read_only_streak"] = int(activity["read_only_streak"] or 0) + 1
                                    else:
                                        activity["read_only_streak"] = 0
                if now - activity["last_persist_at"] >= 5:
                    activity["last_persist_at"] = now
                    if record_path is not None:
                        write_active_task_record(
                            record_path,
                            {"session_id": observed_session_id or None, "status": "running", "runtime_pid": process.pid, **_activity_snapshot()},
                        )
                    if runtime_state_path is not None:
                        write_task_runtime_state(
                            runtime_state_path,
                            {"session_id": observed_session_id or None, "status": "running", "runtime_pid": process.pid, **_activity_snapshot()},
                        )
                if log_handle is not None:
                    log_handle.write(line)
                    log_handle.flush()
                log_has_newline = line.endswith("\n")
        return_code = process.wait()
        if record_path is not None:
            status = "completed" if return_code == 0 else ("stalled" if liveness_stop["value"] else "failed")
            write_active_task_record(record_path, {"status": status, "exit_code": return_code, "runtime_pid": process.pid, **_activity_snapshot()})
            if runtime_state_path is not None:
                write_task_runtime_state(
                    runtime_state_path,
                    {
                        "status": status,
                        "completed_at": utc_now_iso(),
                        "exit_code": return_code,
                        "runtime_pid": process.pid,
                        "session_id": observed_session_id or None,
                        "failure_kind": "stalled_runtime" if liveness_stop["value"] else None,
                        "failure_message": liveness_stop["message"] if liveness_stop["value"] else None,
                        **_activity_snapshot(),
                    },
                )
            append_task_log_event(
                project_root,
                runtime=runtime,
                level="INFO" if return_code == 0 else "ERROR",
                message=f"{status.capitalize()} {phase} {effective_task_id}",
                task_id=effective_task_id,
                task_title=task_title,
                session_id=observed_session_id,
                extra={"exit_code": return_code, "phase": phase},
            )
        if log_handle is not None:
            if not log_has_newline:
                log_handle.write("\n")
            log_handle.write(
                json.dumps(
                    {
                        "type": "app-delivery-wrapper-end",
                        "ts": utc_now_iso(),
                        "runtime": runtime,
                        "session_id": session_id or None,
                        "task_id": effective_task_id or None,
                        "task_title": task_title or None,
                        "phase": phase,
                        "exit_code": return_code,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            log_handle.flush()
        if effective_task_id:
            usage = extract_opencode_usage("".join(chunks))
            if usage is not None:
                append_token_usage_record(
                    project_root,
                    runtime=runtime,
                    task_id=effective_task_id,
                    task_title=task_title,
                    session_id=observed_session_id or session_id,
                    usage=usage,
                    log_file=str(log_path) if log_path else None,
                )
        remove_active_task_record(record_path)
        return subprocess.CompletedProcess(command, return_code, "".join(chunks), None)
    finally:
        heartbeat_stop.set()
        parent_watch_stop.set()
        heartbeat_thread.join(timeout=1)
        parent_watch_thread.join(timeout=1)
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        if log_handle is not None:
            log_handle.close()
        remove_active_task_record(record_path)