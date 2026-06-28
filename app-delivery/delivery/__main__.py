from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .bootstrap import archive_requirements_source, doctor_report, initialize_project, start_preflight
from .control_plane import run_control, routed_status
from .errors import DeliveryError, error_payload
from .loop import DeliveryLoop, PAUSE_FILE, project_summary
from .loop_task_prompt import build_stalled_recovery_prompt
from .loop_review import code_review_request_path, final_review_request_path, import_final_review, import_task_review
from . import project_readiness as readiness
from .runtime_config import load_project_metadata, resolve_project_root, resolve_runtime
from .scaffold import write_project_structure_snapshot
from .session import RuntimeSession, current_session, retire_session, save_current_session
from .watchdog import save_watchdog_state, should_watchdog_resume, spawn_watchdog
from .stage_harness import import_arch_design, import_context_sync, import_decompose, import_spec_review, import_ui_design, load_stage_payload, stage_import_command, stage_input_path, stage_missing_error
from .loop_gitops import reapply_task_exception_patch
from .state import (
    acquire_lock,
    latest_task_log_event,
    load_active_task_records,
    load_task_runtime_state,
    normalize_task_runtime_state,
    read_lock_metadata,
    render_work_items_markdown,
    save_task_runtime_state,
    utc_now_iso,
)
from .task import all_tasks, reset_task, save_tasks


CONTROL_STEP_LIMIT = 128


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            break
        except OSError:
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)


def _terminate_runtime_for_task(project_root: Path, task_id: str) -> None:
    current_tasks = all_tasks(project_root)
    current = next((task for task in current_tasks if task.id == task_id), None)
    if current is None or current.status != "active":
        return
    active_records = [
        record
        for record in load_active_task_records(project_root)
        if str(record.get("task_id") or "").strip() == task_id
    ]
    seen_pids: set[int] = set()
    for record in active_records:
        for key in ("runtime_pid", "pid"):
            try:
                pid = int(record.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen_pids:
                continue
            seen_pids.add(pid)
            _terminate_pid(pid)
    lock_path = project_root / ".app-delivery-runtime" / "locks" / "execution.lock"
    lock_details = read_lock_metadata(lock_path)
    try:
        lock_owner_pid = int((lock_details or {}).get("pid") or 0)
    except (TypeError, ValueError):
        lock_owner_pid = 0
    if lock_owner_pid > 0 and lock_owner_pid not in seen_pids:
        _terminate_pid(lock_owner_pid)


def _seed_recovery_session(project_root: Path, *, runtime_name: str, task_id: str, title: str, session_id: str) -> None:
    active = current_session(project_root)
    if active is not None and str(active.current_task_id or "").strip() == task_id and active.runtime == runtime_name:
        if session_id:
            active.id = session_id
        active.title = title
        active.current_task_id = task_id
        active.last_heartbeat = utc_now_iso()
        save_current_session(project_root, active)
        return
    if active is not None:
        retire_session(project_root, active)
    now = utc_now_iso()
    seeded = RuntimeSession(
        id=session_id,
        runtime=runtime_name,
        task_count=0,
        created_at=now,
        status="active",
        title=title,
        last_heartbeat=now,
        current_task_id=task_id,
    )
    save_current_session(project_root, seeded)


def _run_stalled_recovery_once(
    project_root: Path | str,
    *,
    task_id: str,
    runtime: str | None,
    runtime_attention: dict[str, Any] | None = None,
    spawn_watchdog_after: bool = True,
) -> dict[str, Any] | None:
    resolved = resolve_project_root(project_root)
    current = next((task for task in all_tasks(resolved) if task.id == task_id), None)
    if current is None:
        raise DeliveryError(
            code="stalled_recovery_task_missing",
            message=f"cannot recover stalled task that does not exist: {task_id}",
            exit_code=2,
            details={"project": str(resolved), "task_id": task_id},
        )
    runtime_name = resolve_runtime(runtime, project_root=resolved)
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(resolved, task_id))
    recovery_prompt = build_stalled_recovery_prompt(
        resolved,
        current,
        runtime_state=runtime_state,
        runtime_attention=runtime_attention,
    )
    runtime_session_id = str(runtime_state.get("session_id") or current.status_session_id or "").strip()
    _terminate_runtime_for_task(resolved, task_id)
    with _project_execution_guard(resolved, already_locked=False):
        tasks = all_tasks(resolved)
        current = next((task for task in tasks if task.id == task_id), None)
        if current is None:
            raise DeliveryError(
                code="stalled_recovery_task_missing",
                message=f"cannot recover stalled task that does not exist: {task_id}",
                exit_code=2,
                details={"project": str(resolved), "task_id": task_id},
            )
        updated = reset_task(tasks, task_id, blocked_reason="stalled runtime recovery in progress")
        save_tasks(resolved, updated)
        _seed_recovery_session(
            resolved,
            runtime_name=runtime_name,
            task_id=task_id,
            title=current.title,
            session_id=runtime_session_id,
        )
        pause_path = resolved / PAUSE_FILE
        if pause_path.exists():
            pause_path.unlink()
        save_task_runtime_state(
            resolved,
            task_id,
            {
                "status": "interrupted",
                "completed_at": None,
                "exit_code": None,
                "session_id": runtime_session_id or None,
                "recovery_prompt": recovery_prompt,
                "recovery_reason": "stalled_runtime",
                "recovery_requested_at": utc_now_iso(),
                "stalled_recovery_count": int(runtime_state.get("stalled_recovery_count") or 0) + 1,
            },
        )
    return _run_resume_once(
        resolved,
        runtime=runtime_name,
        no_run=False,
        locked=False,
        spawn_watchdog_after=spawn_watchdog_after,
    )


