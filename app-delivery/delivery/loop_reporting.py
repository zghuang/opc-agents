from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .gates import refresh_gates
from .loop_gitops import is_blocking_verified_task_issue, repair_invalid_verified_tasks
from .runtime_liveness import running_runtime_task_payload, runtime_attention_payload
from .runtime_config import resolve_project_root
from .stack_contracts import PYTHON_REACT_CONTRACT, backend_test_root
from .session import current_session
from .state import latest_task_log_event, lock_file_is_locked, load_active_task_records, load_stale_active_task_records, load_task_runtime_state, load_test_results, normalize_task_runtime_state, prune_stale_active_task_records, save_task_runtime_state, utc_now_iso
from .task import FINAL_VERIFY_TASK_ID, Task, all_tasks, load_requirement_ids, next_generated_task_id, pick_next_task, save_tasks
from .token_usage import execution_token_records_for_project, execution_token_records_for_task, extract_opencode_usage, safe_int


PAUSE_FILE = ".app-delivery-pause"
RUNTIME_STAGNATION_THRESHOLD_SECONDS = 600
RUNTIME_READ_ONLY_STREAK_THRESHOLD = 20
GATE_REPAIR_TASK_PREFIX = "Validation Gate Repair Bundle"
FINAL_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
CODE_LINE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sass",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
CODE_LINE_FILENAMES = {"Dockerfile", "Makefile"}
CODE_LINE_TEST_PARTS = {"__tests__", "e2e", "spec", "specs", "test", "tests"}
CODE_LINE_EXCLUDED_PARTS = {
    ".app-delivery-runtime",
    ".cache",
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".turbo",
    ".venv",
    ".vite",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "docs",
    "node_modules",
    "playwright-report",
    "target",
    "test-results",
    "venv",
}


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


def _format_minutes(duration_minutes: int | None) -> str:
    if duration_minutes is None:
        return "unavailable"
    hours, minutes = divmod(max(0, duration_minutes), 60)
    return f"{hours} hours {minutes} mins"


