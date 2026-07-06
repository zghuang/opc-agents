from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builtin_tasks import FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from .stack_contracts import PYTHON_REACT_CONTRACT
from .state import load_task_runtime_state, normalize_task_runtime_state, process_alive, utc_now_iso
from .task import FINAL_VERIFY_TASK_ID, Task, all_tasks


DEFAULT_ATTENTION_SECONDS = 600
DEFAULT_HARD_STALL_SECONDS = 900
DEFAULT_AUDIT_HARD_STALL_SECONDS = 1500
DEFAULT_LARGE_FRONTEND_HARD_STALL_SECONDS = 1800
DEFAULT_READ_ONLY_STREAK = 20
SOURCE_ROOTS = (PYTHON_REACT_CONTRACT.backend_source_root, PYTHON_REACT_CONTRACT.frontend_source_root, PYTHON_REACT_CONTRACT.mock_server_root)
IGNORED_PARTS = {".app-delivery-runtime", ".git", "node_modules", ".venv", "__pycache__"}
FRESH_ACTIVITY_FIELDS = (
    ("stdout", "seconds_since_output"),
    ("session_store", "seconds_since_session_update"),
    ("tool", "seconds_since_tool"),
    ("mutation", "seconds_since_mutation"),
    ("task_file", "seconds_since_relevant_change"),
)


@dataclass(frozen=True)
class RuntimeLiveness:
    suspected: bool
    kind: str
    message: str
    details: dict[str, Any]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso(value: Any) -> dt.datetime | None:
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


def _seconds_since(value: Any, *, now: dt.datetime | None = None) -> int | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    current = now or _now()
    return int(max(0, (current - parsed).total_seconds()))


def _positive_pids(runtime_state: dict[str, Any]) -> list[int]:
    pids: list[int] = []
    for key in ("runtime_pid", "wrapper_pid"):
        try:
            pid = int(runtime_state.get(key) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            pids.append(pid)
    return pids


def any_runtime_pid_alive(runtime_state: dict[str, Any]) -> bool:
    pids = _positive_pids(runtime_state)
    return bool(pids) and any(process_alive(pid) for pid in pids)


def hard_stall_threshold_for_task(project_root: Path | str, task_id: str) -> int:
    override = os.environ.get("APP_DELIVERY_RUNTIME_HARD_STALL_SECONDS")
    if override:
        return int(override)
    normalized_task_id = str(task_id or "").strip()
    if normalized_task_id in {FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID}:
        return DEFAULT_AUDIT_HARD_STALL_SECONDS
    try:
        task = next((row for row in all_tasks(project_root) if row.id == normalized_task_id), None)
    except Exception:
        task = None
    if task is not None and task.task_kind == "audit":
        return DEFAULT_AUDIT_HARD_STALL_SECONDS
    if task is not None:
        frontend_paths = [path for path in [*task.output_paths, *task.output_tests] if str(path).strip().startswith("frontend/")]
        if len(frontend_paths) >= 12:
            return DEFAULT_LARGE_FRONTEND_HARD_STALL_SECONDS
    return DEFAULT_HARD_STALL_SECONDS


def _path_is_interesting(path: Path) -> bool:
    if not path.exists():
        return False
    if any(part in IGNORED_PARTS for part in path.parts):
        return False
    if path.is_dir():
        return True
    return path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".yaml", ".yml", ".md"}


def _candidate_paths(project_root: Path, task: Task | None) -> list[Path]:
    raw_paths: list[str] = []
    if task is not None:
        raw_paths.extend(task.output_paths)
        raw_paths.extend(task.output_tests)
    if not raw_paths:
        raw_paths.extend(SOURCE_ROOTS)
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        text = str(raw or "").strip()
        if not text or any(token in text for token in ("&&", "||", "|", ";", "$(", "`")):
            continue
        if text.startswith(("npm ", "pnpm ", "yarn ", "pytest ", "python ", "python3 ", "uv ", "curl ", "docker ")):
            continue
        path = (project_root / text).resolve()
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def latest_relevant_mtime(project_root: Path | str, task: Task | None) -> float | None:
    root = Path(project_root).expanduser().resolve()
    latest: float | None = None
    for path in _candidate_paths(root, task):
        if not _path_is_interesting(path):
            continue
        if path.is_file():
            try:
                value = path.stat().st_mtime
            except OSError:
                continue
            latest = value if latest is None else max(latest, value)
            continue
        if path.is_dir():
            for child in path.rglob("*"):
                if not child.is_file() or not _path_is_interesting(child):
                    continue
                try:
                    value = child.stat().st_mtime
                except OSError:
                    continue
                latest = value if latest is None else max(latest, value)
    return latest