def _command_runtime(args: argparse.Namespace, project_root: Path | None = None) -> str:
    return resolve_runtime(getattr(args, "runtime", None), project_root=project_root)


def _command_locked(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "_locked", False))


def _watchdog_enabled_for(project_root: Path | str) -> bool:
    metadata = load_project_metadata(project_root)
    return metadata.get("watchdog_enabled") is True


@contextlib.contextmanager
def _progress_monitor(project_root: Path) -> Any:
    stop_event = threading.Event()

    def worker() -> None:
        last_signature: tuple[str, str, str] | None = None
        while not stop_event.wait(30):
            active_records = sorted(load_active_task_records(project_root), key=lambda row: str(row.get("updated_at") or ""), reverse=True)
            active = active_records[0] if active_records else {}
            task_id = str(active.get("task_id") or "").strip() or "-"
            phase = str(active.get("phase") or "runtime").strip() or "runtime"
            event = latest_task_log_event(project_root) or {}
            message = str(event.get("message") or "still running").strip() or "still running"
            signature = (task_id, phase, message)
            if signature == last_signature:
                print(f"[app-delivery] {phase} {task_id}: still running", file=sys.stderr, flush=True)
            else:
                print(f"[app-delivery] {phase} {task_id}: {message}", file=sys.stderr, flush=True)
                last_signature = signature

    thread = threading.Thread(target=worker, name="app-delivery-progress", daemon=True)
    thread.start()
    try:
        yield None
    finally:
        stop_event.set()
        thread.join(timeout=1)


@contextlib.contextmanager
def _project_execution_guard(project_root: Path, *, already_locked: bool) -> Any:
    if already_locked:
        yield None
        return
    lock_path = project_root / ".app-delivery-runtime" / "locks" / "execution.lock"
    try:
        with acquire_lock(project_root, name="execution", timeout=0):
            yield lock_path
    except TimeoutError as exc:
        lock_details = read_lock_metadata(lock_path)
        raise DeliveryError(
            code="project_busy",
            message=f"another app-delivery run is already active for {project_root}",
            exit_code=2,
            details={"project": str(project_root), "lock_path": str(lock_path), **({"lock_owner": lock_details} if lock_details else {})},
            suggested_action="Wait for the active app-delivery run to finish, or stop the other process before retrying.",
        ) from exc


def cmd_render(args: argparse.Namespace) -> int:
    render_work_items_markdown(resolve_project_root(args.project))
    return 0


def _refresh_project_summary_best_effort(project_root: Path) -> None:
    try:
        write_project_structure_snapshot(project_root)
    except Exception as exc:  # pragma: no cover - defensive guard for machine-owned structure rendering
        print(f"[app-delivery] warning: project structure refresh failed: {exc}", file=sys.stderr)
    try:
        project_summary(project_root)
    except Exception as exc:  # pragma: no cover - defensive guard for machine-owned summary rendering
        print(f"[app-delivery] warning: project summary refresh failed: {exc}", file=sys.stderr)