def _duration_between(started_at: dt.datetime | None, completed_at: dt.datetime | None) -> tuple[int | None, str]:
    if started_at is None or completed_at is None:
        return None, "unavailable"
    minutes = max(0, int((completed_at - started_at).total_seconds() // 60))
    return minutes, _format_minutes(minutes)


def _runtime_activity_from_log(runtime_state: dict[str, Any]) -> dict[str, Any]:
    log_file = str(runtime_state.get("log_file") or "").strip()
    session_id = str(runtime_state.get("session_id") or "").strip()
    if not log_file or not session_id:
        return {}
    path = Path(log_file)
    if not path.exists() or not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    matched: list[dict[str, Any]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload_session_id = str(payload.get("sessionID") or payload.get("session_id") or "").strip()
        if payload_session_id != session_id:
            continue
        matched.append(payload)
    if not matched:
        return {}

    def _tool_kind(tool_name: str) -> str:
        normalized = str(tool_name or "").strip()
        if normalized in {"write", "edit", "str_replace", "insert", "delete", "rename", "create_file", "apply_patch"}:
            return "mutation"
        if normalized in {"read", "grep", "glob", "ls", "find", "view"}:
            return "read_only"
        return "other"

    def _event_time_iso(payload: dict[str, Any]) -> str | None:
        raw = payload.get("timestamp")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    last_tool_name: str | None = None
    last_tool_at: str | None = None
    last_activity_kind: str | None = None
    last_mutation_at: str | None = None
    read_only_streak = 0
    streak_open = True
    for payload in reversed(matched):
        if payload.get("type") != "tool_use":
            continue
        part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        tool_name = str(part.get("tool") or "").strip()
        if not tool_name:
            continue
        kind = _tool_kind(tool_name)
        when = _event_time_iso(payload)
        if last_tool_name is None:
            last_tool_name = tool_name
            last_tool_at = when
            last_activity_kind = kind
        if kind == "mutation" and last_mutation_at is None:
            last_mutation_at = when
        if streak_open:
            if kind == "read_only":
                read_only_streak += 1
            else:
                streak_open = False
    return {
        "last_tool_name": last_tool_name,
        "last_tool_at": last_tool_at,
        "last_activity_kind": last_activity_kind,
        "last_mutation_at": last_mutation_at,
        "read_only_streak": read_only_streak,
        "evidence_source": "wrapper_log",
    }


def _runtime_activity_snapshot(runtime_state: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "last_tool_name": str(runtime_state.get("last_tool_name") or "").strip() or None,
        "last_tool_at": str(runtime_state.get("last_tool_at") or "").strip() or None,
        "last_activity_kind": str(runtime_state.get("last_activity_kind") or "").strip() or None,
        "last_mutation_at": str(runtime_state.get("last_mutation_at") or "").strip() or None,
        "read_only_streak": int(runtime_state.get("read_only_streak") or 0),
        "evidence_source": "runtime_state",
    }
    if snapshot["last_tool_at"] and snapshot["last_tool_name"]:
        return snapshot
    fallback = _runtime_activity_from_log(runtime_state)
    return {**snapshot, **fallback}


def _runtime_attention_payload(runtime_state: dict[str, Any]) -> dict[str, Any] | None:
    if str(runtime_state.get("status") or "").strip() != "running":
        return None
    started_at = _parse_iso_datetime(runtime_state.get("started_at"))
    if started_at is None:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    elapsed_seconds = int(max(0, (now - started_at).total_seconds()))
    if elapsed_seconds < RUNTIME_STAGNATION_THRESHOLD_SECONDS:
        return None
    activity = _runtime_activity_snapshot(runtime_state)
    read_only_streak = int(activity.get("read_only_streak") or 0)
    last_mutation_at = _parse_iso_datetime(activity.get("last_mutation_at"))
    seconds_since_mutation = int(max(0, (now - last_mutation_at).total_seconds())) if last_mutation_at is not None else None
    if seconds_since_mutation is not None and seconds_since_mutation < RUNTIME_STAGNATION_THRESHOLD_SECONDS:
        return None
    last_tool_at = _parse_iso_datetime(activity.get("last_tool_at"))
    seconds_since_tool = int(max(0, (now - last_tool_at).total_seconds())) if last_tool_at is not None else None
    if seconds_since_tool is not None and seconds_since_tool < RUNTIME_STAGNATION_THRESHOLD_SECONDS:
        return None
    last_tool_name = str(activity.get("last_tool_name") or "").strip() or None
    last_activity_kind = str(activity.get("last_activity_kind") or "").strip() or None
    if read_only_streak >= RUNTIME_READ_ONLY_STREAK_THRESHOLD:
        kind = "read_only_stall"
        message = "Runtime is still alive but has accumulated a long read-only tool streak without any recent file mutation."
    else:
        kind = "silent_stall"
        message = "Runtime is still alive but has not produced any recent actionable tool progress."
    return {
        "suspected": True,
        "kind": kind,
        "message": message,
        "elapsed_seconds": elapsed_seconds,
        "seconds_since_tool": seconds_since_tool,
        "seconds_since_mutation": seconds_since_mutation,
        "read_only_streak": read_only_streak,
        "last_tool_name": last_tool_name,
        "last_activity_kind": last_activity_kind,
        "session_id": str(runtime_state.get("session_id") or "").strip() or None,
        "evidence_source": str(activity.get("evidence_source") or "runtime_state").strip() or "runtime_state",
    }


def _collapse_repair_scope_paths(paths: list[str], *, limit: int = 20) -> list[str]:
    collapsed: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        normalized = str(raw_path or "").strip()
        if not normalized:
            continue
        pure = PurePosixPath(normalized.rstrip("/"))
        parts = pure.parts
        if pure.suffix:
            pure = pure.parent
            parts = pure.parts
        collapsed_path = normalized
        if len(parts) >= 4 and parts[0] in {"backend", "frontend", "mock-server"} and parts[1] == "src":
            collapsed_path = "/".join(parts[:4]) + "/"
        elif len(parts) >= 3 and parts[0] in {"backend", "frontend", "mock-server"}:
            collapsed_path = "/".join(parts[:3]) + "/"
        elif len(parts) >= 2 and parts[0] == "docs":
            collapsed_path = "/".join(parts[:2]) + "/"
        if collapsed_path in seen:
            continue
        seen.add(collapsed_path)
        collapsed.append(collapsed_path)
    return collapsed[:limit]


def _supplement_test_specs_for_missing_types(project_root: Path, missing_types: list[str], existing_specs: list[str]) -> list[str]:
    supplemental_specs: list[str] = []
    supplemental_seen: set[str] = set()

    def add_supplemental(spec: str) -> None:
        normalized = str(spec).strip()
        if not normalized or normalized in supplemental_seen:
            return
        supplemental_seen.add(normalized)
        supplemental_specs.append(normalized)

    frontend_dir = project_root / "frontend"
    if frontend_dir.joinpath("package.json").exists():
        if "unit" in missing_types:
            add_supplemental("npm run test")
        if "typecheck" in missing_types:
            add_supplemental("npm run typecheck")
        if "lint" in missing_types:
            add_supplemental("npm run lint")
        if "build" in missing_types:
            add_supplemental("npm run build")
        if "browser" in missing_types or "e2e" in missing_types:
            add_supplemental("npm run e2e")
    backend_tests_root = backend_test_root(PYTHON_REACT_CONTRACT)
    if "integration" in missing_types and project_root.joinpath(*backend_tests_root.split("/")).exists():
        add_supplemental(f"{backend_tests_root}/")
    if "contract" in missing_types and project_root.joinpath(PYTHON_REACT_CONTRACT.mock_server_root, "tests").exists():
        add_supplemental(f"{PYTHON_REACT_CONTRACT.mock_server_root}/tests/")

    ordered: list[str] = []
    seen: set[str] = set()
    for spec in supplemental_specs + list(existing_specs):
        normalized = str(spec).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered[:10]


def _ensure_gate_repair_task(project_root: Path, tasks: list[Any], gates_payload: dict[str, Any]) -> list[Any]:
    gate_rows = gates_payload.get("gates") if isinstance(gates_payload.get("gates"), list) else []
    blocked_gates = [gate for gate in gate_rows if isinstance(gate, dict) and str(gate.get("status") or "").strip() == "blocked"]
    if not blocked_gates:
        return tasks
    by_id = {task.id: task for task in tasks}
    eligible_gates: list[dict[str, Any]] = []
    eligible_candidate_ids: list[str] = []
    for gate in blocked_gates:
        candidate_ids = [
            str(task_id).strip()
            for task_id in gate.get("repair_candidates", [])
            if str(task_id).strip()
        ]
        if not candidate_ids:
            continue
        candidates = [by_id.get(task_id) for task_id in candidate_ids]
        if any(candidate is None for candidate in candidates):
            continue
        if any(candidate.status != "verified" for candidate in candidates if candidate is not None):
            continue
        eligible_gates.append(gate)
        for task_id in candidate_ids:
            if task_id not in eligible_candidate_ids:
                eligible_candidate_ids.append(task_id)
    if not eligible_gates:
        return tasks

    gate_ids = {str(gate.get("id") or "").strip() for gate in eligible_gates if str(gate.get("id") or "").strip()}
    matching_repair_tasks = [
        task
        for task in tasks
        if getattr(task, "task_kind", "") == "repair"
        and str(task.title or "").startswith(GATE_REPAIR_TASK_PREFIX)
        and (not gate_ids or any(gate_id in str(task.title or "") for gate_id in gate_ids))
    ]
    if len(matching_repair_tasks) > 1:
        preferred = next((task for task in reversed(matching_repair_tasks) if task.status in {"verified", "exception"}), None)
        if preferred is None:
            preferred = next((task for task in reversed(matching_repair_tasks) if task.status not in {"verified", "exception"}), None)
        if preferred is None:
            preferred = matching_repair_tasks[-1]
        drop_ids = {task.id for task in matching_repair_tasks if task.id != preferred.id and task.status not in {"verified", "exception"}}
        if drop_ids:
            tasks = [task for task in tasks if task.id not in drop_ids]
            by_id = {task.id: task for task in tasks}
            save_tasks(project_root, tasks)
            matching_repair_tasks = [task for task in tasks if getattr(task, "task_kind", "") == "repair" and str(task.title or "").startswith(GATE_REPAIR_TASK_PREFIX) and (not gate_ids or any(gate_id in str(task.title or "") for gate_id in gate_ids))]
    existing_repair_task = matching_repair_tasks[-1] if matching_repair_tasks else None
    if existing_repair_task is not None and existing_repair_task.status in {"verified", "exception"}:
        return tasks
    repair_task_id = existing_repair_task.id if existing_repair_task is not None else next_generated_task_id(tasks)
    candidate_tasks = [by_id[task_id] for task_id in eligible_candidate_ids if task_id in by_id]
    requirements = sorted(
        {
            requirement_id
            for gate in eligible_gates
            for requirement_id in gate.get("scope_requirements", [])
            if str(requirement_id).strip()
        }
        | {
            requirement_id
            for task in candidate_tasks
            for requirement_id in task.requirements
        }
    )
    output_tests = list(
        dict.fromkeys(
            str(spec).strip()
            for task in candidate_tasks
            for spec in task.output_tests
            if str(spec).strip()
        )
    )[:10]
    missing_types = sorted(
        {
            str(test_type).strip()
            for gate in eligible_gates
            for test_type in gate.get("missing_test_types", [])
            if str(test_type).strip()
        }
    )
    output_tests = _supplement_test_specs_for_missing_types(project_root, missing_types, output_tests)
    output_paths = _collapse_repair_scope_paths(
        [
            *[
                path
                for task in candidate_tasks
                for path in task.output_paths
                if str(path).strip()
            ],
            *[
                str(gate.get("report_artifact") or "").strip()
                for gate in eligible_gates
                if str(gate.get("report_artifact") or "").strip()
            ],
        ]
    )
    if not output_paths:
        output_paths = ["docs/reviews/"]
    title_suffix = ", ".join(str(gate.get("id") or "").strip() for gate in eligible_gates[:3] if str(gate.get("id") or "").strip())
    title = GATE_REPAIR_TASK_PREFIX if not title_suffix else f"{GATE_REPAIR_TASK_PREFIX} ({title_suffix}{'...' if len(eligible_gates) > 3 else ''})"
    replacement = Task.from_dict(
        {
            "id": repair_task_id,
            "title": title,
            "status": existing_repair_task.status if existing_repair_task is not None else "pending",
            "task_kind": "repair",
            "requirements": requirements,
            "acceptance_scenarios": [],
            "dependencies": [task.id for task in tasks if task.id not in {repair_task_id, FINAL_VERIFY_TASK_ID} and task.status == "verified"],
            "output_tests": output_tests,
            "output_paths": output_paths,
            "blocked_reason": "validation gate repair bundle",
            "attempts": existing_repair_task.attempts if existing_repair_task is not None else 0,
        }
    )

    updated: list[Any] = []
    replaced = False
    for task in tasks:
        if task.id == repair_task_id:
            updated.append(replacement)
            replaced = True
            continue
        updated.append(task)
    if not replaced:
        insert_at = len(updated)
        for index, task in enumerate(updated):
            if task.id == FINAL_VERIFY_TASK_ID:
                insert_at = index
                break
        updated.insert(insert_at, replacement)
    save_tasks(project_root, updated)
    return updated


def _task_log_usage_from_runtime_state(runtime_state: dict[str, Any]) -> dict[str, Any] | None:
    log_file = str(runtime_state.get("log_file") or "").strip()
    if not log_file:
        return None
    path = Path(log_file)
    if not path.exists() or not path.is_file():
        return None
    try:
        return extract_opencode_usage(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _task_log_first_started_at(project_root: Path, task_id: str) -> dt.datetime | None:
    task_label = str(task_id or "").strip()
    if not task_label:
        return None
    task_log = project_root / ".app-delivery-runtime" / "task-log.jsonl"
    try:
        lines = task_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    starts: list[dt.datetime] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("task_id") or "").strip() != task_label:
            continue
        if str(payload.get("message") or "").strip() != f"Started implementation {task_label}":
            continue
        started_at = _parse_iso_datetime(payload.get("ts"))
        if started_at is not None:
            starts.append(started_at)
    return min(starts) if starts else None


def _task_log_active_duration_minutes(project_root: Path, task_id: str) -> int | None:
    task_label = str(task_id or "").strip()
    if not task_label:
        return None
    task_log = project_root / ".app-delivery-runtime" / "task-log.jsonl"
    try:
        lines = task_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    starts: list[dt.datetime] = []
    total_seconds = 0
    saw_terminal = False
    terminal_messages = {
        f"Completed implementation {task_label}",
        f"Failed implementation {task_label}",
        f"Stalled implementation {task_label}",
    }
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("task_id") or "").strip() != task_label:
            continue
        timestamp = _parse_iso_datetime(payload.get("ts"))
        if timestamp is None:
            continue
        message = str(payload.get("message") or "").strip()
        if message == f"Started implementation {task_label}":
            starts.append(timestamp)
            continue
        if message not in terminal_messages or not starts:
            continue
        started_at = starts.pop()
        total_seconds += max(0, int((timestamp - started_at).total_seconds()))
        saw_terminal = True
    if not saw_terminal:
        return None
    return max(0, total_seconds // 60)


def _task_started_at_for_metrics(project_root: Path, task: Any, runtime_state: dict[str, Any]) -> dt.datetime | None:
    candidates = [
        _parse_iso_datetime(runtime_state.get("first_started_at")),
        _task_log_first_started_at(project_root, task.id),
        _parse_iso_datetime(task.started_at),
        _parse_iso_datetime(runtime_state.get("started_at")),
    ]
    return min((candidate for candidate in candidates if candidate is not None), default=None)


def _task_metrics(project_root: Path, task: Any) -> dict[str, Any]:
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
    started_at = _task_started_at_for_metrics(project_root, task, runtime_state)
    completed_at = _parse_iso_datetime(task.verified_at or task.completed_at)
    duration_minutes, duration_formatted = _duration_between(started_at, completed_at)
    active_duration_minutes = _task_log_active_duration_minutes(project_root, task.id)
    effective_duration_minutes = active_duration_minutes if active_duration_minutes is not None else duration_minutes
    effective_duration_formatted = _format_minutes(effective_duration_minutes)
    records = execution_token_records_for_task(project_root, task.id)
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cache_read_tokens = 0
    records_count = 0
    for record in records:
        records_count += 1
        prompt_tokens += safe_int(record.get("prompt_tokens"))
        completion_tokens += safe_int(record.get("completion_tokens"))
        total_tokens += safe_int(record.get("total_tokens"))
        cache_read_tokens += safe_int(record.get("cache_read_tokens"))
    if records_count == 0 and runtime_state:
        fallback = _task_log_usage_from_runtime_state(runtime_state)
        if fallback:
            prompt_tokens = safe_int(fallback.get("prompt_tokens"))
            completion_tokens = safe_int(fallback.get("completion_tokens"))
            total_tokens = safe_int(fallback.get("total_tokens"))
            cache_read_tokens = safe_int(fallback.get("cache_read_tokens"))
            records_count = 1
    return {
        "task_id": task.id,
        "title": task.title,
        "status": task.status,
        "session_id": task.status_session_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z") if started_at else None,
        "completed_at": completed_at.isoformat().replace("+00:00", "Z") if completed_at else None,
        "duration_minutes": duration_minutes,
        "duration_formatted": duration_formatted,
        "active_duration_minutes": active_duration_minutes,
        "active_duration_formatted": _format_minutes(active_duration_minutes),
        "effective_duration_minutes": effective_duration_minutes,
        "effective_duration_formatted": effective_duration_formatted,
        "prompt_tokens": prompt_tokens or None,
        "completion_tokens": completion_tokens or None,
        "total_tokens": total_tokens or None,
        "cache_read_tokens": cache_read_tokens or None,
        "token_status": "available" if records_count else "unavailable",
        "token_records": records_count,
    }


def _planning_metrics(project_dir: Path) -> dict[str, Any]:
    stage_dir = project_dir / ".app-delivery-runtime" / "stage-inputs"
    timestamps: list[dt.datetime] = []
    for path in sorted(stage_dir.glob("*.json")):
        candidate = _parse_iso_datetime(load_test_results(project_dir).get("generated_at"))
        try:
            timestamps.append(dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc))
        except OSError:
            continue
    if not timestamps:
        return {
            "started_at": None,
            "completed_at": None,
            "planning_lead_minutes": None,
            "planning_lead_formatted": "unavailable",
        }
    started_at = min(timestamps)
    completed_at = max(timestamps)
    minutes, formatted = _duration_between(started_at, completed_at)
    return {
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "planning_lead_minutes": minutes,
        "planning_lead_formatted": formatted,
    }


def _project_token_summary(project_root: Path) -> dict[str, Any]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cache_read_tokens = 0
    records = execution_token_records_for_project(project_root)
    for record in records:
        prompt_tokens += safe_int(record.get("prompt_tokens"))
        completion_tokens += safe_int(record.get("completion_tokens"))
        total_tokens += safe_int(record.get("total_tokens"))
        cache_read_tokens += safe_int(record.get("cache_read_tokens"))
    return {
        "status": "available" if records else "unavailable",
        "execution_prompt_tokens": prompt_tokens or None,
        "execution_completion_tokens": completion_tokens or None,
        "execution_total_tokens": total_tokens or None,
        "execution_cache_read_tokens": cache_read_tokens or None,
        "execution_records": len(records),
        "note": "Execution runtime usage is aggregated from project-local opencode wrapper logs. Planning/review Hermes token linkage is not yet available per project." if records else "No project-local execution token records were found yet. Planning/review Hermes token linkage is not yet available per project.",
    }


def _count_nonblank_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
    except OSError:
        return 0


def _is_test_code_path(relative_path: Path) -> bool:
    parts = {part.casefold() for part in relative_path.parts[:-1]}
    if parts.intersection(CODE_LINE_TEST_PARTS):
        return True
    stem = relative_path.stem.casefold()
    name = relative_path.name.casefold()
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    )


def _project_code_line_summary(project_root: Path) -> dict[str, Any]:
    totals = {
        "total": 0,
        "application": 0,
        "test": 0,
        "backend": 0,
        "frontend": 0,
        "other": 0,
        "files": 0,
        "application_files": 0,
        "test_files": 0,
        "backend_files": 0,
        "frontend_files": 0,
        "other_files": 0,
        "basis": "nonblank lines in code files, excluding dependencies, build outputs, runtime state, and docs; total includes application and test code",
    }
    for current_root, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [name for name in dirnames if name not in CODE_LINE_EXCLUDED_PARTS]
        current_dir = Path(current_root)
        for filename in filenames:
            path = current_dir / filename
            if path.is_symlink():
                continue
            if path.suffix not in CODE_LINE_EXTENSIONS and path.name not in CODE_LINE_FILENAMES:
                continue
            try:
                relative = path.relative_to(project_root)
            except ValueError:
                continue
            first_part = relative.parts[0] if relative.parts else ""
            if first_part == "backend":
                bucket = "backend"
            elif first_part == "frontend":
                bucket = "frontend"
            else:
                bucket = "other"
            line_count = _count_nonblank_lines(path)
            totals["total"] += line_count
            totals[bucket] += line_count
            totals["files"] += 1
            totals[f"{bucket}_files"] += 1
            if _is_test_code_path(relative):
                totals["test"] += line_count
                totals["test_files"] += 1
            else:
                totals["application"] += line_count
                totals["application_files"] += 1
    return totals


def _backfill_missing_status_session_ids(project_root: Path, tasks: list[Any]) -> list[Any]:
    changed = False
    updated: list[Any] = []
    for task in tasks:
        if str(task.status_session_id or "").strip():
            updated.append(task)
            continue
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
        session_id = str(runtime_state.get("session_id") or "").strip()
        if not session_id:
            for record in reversed(execution_token_records_for_task(project_root, task.id)):
                session_id = str(record.get("session_id") or "").strip()
                if session_id:
                    break
        if not session_id:
            updated.append(task)
            continue
        data = task.to_dict()
        data["status_session_id"] = session_id
        updated.append(type(task).from_dict(data))
        changed = True
    if changed:
        save_tasks(project_root, updated)
    return updated


def _prune_superseded_repair_tasks(project_root: Path, tasks: list[Any]) -> list[Any]:
    final_runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID))
    current_final_repair_task_id = str(final_runtime_state.get("repair_task_id") or "").strip()
    final_task = next((task for task in tasks if task.id == FINAL_VERIFY_TASK_ID), None)
    final_dependency_ids = {str(task_id).strip() for task_id in getattr(final_task, "dependencies", []) if str(task_id).strip()}
    repair_groups: dict[str, list[Any]] = {}
    for task in tasks:
        if getattr(task, "task_kind", "") != "repair":
            continue
        title = str(task.title or "").strip()
        if not title:
            continue
        repair_groups.setdefault(title, []).append(task)

    drop_ids: set[str] = set()
    replacements: dict[str, str] = {}
    for grouped in repair_groups.values():
        if len(grouped) < 2:
            continue
        terminal = [task for task in grouped if task.status in {"verified", "exception"}]
        if not terminal:
            continue
        preferred = terminal[-1]
        for task in grouped:
            if task.status not in {"verified", "exception"}:
                if task.id == current_final_repair_task_id and task.id in final_dependency_ids:
                    continue
                drop_ids.add(task.id)
                replacements[task.id] = preferred.id

    if not drop_ids:
        return tasks

    updated = [task for task in tasks if task.id not in drop_ids]
    save_tasks(project_root, updated)
    current_repair_task_id = str(final_runtime_state.get("repair_task_id") or "").strip()
    replacement_id = replacements.get(current_repair_task_id)
    if replacement_id:
        save_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID, {"repair_task_id": replacement_id})
    return updated