def seconds_since_relevant_change(project_root: Path | str, task: Task | None, *, now: dt.datetime | None = None) -> int | None:
    latest = latest_relevant_mtime(project_root, task)
    if latest is None:
        return None
    current = now or _now()
    return int(max(0, current.timestamp() - latest))


def _runtime_activity_details(runtime_state: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    return {
        "elapsed_seconds": _seconds_since(runtime_state.get("started_at"), now=now),
        "seconds_since_output": _seconds_since(runtime_state.get("last_output_at"), now=now),
        "seconds_since_session_update": _seconds_since(runtime_state.get("last_session_update_at"), now=now),
        "seconds_since_tool": _seconds_since(runtime_state.get("last_tool_at"), now=now),
        "seconds_since_mutation": _seconds_since(runtime_state.get("last_mutation_at"), now=now),
        "last_tool_name": str(runtime_state.get("last_tool_name") or "").strip() or None,
        "last_activity_kind": str(runtime_state.get("last_activity_kind") or "").strip() or None,
        "read_only_streak": int(runtime_state.get("read_only_streak") or 0),
        "session_id": str(runtime_state.get("session_id") or "").strip() or None,
        "runtime_pid": runtime_state.get("runtime_pid"),
        "wrapper_pid": runtime_state.get("wrapper_pid"),
    }


def _fresh_activity(details: dict[str, Any], attention_seconds: int) -> list[str]:
    fresh: list[str] = []
    for name, field in FRESH_ACTIVITY_FIELDS:
        value = details.get(field)
        if value is not None and int(value) < attention_seconds:
            fresh.append(name)
    return fresh


def classify_running_runtime(
    project_root: Path | str,
    task: Task | None,
    runtime_state: dict[str, Any],
    *,
    active_record_present: bool = False,
    attention_seconds: int = DEFAULT_ATTENTION_SECONDS,
    hard_stall_seconds: int = DEFAULT_HARD_STALL_SECONDS,
    now: dt.datetime | None = None,
) -> RuntimeLiveness:
    current = now or _now()
    details = _runtime_activity_details(runtime_state, now=current)
    details["active_record_present"] = active_record_present
    relevant_change_age = seconds_since_relevant_change(project_root, task, now=current)
    details["seconds_since_relevant_change"] = relevant_change_age

    if str(runtime_state.get("status") or "").strip() != "running":
        return RuntimeLiveness(False, "not_running", "runtime is not marked running", details)

    pids = _positive_pids(runtime_state)
    if pids and not any(process_alive(pid) for pid in pids):
        return RuntimeLiveness(True, "orphaned_runtime", "Runtime state is running but recorded runtime processes are no longer alive.", details)

    elapsed = details.get("elapsed_seconds")
    if elapsed is None or elapsed < attention_seconds:
        return RuntimeLiveness(False, "running", "runtime has not exceeded the attention threshold", details)

    read_only_streak = int(details.get("read_only_streak") or 0)

    fresh = _fresh_activity(details, attention_seconds)
    details["fresh_activity"] = fresh
    if fresh:
        return RuntimeLiveness(False, "running", "runtime still has recent activity evidence: " + ", ".join(fresh), details)

    if read_only_streak >= DEFAULT_READ_ONLY_STREAK:
        return RuntimeLiveness(True, "read_only_stall", "Runtime has a long read-only tool streak without recent mutation or relevant file changes.", details)

    if elapsed >= hard_stall_seconds:
        return RuntimeLiveness(True, "silent_stall", "Runtime has exceeded the hard stall threshold without recent tool, mutation, or relevant file progress.", details)

    return RuntimeLiveness(False, "watch", "runtime is stale but below the hard stall threshold", details)


def running_runtime_task_payload(project_root: Path | str, tasks: list[Task]) -> dict[str, Any] | None:
    candidates: list[tuple[str, Task, dict[str, Any]]] = []
    for task in tasks:
        if task.id == FINAL_VERIFY_TASK_ID:
            continue
        if task.status in {"verified", "cancelled", "exception"}:
            continue
        state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
        if str(state.get("status") or "").strip() != "running":
            continue
        updated_at = str(state.get("updated_at") or "")
        candidates.append((updated_at, task, state))
    if not candidates:
        return None
    _, task, state = sorted(candidates, key=lambda row: row[0], reverse=True)[0]
    payload = task.to_dict()
    payload["phase"] = state.get("phase") or "implementation"
    payload["runtime_pid"] = state.get("runtime_pid")
    payload["updated_at"] = state.get("updated_at")
    payload["log_file"] = state.get("log_file")
    payload["session_id"] = state.get("session_id")
    payload["runtime_state"] = state
    return payload


def wrapper_should_interrupt(
    project_root: Path | str,
    task_id: str,
    activity: dict[str, Any],
    *,
    started_monotonic: float,
    now_monotonic: float,
    threshold_seconds: int | None = None,
    attention_seconds: int = DEFAULT_ATTENTION_SECONDS,
) -> RuntimeLiveness:
    threshold = int(threshold_seconds) if threshold_seconds is not None else hard_stall_threshold_for_task(project_root, task_id)
    elapsed = int(max(0, now_monotonic - started_monotonic))
    if elapsed < threshold:
        return RuntimeLiveness(False, "running", "runtime is below the hard stall threshold", {"elapsed_seconds": elapsed})
    last_tool_at = activity.get("last_tool_at")
    last_output_at = activity.get("last_output_at")
    last_session_update_at = activity.get("last_session_update_at")
    last_mutation_at = activity.get("last_mutation_at")
    details = {
        "elapsed_seconds": elapsed,
        "last_output_at": last_output_at or None,
        "last_session_update_at": last_session_update_at or None,
        "last_tool_at": last_tool_at or None,
        "last_mutation_at": last_mutation_at or None,
        "last_tool_name": activity.get("last_tool_name") or None,
        "last_activity_kind": activity.get("last_activity_kind") or None,
        "read_only_streak": int(activity.get("read_only_streak") or 0),
    }
    current = _now()
    seconds_since_output = _seconds_since(last_output_at, now=current)
    seconds_since_session_update = _seconds_since(last_session_update_at, now=current)
    seconds_since_tool = _seconds_since(last_tool_at, now=current)
    seconds_since_mutation = _seconds_since(last_mutation_at, now=current)
    try:
        task = next((row for row in all_tasks(project_root) if row.id == str(task_id or "").strip()), None)
    except Exception:
        task = None
    seconds_since_relevant_change_value = seconds_since_relevant_change(project_root, task, now=current)
    details["seconds_since_output"] = seconds_since_output
    details["seconds_since_session_update"] = seconds_since_session_update
    details["seconds_since_tool"] = seconds_since_tool
    details["seconds_since_mutation"] = seconds_since_mutation
    details["seconds_since_relevant_change"] = seconds_since_relevant_change_value
    fresh = _fresh_activity(details, attention_seconds)
    details["fresh_activity"] = fresh
    if fresh:
        return RuntimeLiveness(False, "running", "runtime has recent activity evidence: " + ", ".join(fresh), details)
    if int(activity.get("read_only_streak") or 0) >= DEFAULT_READ_ONLY_STREAK:
        return RuntimeLiveness(True, "read_only_stall", "runtime liveness stalled: long read-only streak without progress", details)
    return RuntimeLiveness(True, "silent_stall", "runtime liveness stalled: no recent tool, mutation, or output progress", details)


def runtime_attention_payload(project_root: Path | str, task: Task | None, runtime_state: dict[str, Any], *, active_record_present: bool = False) -> dict[str, Any] | None:
    decision = classify_running_runtime(project_root, task, runtime_state, active_record_present=active_record_present)
    if not decision.suspected:
        return None
    return {
        "suspected": True,
        "kind": decision.kind,
        "message": decision.message,
        **decision.details,
        "evidence_source": "runtime_liveness",
        "checked_at": utc_now_iso(),
    }