def cmd_status(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    payload = routed_status(project_root)
    _refresh_project_summary_best_effort(project_root)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    print(json.dumps(project_summary(resolve_project_root(args.project)), indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    payload = doctor_report(resolve_runtime(getattr(args, "runtime", None)), framework_root=args.framework_root, opc_home=args.opc_home)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("status") == "ok" else 1


def cmd_preflight(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    payload = start_preflight(project_root, args.requirements, runtime=_command_runtime(args, project_root))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("status") == "ok" else 1


def cmd_init_project(args: argparse.Namespace) -> int:
    payload = initialize_project(
        resolve_project_root(args.project),
        description=args.description or "App-delivery project",
        runtime=resolve_runtime(args.runtime),
        stack=args.stack,
        force=args.force,
        framework_root=args.framework_root,
        watchdog_enabled=bool(getattr(args, "watchdog", True)),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _run_scaffold_once(project_root: Path | str, *, locked: bool = False) -> dict[str, Any]:
    resolved = resolve_project_root(project_root)
    with _project_execution_guard(resolved, already_locked=locked):
        return DeliveryLoop(resolved).run_builtin_scaffold()


def _run_loop_once(
    project_root: Path | str,
    *,
    runtime: str | None,
    max_auto_tasks: int | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    resolved = resolve_project_root(project_root)
    with _project_execution_guard(resolved, already_locked=locked):
        with _progress_monitor(resolved):
            return DeliveryLoop(resolved, runtime=resolve_runtime(runtime, project_root=resolved)).run(max_auto_tasks=max_auto_tasks)


def _run_verify_once(
    project_root: Path | str,
    *,
    runtime: str | None,
    mode: str = "all",
    locked: bool = False,
) -> dict[str, Any]:
    resolved = resolve_project_root(project_root)
    if mode == "all":
        tasks = all_tasks(resolved)
        incomplete_task_ids = [
            task.id
            for task in tasks
            if task.id != "T-FINAL" and task.task_kind != "repair" and task.status not in {"verified", "cancelled"}
        ]
        if incomplete_task_ids:
            raise DeliveryError(
                code="verify_project_incomplete",
                message="full verify is only allowed after all non-final tasks are verified",
                exit_code=2,
                details={"project": str(resolved), "incomplete_task_ids": incomplete_task_ids},
                suggested_action="Continue normal delivery or use `app-delivery verify --mode non-verified` for diagnostic coverage without mutating final verification state.",
            )
    with _project_execution_guard(resolved, already_locked=locked):
        return DeliveryLoop(resolved, runtime=resolve_runtime(runtime, project_root=resolved)).final_verify(suite_mode=mode)


def _run_resume_once(
    project_root: Path | str,
    *,
    runtime: str | None,
    no_run: bool = False,
    max_auto_tasks: int | None = None,
    locked: bool = False,
    spawn_watchdog_after: bool = True,
) -> dict[str, Any] | None:
    resolved = resolve_project_root(project_root)
    with _project_execution_guard(resolved, already_locked=locked):
        pause_path = resolved / PAUSE_FILE
        if pause_path.exists():
            pause_path.unlink()
        if no_run:
            if spawn_watchdog_after and _watchdog_enabled_for(resolved):
                spawn_watchdog(resolved)
            return None
        result = _run_loop_once(
            resolved,
            runtime=runtime,
            max_auto_tasks=max_auto_tasks,
            locked=True,
        )
    if spawn_watchdog_after and _watchdog_enabled_for(resolved):
        spawn_watchdog(resolved)
    return result


def _run_fix_once(
    project_root: Path | str,
    *,
    task_id: str,
    runtime: str | None,
    no_start: bool = False,
    spawn_watchdog_after: bool = True,
) -> dict[str, Any] | None:
    resolved = resolve_project_root(project_root)
    _terminate_runtime_for_task(resolved, task_id)
    with _project_execution_guard(resolved, already_locked=False):
        tasks = all_tasks(resolved)
        current = next((task for task in tasks if task.id == task_id), None)
        updated = reset_task(tasks, task_id)
        save_tasks(resolved, updated)
        if current and current.status_session_id:
            active_session = current_session(resolved)
            if active_session and active_session.id == current.status_session_id:
                retire_session(resolved, active_session)
        reapply_task_exception_patch(resolved, task_id)
        pause_path = resolved / PAUSE_FILE
        if pause_path.exists():
            pause_path.unlink()
    if no_start:
        if spawn_watchdog_after and _watchdog_enabled_for(resolved):
            spawn_watchdog(resolved)
        return None
    return _run_resume_once(
        resolved,
        runtime=runtime,
        no_run=False,
        locked=False,
        spawn_watchdog_after=spawn_watchdog_after,
    )


def _execute_framework_control_step(step: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    action = str(step.get("action") or "").strip()
    project_root = resolve_project_root(args.project)
    if action == "run_scaffold":
        return _run_scaffold_once(project_root)
    if action == "run_repair_task":
        task_id = str(step.get("task_id") or "").strip()
        if not task_id:
            raise DeliveryError(
                code="control_repair_task_missing",
                message="framework repair step is missing task_id",
                exit_code=2,
                details={"step": step},
            )
        return _run_fix_once(
            project_root,
            task_id=task_id,
            runtime=getattr(args, "runtime", None),
            no_start=False,
            spawn_watchdog_after=False,
        )
    if action == "run_resume":
        return _run_resume_once(
            project_root,
            runtime=getattr(args, "runtime", None),
            max_auto_tasks=getattr(args, "max_auto_tasks", None),
            spawn_watchdog_after=False,
        )
    if action == "run_final_verify":
        return _run_verify_once(project_root, runtime=getattr(args, "runtime", None), mode="all")
    if action == "recover_stalled":
        task_id = str(step.get("task_id") or "").strip()
        if not task_id:
            raise DeliveryError(
                code="control_recover_stalled_task_missing",
                message="stalled recovery step is missing task_id",
                exit_code=2,
                details={"step": step},
            )
        return _run_stalled_recovery_once(
            project_root,
            task_id=task_id,
            runtime=getattr(args, "runtime", None),
            runtime_attention={key: value for key, value in step.items() if key.startswith("attention_") or key in {"message"}},
            spawn_watchdog_after=False,
        )
    raise DeliveryError(
        code="control_step_unsupported",
        message=f"unsupported framework control action: {action}",
        exit_code=2,
        details={"step": step},
    )


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


def _review_input_matches_latest_runtime_attempt(project_root: Path, task_id: str, input_path: Path) -> bool:
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task_id))
    completed_at = _parse_iso_datetime(runtime_state.get("completed_at"))
    if completed_at is None:
        return False
    try:
        input_mtime = dt.datetime.fromtimestamp(input_path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return False
    return input_mtime > completed_at


def _host_step_autorun_enabled(project_root: Path) -> bool:
    env_value = str(os.environ.get("APP_DELIVERY_DEFER_HOST_STEPS") or "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return False
    metadata = load_project_metadata(project_root)
    configured = metadata.get("host_step_autorun")
    if isinstance(configured, bool):
        return configured
    return True


def _host_step_can_autorun(step: dict[str, Any], project_root: Path) -> bool:
    if not _host_step_autorun_enabled(project_root):
        return False
    if str(step.get("owner") or "").strip() != "host":
        return False
    if str(step.get("kind") or "").strip() != "host_skill":
        return False
    if not str(step.get("skill") or "").strip():
        return False
    if not str(step.get("prompt") or "").strip():
        return False
    if not str(step.get("expected_input_path") or "").strip():
        return False
    return True


def _run_host_skill_for_control_step(step: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    if not _host_step_can_autorun(step, project_root):
        return None
    skill = str(step.get("skill") or "").strip()
    prompt = str(step.get("prompt") or "").strip()
    hermes_bin = str(os.environ.get("APP_DELIVERY_HERMES_BIN") or "hermes").strip() or "hermes"
    hermes_path = hermes_bin if os.sep in hermes_bin else (shutil.which(hermes_bin) or "")
    if not hermes_path:
        raise DeliveryError(
            code="host_skill_runner_missing",
            message=f"unable to execute host skill {skill}: Hermes CLI was not found",
            exit_code=2,
            details={"skill": skill, "runner": hermes_bin, "project": str(project_root)},
            suggested_action="Install Hermes CLI or set APP_DELIVERY_HERMES_BIN to the executable path, or defer host steps explicitly with APP_DELIVERY_DEFER_HOST_STEPS=1.",
        )
    command = [hermes_path, "chat", "-Q", "--skills", skill, "--query", prompt]
    completed = subprocess.run(
        command,
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout or "").strip()
    result: dict[str, Any] = {
        "status": "executed",
        "skill": skill,
        "runner": hermes_path,
        "exit_code": completed.returncode,
    }
    if output:
        result["output"] = output[-4000:]
    if completed.returncode != 0:
        raise DeliveryError(
            code="host_skill_execution_failed",
            message=f"host skill execution failed for {skill}",
            exit_code=2,
            details={"skill": skill, "project": str(project_root), "command": command, **({"output": output[-4000:]} if output else {})},
            suggested_action="Inspect the host-skill output and rerun the control plane after fixing the review/planning skill failure.",
        )
    return result


def _execute_host_control_step_if_ready(step: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    expected_input = str(step.get("expected_input_path") or "").strip()
    if not expected_input:
        return None
    input_path_value = str(Path(expected_input).expanduser().resolve())
    input_path = Path(input_path_value)
    if not input_path.exists():
        return None

    skill = str(step.get("skill") or "").strip()
    task_id = str(step.get("task_id") or "").strip()
    if skill == "code-review" and task_id:
        request_path = code_review_request_path(project_root, task_id)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns and not _review_input_matches_latest_runtime_attempt(project_root, task_id, input_path):
            return None
    if skill == "final-review":
        request_path = final_review_request_path(project_root)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns:
            return None

    if skill == "code-review":
        if not task_id:
            raise DeliveryError(
                code="control_review_task_missing",
                message="host review step is missing task_id",
                exit_code=2,
                details={"step": step},
            )
        payload, loaded_input_path = load_stage_payload(project_root, "code-review", input_path_value, expected_type=dict)
        assert isinstance(payload, dict)
        exit_code = import_task_review(project_root, task_id, payload, loaded_input_path)
    elif skill == "final-review":
        payload, loaded_input_path = load_stage_payload(project_root, "final-review", input_path_value, expected_type=dict)
        assert isinstance(payload, dict)
        exit_code = import_final_review(project_root, payload, loaded_input_path)
    elif skill == "spec-review":
        payload, loaded_input_path = load_stage_payload(
            project_root,
            "spec-review",
            input_path_value,
            expected_type=dict,
            required_fields=["requirements", "acceptance_scenarios"],
        )
        assert isinstance(payload, dict)
        exit_code = import_spec_review(project_root, payload, loaded_input_path)
    elif skill == "arch-design":
        payload, loaded_input_path = load_stage_payload(
            project_root,
            "arch-design",
            input_path_value,
            expected_type=dict,
            required_fields=["architecture_md", "shared_components_md", "ui_required"],
        )
        assert isinstance(payload, dict)
        exit_code = import_arch_design(project_root, payload, loaded_input_path)
    elif skill == "ui-design":
        payload, loaded_input_path = load_stage_payload(
            project_root,
            "ui-design",
            input_path_value,
            expected_type=dict,
            required_fields=["template_selection_md", "design_system_md", "page_archetypes_md", "states_md"],
        )
        assert isinstance(payload, dict)
        exit_code = import_ui_design(project_root, payload, loaded_input_path)
    elif skill == "project-context-sync":
        payload, loaded_input_path = load_stage_payload(
            project_root,
            "project-context-sync",
            input_path_value,
            expected_type=dict,
            required_fields=["code_map_md", "test_plan"],
        )
        assert isinstance(payload, dict)
        exit_code = import_context_sync(project_root, payload, loaded_input_path)
    elif skill == "task-decompose":
        payload, loaded_input_path = load_stage_payload(
            project_root,
            "task-decompose",
            input_path_value,
            expected_type=dict,
            required_fields=["items", "validation_gates", "delivery_complexity"],
        )
        assert isinstance(payload, dict)
        exit_code = import_decompose(project_root, payload, loaded_input_path)
    else:
        return None

    result: dict[str, Any] = {
        "status": "imported",
        "skill": skill,
        "input_path": str(loaded_input_path),
        "exit_code": exit_code,
    }
    if task_id:
        result["task_id"] = task_id
    return result


def _execute_host_control_step(step: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    result = _execute_host_control_step_if_ready(step, project_root)
    if result is not None:
        return result
    host_execution = _run_host_skill_for_control_step(step, project_root)
    if host_execution is None:
        return None
    result = _execute_host_control_step_if_ready(step, project_root)
    if result is None:
        raise DeliveryError(
            code="host_skill_output_missing",
            message=f"host skill {step.get('skill')} completed but did not produce an importable artifact",
            exit_code=2,
            details={
                "project": str(project_root),
                "step": step,
                "host_execution": host_execution,
            },
            suggested_action="Check the host skill output and ensure it writes the canonical JSON artifact to expected_input_path.",
        )
    result["host_execution"] = host_execution
    return result


def cmd_control(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    goal = str(args.goal or "auto").strip().lower()

    if goal == "repair" and not str(getattr(args, "task_id", "") or "").strip():
        raise DeliveryError(
            code="repair_task_missing",
            message="control goal=repair requires --task-id",
            exit_code=2,
        )

    executed_steps: list[dict[str, Any]] = []

    if goal == "repair":
        repair_result = _run_fix_once(
            project_root,
            task_id=str(args.task_id).strip(),
            runtime=getattr(args, "runtime", None),
            no_start=True,
            spawn_watchdog_after=False,
        )
        executed_steps.append({
            "owner": "framework",
            "action": "repair_task",
            "task_id": str(args.task_id).strip(),
            "result": repair_result,
        })
        goal = "auto"

    snapshot = run_control(
        project_root,
        goal=goal,
        requirements_path=getattr(args, "requirements", None),
        repair_task_id=getattr(args, "task_id", None),
    )
    executed_steps.extend(snapshot.pop("executed_steps", []))

    safety = 0
    while goal == "auto" and snapshot.get("must_continue"):
        safety += 1
        if safety > int(getattr(args, "max_control_steps", CONTROL_STEP_LIMIT) or CONTROL_STEP_LIMIT):
            raise DeliveryError(
                code="control_step_limit_exceeded",
                message="control plane exceeded the maximum allowed deterministic continuation steps",
                exit_code=2,
                details={"snapshot": snapshot, "executed_steps": executed_steps},
            )
        next_step = snapshot.get("next_step")
        if not isinstance(next_step, dict):
            break
        owner = str(next_step.get("owner") or "framework").strip()
        action = str(next_step.get("action") or "").strip()
        if owner == "host":
            result = _execute_host_control_step(next_step, project_root)
            if result is None:
                executed_steps.append({
                    "owner": owner,
                    "action": action,
                    "deferred_to_host": True,
                })
                break
            executed_steps.append({
                "owner": owner,
                "action": action,
                "result": result,
            })
        else:
            result = _execute_framework_control_step(next_step, args)
            executed_steps.append({
                "owner": owner,
                "action": action,
                "result": result,
            })
        snapshot = run_control(
            project_root,
            goal="auto",
            requirements_path=getattr(args, "requirements", None),
        )
        executed_steps.extend(snapshot.pop("executed_steps", []))

    if executed_steps:
        snapshot["executed_steps"] = executed_steps

    _refresh_project_summary_best_effort(project_root)

    if goal in {"auto", "repair"} and _watchdog_enabled_for(project_root):
        control_status = str(snapshot.get("control_status") or "").strip()
        if control_status in {"in_progress", "running"}:
            spawn_watchdog(project_root)

    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return 0 if str(snapshot.get("control_status") or "") not in {"blocked"} else 1


def cmd_scaffold(args: argparse.Namespace) -> int:
    _run_scaffold_once(args.project, locked=_command_locked(args))
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    if args.status:
        return cmd_status(args)
    result = _run_loop_once(
        args.project,
        runtime=getattr(args, "runtime", None),
        max_auto_tasks=getattr(args, "max_auto_tasks", None),
        locked=_command_locked(args),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = _run_verify_once(
        args.project,
        runtime=getattr(args, "runtime", None),
        mode=args.mode,
        locked=_command_locked(args),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("passed") else 1


def cmd_code_review(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "code-review", args.input, expected_type=dict)
        assert isinstance(payload, dict)
        return import_task_review(project_root, args.task_id, payload, input_path)


def cmd_final_review(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "final-review", args.input, expected_type=dict)
        assert isinstance(payload, dict)
        return import_final_review(project_root, payload, input_path)


def cmd_spec_review(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "spec-review", args.input, expected_type=dict, required_fields=["requirements", "acceptance_scenarios"])
        assert isinstance(payload, dict)
        return import_spec_review(project_root, payload, input_path)


def cmd_arch_design(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "arch-design", args.input, expected_type=dict, required_fields=["architecture_md", "shared_components_md", "ui_required"])
        assert isinstance(payload, dict)
        return import_arch_design(project_root, payload, input_path)


def cmd_ui_design(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "ui-design", args.input, expected_type=dict, required_fields=["template_selection_md", "design_system_md", "page_archetypes_md", "states_md"])
        assert isinstance(payload, dict)
        return import_ui_design(project_root, payload, input_path)


def cmd_context_sync(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "project-context-sync", args.input, expected_type=dict, required_fields=["code_map_md", "test_plan"])
        assert isinstance(payload, dict)
        return import_context_sync(project_root, payload, input_path)


def cmd_decompose(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(
            project_root,
            "task-decompose",
            args.input,
            expected_type=dict,
            required_fields=["items", "delivery_complexity", "validation_gates"],
        )
        assert isinstance(payload, dict)
        return import_decompose(project_root, payload, input_path)


def cmd_start(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        project_root.mkdir(parents=True, exist_ok=True)
        docs_dir = project_root / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        runtime_name = _command_runtime(args, project_root)
        if args.requirements:
            archive_requirements_source(project_root, args.requirements)
        requirements_source = readiness.discover_requirements_source(project_root, explicit_path=args.requirements)
        if not readiness.requirements_ready(project_root):
            raise stage_missing_error(
                "spec-review",
                project_root,
                requirements_path=str(requirements_source) if requirements_source is not None else args.requirements,
            )
        if readiness.blocking_clarifications_present(project_root):
            raise DeliveryError(
                code="clarification_blocking",
                message=f"blocking clarifications remain unresolved: {project_root / 'docs' / 'clarification-needed.md'}",
                exit_code=2,
                details={
                    "project": str(project_root),
                    "clarification_path": str(project_root / 'docs' / 'clarification-needed.md'),
                    "expected_input_path": str(stage_input_path(project_root, "spec-review")),
                    "import_command": stage_import_command(project_root, "spec-review"),
                },
                suggested_action="Regenerate spec-review so the blocking clarification is resolved inside the canonical spec-review output, then rerun start.",
            )
        if not readiness.architecture_ready(project_root):
            raise stage_missing_error("arch-design", project_root)
        if readiness.ui_required(project_root) and not readiness.ui_ready(project_root):
            raise stage_missing_error("ui-design", project_root)
        if not readiness.context_ready(project_root):
            raise stage_missing_error("project-context-sync", project_root)
        if not readiness.work_items_ready(project_root):
            raise stage_missing_error("task-decompose", project_root)
        contract_errors = readiness.work_item_contract_errors(project_root)
        if contract_errors:
            raise DeliveryError(
                code="work_items_contract_invalid",
                message="one or more work items have invalid task contracts",
                exit_code=2,
                details={
                    "errors": contract_errors,
                    "expected_input_path": str(stage_input_path(project_root, "task-decompose")),
                    "import_command": stage_import_command(project_root, "task-decompose"),
                },
                suggested_action="Rerun task-decompose in the host skill layer, regenerate the canonical work-items JSON, and re-import it before starting the delivery loop.",
            )
        if readiness.needs_scaffold(project_root):
            cmd_scaffold(argparse.Namespace(project=str(project_root), _locked=True))
        result = cmd_loop(argparse.Namespace(project=str(project_root), runtime=runtime_name, status=False, max_auto_tasks=getattr(args, "max_auto_tasks", None), _locked=True))
    if _watchdog_enabled_for(project_root):
        spawn_watchdog(project_root)
    return result


def cmd_pause(args: argparse.Namespace) -> int:
    return cmd_control(
        argparse.Namespace(
            project=str(resolve_project_root(args.project)),
            goal="pause",
            requirements=None,
            task_id=None,
            runtime=getattr(args, "runtime", None),
            max_auto_tasks=None,
            max_control_steps=CONTROL_STEP_LIMIT,
        )
    )


def cmd_resume(args: argparse.Namespace) -> int:
    result = _run_resume_once(
        args.project,
        runtime=getattr(args, "runtime", None),
        no_run=getattr(args, "no_run", False),
        locked=_command_locked(args),
        spawn_watchdog_after=True,
    )
    if result is None:
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_watchdog_run(args: argparse.Namespace) -> int:
    import time
    project_root = resolve_project_root(args.project)
    interval_seconds = max(5, int(getattr(args, "interval_seconds", 30) or 30))
    save_watchdog_state(
        project_root,
        {
            "pid": os.getpid(),
            "project": str(project_root),
            "status": "running",
            "started_at": utc_now_iso(),
            "last_action": "started",
        },
    )
    while True:
        payload = routed_status(project_root)
        control_status = str(payload.get("control_status") or "").strip()
        active_task = payload.get("active_task") if isinstance(payload.get("active_task"), dict) else None
        runtime_attention = payload.get("runtime_attention") if isinstance(payload.get("runtime_attention"), dict) else None
        if control_status == "running" and active_task is not None and runtime_attention and runtime_attention.get("suspected"):
            task_id = str(active_task.get("id") or "").strip()
            if task_id:
                save_watchdog_state(
                    project_root,
                    {
                        "pid": os.getpid(),
                        "project": str(project_root),
                        "status": "running",
                        "last_action": "recover_stalled",
                        "task_id": task_id,
                        "attention_kind": runtime_attention.get("kind"),
                    },
                )
                _run_stalled_recovery_once(
                    project_root,
                    task_id=task_id,
                    runtime=None,
                    runtime_attention=runtime_attention,
                    spawn_watchdog_after=False,
                )
                continue
        if control_status == "paused":
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "paused", "last_action": "paused"})
            return 0
        if control_status == "complete":
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "done", "last_action": "complete"})
            return 0
        if control_status == "blocked":
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "blocked", "last_action": "blocked"})
            return 0
        if payload.get("must_continue"):
            next_step = payload.get("next_step") if isinstance(payload.get("next_step"), dict) else {}
            if str(next_step.get("owner") or "").strip() == "host" and not _host_step_can_autorun(next_step, project_root):
                save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "blocked", "last_action": "host_step_required"})
                return 0
            last_action = "host_step" if str(next_step.get("owner") or "").strip() == "host" else "resume"
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "running", "last_action": last_action})
            cmd_control(
                argparse.Namespace(
                    project=str(project_root),
                    goal="auto",
                    requirements=None,
                    task_id=None,
                    runtime=None,
                    max_auto_tasks=None,
                    max_control_steps=CONTROL_STEP_LIMIT,
                )
            )
            continue
        save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "running", "last_action": "idle"})
        time.sleep(interval_seconds)


def cmd_fix(args: argparse.Namespace) -> int:
    result = _run_fix_once(
        args.project,
        task_id=args.task_id,
        runtime=getattr(args, "runtime", None),
        no_start=getattr(args, "no_start", False),
        spawn_watchdog_after=True,
    )
    if result is None:
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_recover_stalled(args: argparse.Namespace) -> int:
    result = _run_stalled_recovery_once(
        args.project,
        task_id=args.task_id,
        runtime=getattr(args, "runtime", None),
        runtime_attention=None,
        spawn_watchdog_after=True,
    )
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m delivery")
    subparsers = parser.add_subparsers(dest="command", required=True)

    spec_review = subparsers.add_parser("spec-review")
    spec_review.add_argument("--project", required=True)
    spec_review.add_argument("--input", required=True)
    spec_review.set_defaults(func=cmd_spec_review)

    arch_design = subparsers.add_parser("arch-design")
    arch_design.add_argument("--project", required=True)
    arch_design.add_argument("--input", required=True)
    arch_design.set_defaults(func=cmd_arch_design)

    ui_design = subparsers.add_parser("ui-design")
    ui_design.add_argument("--project", required=True)
    ui_design.add_argument("--input", required=True)
    ui_design.set_defaults(func=cmd_ui_design)

    context_sync = subparsers.add_parser("context-sync")
    context_sync.add_argument("--project", required=True)
    context_sync.add_argument("--input", required=True)
    context_sync.set_defaults(func=cmd_context_sync)

    decompose = subparsers.add_parser("decompose")
    decompose.add_argument("--project", required=True)
    decompose.add_argument("--input", required=True)
    decompose.set_defaults(func=cmd_decompose)

    scaffold = subparsers.add_parser("scaffold")
    scaffold.add_argument("--project", required=True)
    scaffold.set_defaults(func=cmd_scaffold)

    loop = subparsers.add_parser("loop")
    loop.add_argument("--project", required=True)
    loop.add_argument("--runtime")
    loop.add_argument("--status", action="store_true")
    loop.add_argument("--max-auto-tasks", type=int)
    loop.set_defaults(func=cmd_loop)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--project", required=True)
    verify.add_argument("--runtime")
    verify.add_argument("--mode", choices=["all", "non-verified"], default="all")
    verify.set_defaults(func=cmd_verify)

    code_review = subparsers.add_parser("code-review")
    code_review.add_argument("--project", required=True)
    code_review.add_argument("--task-id", required=True)
    code_review.add_argument("--input", required=True)
    code_review.set_defaults(func=cmd_code_review)

    final_review = subparsers.add_parser("final-review")
    final_review.add_argument("--project", required=True)
    final_review.add_argument("--input", required=True)
    final_review.set_defaults(func=cmd_final_review)

    render = subparsers.add_parser("render")
    render.add_argument("--project", required=True)
    render.set_defaults(func=cmd_render)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--project", required=True)
    summary.set_defaults(func=cmd_summary)

    control = subparsers.add_parser("control")
    control.add_argument("--project", required=True)
    control.add_argument("--goal", choices=["auto", "status", "pause", "repair"], default="auto")
    control.add_argument("--requirements")
    control.add_argument("--task-id")
    control.add_argument("--runtime")
    control.add_argument("--max-auto-tasks", type=int)
    control.add_argument("--max-control-steps", type=int, default=CONTROL_STEP_LIMIT)
    control.set_defaults(func=cmd_control)

    start = subparsers.add_parser("start")
    start.add_argument("--project", required=True)
    start.add_argument("--requirements")
    start.add_argument("--runtime")
    start.add_argument("--max-auto-tasks", type=int)
    start.set_defaults(func=cmd_start)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--runtime")
    doctor.add_argument("--framework-root")
    doctor.add_argument("--opc-home")
    doctor.set_defaults(func=cmd_doctor)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--project", required=True)
    preflight.add_argument("--requirements", required=True)
    preflight.add_argument("--runtime")
    preflight.set_defaults(func=cmd_preflight)

    init_project = subparsers.add_parser("init-project")
    init_project.add_argument("--project", required=True)
    init_project.add_argument("--description", default="")
    init_project.add_argument("--runtime", required=True)
    init_project.add_argument("--stack", default="python-react")
    init_project.add_argument("--framework-root")
    init_project.add_argument("--force", action="store_true")
    init_project.add_argument("--watchdog", action=argparse.BooleanOptionalAction, default=True)
    init_project.set_defaults(func=cmd_init_project)

    pause = subparsers.add_parser("pause")
    pause.add_argument("--project", required=True)
    pause.set_defaults(func=cmd_pause)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--project", required=True)
    resume.add_argument("--runtime")
    resume.add_argument("--no-run", action="store_true")
    resume.set_defaults(func=cmd_resume)

    fix = subparsers.add_parser("fix")
    fix.add_argument("--project", required=True)
    fix.add_argument("--task-id", required=True)
    fix.add_argument("--runtime")
    fix.add_argument("--no-start", action="store_true")
    fix.set_defaults(func=cmd_fix)

    recover_stalled = subparsers.add_parser("recover-stalled")
    recover_stalled.add_argument("--project", required=True)
    recover_stalled.add_argument("--task-id", required=True)
    recover_stalled.add_argument("--runtime")
    recover_stalled.set_defaults(func=cmd_recover_stalled)

    status = subparsers.add_parser("status")
    status.add_argument("--project", required=True)
    status.set_defaults(func=cmd_status)

    watchdog_run = subparsers.add_parser("watchdog-run")
    watchdog_run.add_argument("--project", required=True)
    watchdog_run.add_argument("--interval-seconds", type=int, default=30)
    watchdog_run.set_defaults(func=cmd_watchdog_run)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        payload, exit_code = error_payload(exc)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())