def _prune_obsolete_gate_repair_tasks(project_root: Path, tasks: list[Any], gates_payload: dict[str, Any]) -> list[Any]:
    gate_rows = gates_payload.get("gates") if isinstance(gates_payload.get("gates"), list) else []
    non_verified_gates = [
        gate
        for gate in gate_rows
        if isinstance(gate, dict) and str(gate.get("status") or "").strip() not in {"", "verified"}
    ]
    if non_verified_gates:
        return tasks

    drop_ids = {
        task.id
        for task in tasks
        if getattr(task, "task_kind", "") == "repair"
        and str(task.title or "").startswith(GATE_REPAIR_TASK_PREFIX)
        and task.status == "pending"
        and not str(getattr(task, "status_session_id", "") or "").strip()
        and int(getattr(task, "attempts", 0) or 0) == 0
    }
    if not drop_ids:
        return tasks
    updated = [task for task in tasks if task.id not in drop_ids]
    save_tasks(project_root, updated)
    return updated


def _reconcile_final_repair_runtime_state(project_root: Path, tasks: list[Any]) -> dict[str, Any]:
    final_runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID))
    current_repair_task_id = str(final_runtime_state.get("repair_task_id") or "").strip()
    if current_repair_task_id and any(task.id == current_repair_task_id for task in tasks):
        return final_runtime_state

    final_repair_tasks = [
        task
        for task in tasks
        if getattr(task, "task_kind", "") == "repair"
        and str(task.title or "").startswith(FINAL_REPAIR_TASK_PREFIX)
    ]
    if not final_repair_tasks:
        if current_repair_task_id:
            save_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID, {"repair_task_id": None})
            final_runtime_state["repair_task_id"] = None
        return final_runtime_state

    preferred = next((task for task in reversed(final_repair_tasks) if task.status in {"verified", "exception"}), None)
    if preferred is None:
        preferred = final_repair_tasks[-1]
    save_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID, {"repair_task_id": preferred.id})
    final_runtime_state["repair_task_id"] = preferred.id
    return final_runtime_state


def _execution_lock_live(project_root: Path) -> bool:
    lock_path = project_root / ".app-delivery-runtime" / "locks" / "execution.lock"
    return lock_file_is_locked(lock_path)


def _trim_status_text(value: Any, *, limit: int = 500) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _task_attention_payload(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "blocked_reason": _trim_status_text(getattr(task, "blocked_reason", None)),
        "review_artifact": getattr(task, "review_artifact", None),
        "status_session_id": getattr(task, "status_session_id", None),
    }


def _stale_active_ledger_tasks(project_root: Path, ledger_tasks: list[Any], display_tasks: list[Any]) -> list[dict[str, Any]]:
    display_by_id = {task.id: task for task in display_tasks}
    stale: list[dict[str, Any]] = []
    for task in ledger_tasks:
        if task.status != "active":
            continue
        display_task = display_by_id.get(task.id)
        if display_task is None or display_task.status == "active":
            continue
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
        stale.append(
            {
                **_task_attention_payload(task),
                "display_status": display_task.status,
                "runtime_status": str(runtime_state.get("status") or "").strip() or None,
                "runtime_pid": runtime_state.get("runtime_pid"),
                "wrapper_pid": runtime_state.get("wrapper_pid"),
                "last_output_at": str(runtime_state.get("last_output_at") or "").strip() or None,
            }
        )
    return stale


def _requirements_source_archived(project_root: Path) -> bool:
    docs_dir = project_root / "docs"
    return any(path.is_file() for path in docs_dir.glob("requirements-source.*"))


def _delivery_claim_allowed(tasks: list[Any], invalid_verified_task_ids: set[str] | None = None) -> bool:
    invalid_ids = set(invalid_verified_task_ids or set())
    actionable = [task for task in tasks if task.id != FINAL_VERIFY_TASK_ID and task.status != "cancelled"]
    final_task = next((task for task in tasks if task.id == FINAL_VERIFY_TASK_ID), None)
    return final_task is not None and final_task.status == "verified" and not invalid_ids and not any(
        task.status in {"pending", "active", "review_pending", "blocked", "exception"}
        for task in actionable
    )


def _display_tasks(project_root: Path, tasks: list[Any]) -> list[Any]:
    active_records = load_active_task_records(project_root)
    live_task_ids = {str(row.get("task_id") or "").strip() for row in active_records if str(row.get("task_id") or "").strip()}
    active = current_session(project_root)
    execution_live = _execution_lock_live(project_root)
    display: list[Any] = []
    for task in tasks:
        if task.status != "active":
            display.append(task)
            continue
        session_match = bool(execution_live and active and str(active.id or "").strip() and active.current_task_id == task.id)
        if task.id in live_task_ids or session_match:
            display.append(task)
            continue
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
        if str(runtime_state.get("status") or "").strip() in {"completed", "failed", "interrupted"}:
            display.append(task)
            continue
        data = task.to_dict()
        data["status"] = "pending"
        data["status_session_id"] = None
        display.append(type(task).from_dict(data))
    return display


def _active_task_payload(project_root: Path, tasks: list[Any]) -> dict[str, Any] | None:
    active_records = sorted(load_active_task_records(project_root), key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    active_task_ids = {task.id for task in tasks if task.status == "active"}
    active_records = [record for record in active_records if str(record.get("task_id") or "").strip() in active_task_ids]
    if active_records:
        record = active_records[0]
        task_id = str(record.get("task_id") or "").strip()
        task = next((row for row in tasks if row.id == task_id), None)
        if task is not None:
            payload = task.to_dict()
            payload["phase"] = record.get("phase") or "implementation"
            payload["runtime_pid"] = record.get("runtime_pid")
            payload["updated_at"] = record.get("updated_at")
            payload["log_file"] = record.get("log_file")
            payload["session_id"] = record.get("session_id")
            return payload
        return {
            "id": task_id or None,
            "title": record.get("task_title"),
            "status": "active",
            "phase": record.get("phase") or "runtime",
            "runtime_pid": record.get("runtime_pid"),
            "updated_at": record.get("updated_at"),
            "log_file": record.get("log_file"),
            "session_id": record.get("session_id"),
        }
    live_active_tasks = [task for task in tasks if task.status == "active"]
    execution_live = _execution_lock_live(project_root)
    if live_active_tasks:
        task = live_active_tasks[0]
        if not execution_live:
            runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
            if str(runtime_state.get("status") or "").strip() in {"completed", "failed", "interrupted"}:
                payload = task.to_dict()
                payload["phase"] = runtime_state.get("phase") or "implementation"
                payload["runtime_pid"] = runtime_state.get("runtime_pid")
                payload["updated_at"] = runtime_state.get("updated_at")
                payload["log_file"] = runtime_state.get("log_file")
                payload["session_id"] = runtime_state.get("session_id")
                payload["runtime_state"] = runtime_state
                return payload
            return running_runtime_task_payload(project_root, tasks)
        payload = task.to_dict()
        payload["phase"] = "implementation"
        active = current_session(project_root)
        if active is not None and active.current_task_id == task.id:
            payload["session_id"] = active.id
            payload["updated_at"] = active.last_heartbeat
        return payload
    active = current_session(project_root)
    if active is None or not active.current_task_id:
        return running_runtime_task_payload(project_root, tasks)
    if not execution_live:
        return running_runtime_task_payload(project_root, tasks)
    task = next((row for row in tasks if row.id == active.current_task_id), None)
    if task is None:
        return {
            "id": active.current_task_id,
            "title": active.title,
            "status": "active",
            "session_id": active.id,
            "updated_at": active.last_heartbeat,
        }
    if task.status != "active":
        return running_runtime_task_payload(project_root, tasks)
    payload = task.to_dict()
    payload["session_id"] = active.id
    payload["updated_at"] = active.last_heartbeat
    return payload


def _orphan_active_task_records(project_root: Path, tasks: list[Any]) -> list[dict[str, Any]]:
    active_task_ids = {task.id for task in tasks if task.status == "active"}
    orphans: list[dict[str, Any]] = []
    for record in sorted(load_active_task_records(project_root), key=lambda row: str(row.get("updated_at") or ""), reverse=True):
        task_id = str(record.get("task_id") or "").strip()
        if task_id and task_id not in active_task_ids:
            orphans.append(record)
    return orphans


def render_project_summary(project_root: Path | str, payload: dict[str, Any]) -> Path:
    project_dir = resolve_project_root(project_root)
    summary_json_path = project_dir / "docs" / "project-summary.json"
    summary_json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary_md_path = project_dir / "docs" / "project-summary.md"
    project_name = project_dir.name
    planning = payload.get("planning") if isinstance(payload.get("planning"), dict) else {}
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    duration = payload.get("duration") if isinstance(payload.get("duration"), dict) else {}
    verified_metrics = payload.get("verified_task_metrics") if isinstance(payload.get("verified_task_metrics"), dict) else {}
    code_lines = payload.get("code_lines") if isinstance(payload.get("code_lines"), dict) else {}
    lines = [
        f"# Project Summary - {project_name}",
        "",
        "> Generated from the canonical JSON ledgers. This summary is observational only; final delivery is proven by the task ledger, review artifacts, and verification evidence.",
        "",
        f"- Generated at: {payload.get('generated_at', 'unknown')}",
        f"- Effective Code Lines: {code_lines.get('total', 0)}",
        f"- Application Code Lines: {code_lines.get('application', 0)}",
        f"- Test Code Lines: {code_lines.get('test', 0)}",
        "- Code Line Formula: Effective Code Lines = Application Code Lines + Test Code Lines",
        f"- Backend Code Lines: {code_lines.get('backend', 0)}",
        f"- Frontend Code Lines: {code_lines.get('frontend', 0)}",
        f"- Other Code Lines: {code_lines.get('other', 0)}",
        f"- Code Files Counted: {code_lines.get('files', 0)}",
        f"- Elapsed Duration: {duration.get('duration_formatted', 'unavailable')}",
        f"- Planning lead time: {planning.get('planning_lead_formatted', 'unavailable')}",
        f"- Billable tokens: {tokens.get('execution_total_tokens', 'unavailable')}",
        f"- Observed tokens incl. cache reads: {((tokens.get('execution_total_tokens') or 0) + (tokens.get('execution_cache_read_tokens') or 0)) if tokens.get('status') == 'available' else 'unavailable'}",
        f"- Project: {payload.get('project', project_dir)}",
        f"- Control Status Hint: {payload.get('control_status_hint', 'unknown')}",
        f"- Delivery Claim Allowed: {payload.get('delivery_claim_allowed', False)}",
        f"- Paused: {payload.get('paused', False)}",
        f"- Final Verify Ready: {payload.get('final_verify_ready', False)}",
        f"- Requirements Source Archived: {payload.get('requirements_source_archived', False)}",
        "",
    ]
    if payload.get("requirements_source_archived") is False:
        lines.extend(
            [
                "## Evidence Warning",
                "",
                "- Raw requirements archive is missing. `docs/requirements.json` exists, but `docs/requirements-source.*` was not archived for this project instance.",
                "- If the original requirements document is still available, rerun preflight/start with `--requirements` or re-import spec-review with `source_requirements_path` so the raw source is preserved as evidence.",
                "",
            ]
        )
    lines.extend([
        "## Token Coverage",
        "",
        f"- Execution runtime (project-local): {tokens.get('execution_total_tokens', 'unavailable')}",
        f"- Cache reads: {tokens.get('execution_cache_read_tokens', 'unavailable')}",
        f"- Status: {tokens.get('status', 'unavailable')}",
        f"- Note: {tokens.get('note', 'unavailable')}",
        "",
        "## Verified Task Metrics",
        "",
        f"- Verified tasks: {verified_metrics.get('verified_tasks', 0)}",
        f"- Tasks with recorded token metrics: {verified_metrics.get('tasks_with_total_tokens', 0)}",
        f"- Summed verified task active durations: {verified_metrics.get('task_duration_minutes_sum', 'unavailable')}",
        f"- Verified task execution tokens: {verified_metrics.get('task_total_tokens_sum', 'unavailable')}",
        "",
    ])
    lines.extend([
        "",
        "## Task Metrics",
        "",
        "Task duration shows active implementation runtime intervals when task-log events are available. The JSON also keeps wall-clock span fields from first start to final verified/completed time for auditability.",
        "",
        "| Task | Active Duration | Tokens | Session |",
        "| --- | --- | --- | --- |",
    ])
    for row in payload.get("task_metrics", []) if isinstance(payload.get("task_metrics"), list) else []:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| {row.get('task_id', '-')} | {row.get('effective_duration_formatted') or row.get('duration_formatted', 'unavailable')} | {row.get('total_tokens', 'unavailable')} | {row.get('session_id') or '-'} |"
        )
    summary_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_json_path


def render_release_evidence(project_root: Path | str, results_summary: str, requirement_coverage: dict[str, Any], missing_test_types: list[tuple[str, str]]) -> Path:
    project_dir = resolve_project_root(project_root)
    release_path = project_dir / "docs" / "release-evidence.md"
    deferral_path = project_dir / "docs" / "reviews" / "task-contract-deferrals.json"
    deferred_acceptance: list[dict[str, Any]] = []
    if deferral_path.exists():
        try:
            deferral_payload = json.loads(deferral_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            deferral_payload = {}
        if isinstance(deferral_payload, dict) and isinstance(deferral_payload.get("deferred_acceptance_scenarios"), list):
            deferred_acceptance = [row for row in deferral_payload["deferred_acceptance_scenarios"] if isinstance(row, dict)]
    lines = [
        "# Release Evidence",
        "",
        "## Full Suite Summary",
        "",
        results_summary or "No results.",
        "",
        "## Requirement Coverage",
        "",
        f"- Total requirements: {requirement_coverage['total']}",
        f"- Covered requirements: {requirement_coverage['covered']}",
        f"- Uncovered requirements: {', '.join(requirement_coverage['uncovered']) or 'none'}",
        "",
        "## Test Type Coverage Gaps",
        "",
    ]
    if missing_test_types:
        for requirement_id, test_type in missing_test_types:
            lines.append(f"- {requirement_id}: missing {test_type}")
    else:
        lines.append("- none")
    lines.extend(["", "## Deferred Acceptance Scenarios", ""])
    if deferred_acceptance:
        for row in deferred_acceptance:
            lines.append(
                f"- {row.get('id', '-')}: from {row.get('from_task', '-')} — {row.get('reason', '')} "
                f"(policy={row.get('policy', '-')}; review={row.get('review_artifact', '-')})"
            )
    else:
        lines.append("- none")
    release_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return release_path


def status(project_root: Path | str) -> dict[str, Any]:
    project_dir = resolve_project_root(project_root)
    prune_stale_active_task_records(project_dir)
    gates_payload = refresh_gates(project_dir)
    tasks = all_tasks(project_dir)
    tasks = _prune_obsolete_gate_repair_tasks(project_dir, tasks, gates_payload)
    tasks = _prune_superseded_repair_tasks(project_dir, tasks)
    tasks = _backfill_missing_status_session_ids(project_dir, tasks)
    tasks, invalid_verified = repair_invalid_verified_tasks(project_dir, tasks)
    final_runtime_state = _reconcile_final_repair_runtime_state(project_dir, tasks)
    display_tasks = _display_tasks(project_dir, tasks)
    by_status: dict[str, int] = {}
    for task in display_tasks:
        by_status[task.status] = by_status.get(task.status, 0) + 1
    next_task = pick_next_task(display_tasks)
    invalid_verified_task_ids = {
        task_id for task_id, issue in invalid_verified.items() if is_blocking_verified_task_issue(issue)
    }
    actionable = [
        task
        for task in display_tasks
        if task.id != FINAL_VERIFY_TASK_ID and task.status != "cancelled" and getattr(task, "task_kind", "feature") != "repair"
    ]
    all_actionable_verified = bool(actionable) and all(task.status == "verified" and task.id not in invalid_verified_task_ids for task in actionable)
    final_task = next((task for task in display_tasks if task.id == FINAL_VERIFY_TASK_ID), None)
    review_pending_task = next((task for task in display_tasks if task.status == "review_pending"), None)
    active_payload = _active_task_payload(project_dir, display_tasks)
    runtime_attention = None
    if isinstance(active_payload, dict):
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_dir, str(active_payload.get("id") or "")))
        if runtime_state:
            active_payload["runtime_state"] = runtime_state
            active_task = next((task for task in display_tasks if task.id == str(active_payload.get("id") or "")), None)
            runtime_attention = runtime_attention_payload(
                project_dir,
                active_task,
                runtime_state,
                active_record_present=bool(load_active_task_records(project_dir)),
            ) or _runtime_attention_payload(runtime_state)
            if runtime_attention is not None:
                active_payload["runtime_attention"] = runtime_attention
    next_payload = next_task.to_dict() if next_task else None
    if isinstance(next_payload, dict):
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_dir, str(next_payload.get("id") or "")))
        if runtime_state:
            next_payload["runtime_state"] = runtime_state
    review_payload = review_pending_task.to_dict() if review_pending_task else None
    if isinstance(review_payload, dict):
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_dir, str(review_payload.get("id") or "")))
        if runtime_state:
            review_payload["runtime_state"] = runtime_state
    final_verify_payload = None
    if final_task is not None or final_runtime_state:
        repair_candidates = [
            str(task_id).strip()
            for task_id in final_runtime_state.get("repair_candidates", [])
            if str(task_id).strip()
        ]
        repair_task_id = str(final_runtime_state.get("repair_task_id") or "").strip() or None
        repair_task = next((task for task in display_tasks if task.id == repair_task_id), None) if repair_task_id else None
        repair_task_status = repair_task.status if repair_task is not None else None
        final_verify_status = str(final_runtime_state.get("final_verify_status") or "").strip()
        final_repair_limit_reached = bool(final_runtime_state.get("final_repair_limit_reached"))
        if not all_actionable_verified and final_task is not None and final_task.status == "blocked":
            final_verify_status = "deferred"
        elif final_task is not None and final_task.status == "blocked" and repair_candidates and final_verify_status in {"", "blocked"} and not final_repair_limit_reached:
            final_verify_status = "repair_required"
        blocked_reason = getattr(final_task, "blocked_reason", None) if final_task is not None else None
        if final_verify_status == "deferred":
            blocked_reason = "final verification state is deferred until all non-final tasks are verified"
        final_verify_payload = {
            "task_status": final_task.status if final_task is not None else None,
            "status": final_verify_status or (final_task.status if final_task is not None else None),
            "blocked_reason": blocked_reason,
            "repair_candidates": repair_candidates,
            "repair_task_id": repair_task_id,
            "repair_task_status": repair_task_status,
            "repair_report": str(final_runtime_state.get("repair_report_artifact") or "").strip() or None,
            "missing_test_types": [
                list(item)
                for item in final_runtime_state.get("final_verify_missing_test_types", [])
                if isinstance(item, (list, tuple)) and item
            ],
            "gate_statuses": final_runtime_state.get("final_verify_gate_statuses", []),
            "will_continue": bool(repair_candidates) and repair_task_status != "exception" and final_verify_status not in {"blocked", "deferred"} and not final_repair_limit_reached,
        }
    return {
        "project": str(project_dir),
        "counts": by_status,
        "next_task": next_payload,
        "review_pending_task": review_payload,
        "active_task": active_payload,
        "runtime_attention": runtime_attention,
        "exception_tasks": [_task_attention_payload(task) for task in display_tasks if task.status == "exception"],
        "stale_active_ledger_tasks": _stale_active_ledger_tasks(project_dir, tasks, display_tasks),
        "orphan_active_tasks": _orphan_active_task_records(project_dir, display_tasks),
        "stale_active_tasks": load_stale_active_task_records(project_dir),
        "paused": (project_dir / PAUSE_FILE).exists(),
        "final_verify_ready": all_actionable_verified and final_task is not None and final_task.status == "pending",
        "final_verify": final_verify_payload,
        "invalid_verified_tasks": invalid_verified,
        "requirements_source_archived": _requirements_source_archived(project_dir),
        "gates": gates_payload,
    }


def project_summary(project_root: Path | str) -> dict[str, Any]:
    project_dir = resolve_project_root(project_root)
    tasks = all_tasks(project_dir)
    status_payload = status(project_dir)
    invalid_verified = status_payload.get("invalid_verified_tasks") if isinstance(status_payload.get("invalid_verified_tasks"), dict) else {}
    invalid_verified_task_ids = {
        task_id for task_id, issue in invalid_verified.items() if is_blocking_verified_task_issue(issue)
    }
    requirement_ids = load_requirement_ids(project_dir)
    planned_covered: set[str] = set()
    for task in tasks:
        if task.id == FINAL_VERIFY_TASK_ID:
            continue
        planned_covered.update(task.requirements)
    unplanned = sorted(req_id for req_id in requirement_ids if req_id not in planned_covered)
    test_results = load_test_results(project_dir)
    full_suite = test_results.get("full_suite_results") if isinstance(test_results.get("full_suite_results"), dict) else {}
    active = current_session(project_dir)
    active_task = status_payload["active_task"] if isinstance(status_payload.get("active_task"), dict) else None
    if active_task is None and active is not None and active.current_task_id:
        task = next((row for row in tasks if row.id == active.current_task_id), None)
        if task is not None:
            active_task = task.to_dict()
            active_task["session_id"] = active.id
            active_task["updated_at"] = active.last_heartbeat
            active_task["phase"] = "implementation"
    derived_last_heartbeat = None
    if active_task is not None:
        derived_last_heartbeat = active_task.get("updated_at")
    task_metrics = [_task_metrics(project_dir, task) for task in tasks if task.id != FINAL_VERIFY_TASK_ID]
    verified_rows = [row for row in task_metrics if str(row.get("status") or "") == "verified"]
    verified_duration_sum = sum((row.get("effective_duration_minutes") or 0) for row in verified_rows if isinstance(row.get("effective_duration_minutes"), int)) or None
    verified_token_sum = sum((row.get("total_tokens") or 0) for row in verified_rows if isinstance(row.get("total_tokens"), int)) or None
    project_start = min(
        (
            candidate
            for candidate in [
                _parse_iso_datetime(active.created_at) if active is not None else None,
                *(_parse_iso_datetime(row.get("started_at")) for row in task_metrics if isinstance(row.get("started_at"), str)),
            ]
            if candidate is not None
        ),
        default=None,
    )
    project_end = max(
        (
            candidate
            for candidate in [
                *(_parse_iso_datetime(task.verified_at or task.completed_at) for task in tasks if getattr(task, "completed_at", None) or getattr(task, "verified_at", None)),
                _parse_iso_datetime(status_payload.get("review_pending_task", {}).get("runtime_state", {}).get("completed_at")) if isinstance(status_payload.get("review_pending_task"), dict) else None,
            ]
            if candidate is not None
        ),
        default=None,
    )
    duration_minutes, duration_formatted = _duration_between(project_start, project_end)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "project": str(project_dir),
        "counts": status_payload["counts"],
        "control_status_hint": "complete" if _delivery_claim_allowed(tasks, invalid_verified_task_ids) else "not_ready",
        "delivery_claim_allowed": _delivery_claim_allowed(tasks, invalid_verified_task_ids),
        "paused": status_payload["paused"],
        "next_task": status_payload["next_task"],
        "review_pending_task": status_payload.get("review_pending_task"),
        "active_task": active_task,
        "final_verify_ready": status_payload["final_verify_ready"],
        "requirements_source_archived": status_payload.get("requirements_source_archived"),
        "gates": status_payload.get("gates"),
        "requirements": {
            "total": len(requirement_ids),
            "planned": len(planned_covered),
            "unplanned": unplanned,
        },
        "tests": full_suite,
        "recent_activity": latest_task_log_event(project_dir),
        "duration": {
            "started_at": project_start.isoformat().replace("+00:00", "Z") if project_start else None,
            "completed_at": project_end.isoformat().replace("+00:00", "Z") if project_end else None,
            "duration_minutes": duration_minutes,
            "duration_formatted": duration_formatted,
        },
        "planning": _planning_metrics(project_dir),
        "tokens": _project_token_summary(project_dir),
        "code_lines": _project_code_line_summary(project_dir),
        "task_metrics": task_metrics,
        "verified_task_metrics": {
            "verified_tasks": len(verified_rows),
            "task_duration_minutes_sum": verified_duration_sum,
            "task_total_tokens_sum": verified_token_sum,
            "tasks_with_total_tokens": len([row for row in verified_rows if row.get("total_tokens") is not None]),
        },
        "active_session": {
            "id": active.id,
            "runtime": active.runtime,
            "task_count": active.task_count,
            "created_at": active.created_at,
            "current_task_id": active.current_task_id,
            "last_heartbeat": derived_last_heartbeat or active.last_heartbeat,
        }
        if active
        else None,
    }
    render_project_summary(project_dir, payload)
    return payload


__all__ = ["PAUSE_FILE", "project_summary", "render_project_summary", "render_release_evidence", "status"]