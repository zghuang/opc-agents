from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .bootstrap import archive_requirements_source, doctor_report, initialize_project, start_preflight
from .control_plane import mark_auto_exception_repair_attempted, run_control, routed_status
from .errors import DeliveryError, error_payload
from .gates import accept_gate
from .loop import DeliveryLoop, PAUSE_FILE, project_summary
from .loop_task_prompt import build_stalled_recovery_prompt
from .loop_review import import_final_review, import_task_review
from . import project_readiness as readiness
from .production_semantics import scan_production_semantics
from .review_artifacts import code_review_request_path, final_review_request_path
from .runtime_config import load_project_metadata, resolve_project_root, resolve_runtime
from .scaffold import write_project_structure_snapshot
from .session import RuntimeSession, current_session, retire_session, save_current_session
from .watchdog import process_alive, save_watchdog_state, should_watchdog_resume, spawn_watchdog
from .stage_harness import import_arch_design, import_context_sync, import_decompose, import_spec_review, import_ui_design, load_stage_payload, stage_import_command, stage_input_path, stage_missing_error
from .loop_gitops import try_reapply_task_exception_patch
from .state import (
    acquire_lock,
    latest_task_log_event,
    lock_file_is_locked,
    load_active_task_records,
    load_task_runtime_state,
    load_json,
    normalize_task_runtime_state,
    read_lock_metadata,
    render_work_items_markdown,
    save_task_runtime_state,
    task_runtime_state_path,
    utc_now_iso,
    write_json,
)
from .task import FINAL_VERIFY_TASK_ID, all_tasks, mark_task, reset_task, save_tasks


CONTROL_STEP_LIMIT = 128
EXECUTION_LOCK_TIMEOUT_SECONDS = 5.0
HOST_HANDOFF_FILE = Path(".app-delivery-runtime") / "host-handoff.json"
HOST_HANDOFF_RETRY_AFTER_SECONDS = 60


def _runtime_state_for_exception_patch_reapply(reapply_result: dict[str, Any]) -> dict[str, Any]:
    status = str(reapply_result.get("status") or "").strip()
    state: dict[str, Any] = {
        "exception_patch_reapply": reapply_result,
        "exception_patch_conflict": None,
    }
    if status == "conflict":
        state.update(
            {
                "force_task_prompt": True,
                "force_task_prompt_reason": "exception_patch_conflict",
                "exception_patch_conflict": reapply_result,
            }
        )
    return state
HOST_HANDOFF_MAX_RETRY_ATTEMPTS = 1
HOST_FALLBACK_FILE = Path(".app-delivery-runtime") / "host-fallback.json"
HOST_FALLBACK_LOCK_FILE = Path(".app-delivery-runtime") / "host-fallback-lock.json"
REVIEW_RUNNER_DEFAULT_AFTER_SECONDS = 0
REVIEW_RUNNER_DEFAULT_MAX_ATTEMPTS = 3
REVIEW_RUNNER_DEFAULT_IDLE_TIMEOUT_SECONDS = 600
HOST_FALLBACK_DEFAULT_MAX_ATTEMPTS = 1
FINAL_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"
HANDOFF_REVIEW_RUNNING_STATUS = "review_running"
HANDOFF_REVIEW_RUNNER_FAILED_STATUS = "review_runner_failed"


def _command_runtime(args: argparse.Namespace, project_root: Path | None = None) -> str:
    return resolve_runtime(getattr(args, "runtime", None), project_root=project_root)


def _command_locked(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "_locked", False))


def _watchdog_enabled_for(project_root: Path | str) -> bool:
    metadata = load_project_metadata(project_root)
    return metadata.get("watchdog_enabled") is True


def _host_step_expects_importable_artifact(step: dict[str, Any]) -> bool:
    return bool(str(step.get("skill") or "").strip() and str(step.get("expected_input_path") or "").strip())


def _snapshot_needs_watchdog(snapshot: dict[str, Any]) -> bool:
    control_status = str(snapshot.get("control_status") or "").strip()
    if control_status == "running":
        return True
    if control_status != "in_progress" or not snapshot.get("must_continue"):
        return False
    next_step = snapshot.get("next_step") if isinstance(snapshot.get("next_step"), dict) else {}
    owner = str(next_step.get("owner") or "framework").strip()
    if owner == "host":
        return _host_step_expects_importable_artifact(next_step)
    return owner == "framework"


def _host_fallback_enabled_for(project_root: Path | str) -> bool:
    metadata = load_project_metadata(project_root)
    if "host_fallback_enabled" in metadata:
        return metadata.get("host_fallback_enabled") is True
    return metadata.get("watchdog_enabled") is True


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int_config(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _host_fallback_after_seconds(project_root: Path | str) -> int:
    metadata = load_project_metadata(project_root)
    mode = str(metadata.get("review_runner_mode") or "framework").strip().casefold()
    if mode in {"deferred", "legacy", "legacy_handoff"}:
        return _positive_int(metadata.get("host_fallback_after_seconds"), 600)
    return _nonnegative_int_config(metadata.get("review_runner_after_seconds"), REVIEW_RUNNER_DEFAULT_AFTER_SECONDS)


def _host_fallback_max_attempts(project_root: Path | str) -> int:
    metadata = load_project_metadata(project_root)
    mode = str(metadata.get("review_runner_mode") or "framework").strip().casefold()
    if mode not in {"deferred", "legacy", "legacy_handoff"}:
        return _positive_int(metadata.get("review_runner_max_attempts"), REVIEW_RUNNER_DEFAULT_MAX_ATTEMPTS)
    return _positive_int(metadata.get("host_fallback_max_attempts"), HOST_FALLBACK_DEFAULT_MAX_ATTEMPTS)


def _review_runner_idle_timeout_seconds(project_root: Path | str) -> int:
    metadata = load_project_metadata(project_root)
    return _positive_int(metadata.get("review_runner_idle_timeout_seconds"), REVIEW_RUNNER_DEFAULT_IDLE_TIMEOUT_SECONDS)


def _is_final_repair_task(task: Any) -> bool:
    title = str(getattr(task, "title", "") or "").strip()
    task_kind = str(getattr(task, "task_kind", "") or "").strip()
    return task_kind == "repair" and (
        title.startswith(FINAL_REPAIR_TASK_PREFIX)
        or title.startswith(FINAL_REVIEW_REPAIR_TASK_PREFIX)
    )


def _guard_final_repair_limit_not_reopened(project_root: Path, task: Any | None) -> None:
    if task is None or not _is_final_repair_task(task):
        return
    final_runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID))
    if not bool(final_runtime_state.get("final_repair_limit_reached")):
        return
    raise DeliveryError(
        code="final_repair_limit_reached",
        message=(
            f"final verification has reached the maximum repair iterations; refusing to reopen final repair task {task.id}. "
            "Resolve remaining failures through explicit manual escalation instead of ordinary fix/reset-repair."
        ),
        exit_code=2,
        details={
            "task_id": task.id,
            "task_title": getattr(task, "title", ""),
            "final_repair_limit_reached": True,
            "current_final_repair_task_id": str(final_runtime_state.get("repair_task_id") or "").strip() or None,
        },
    )


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
        timeout = float(os.environ.get("APP_DELIVERY_EXECUTION_LOCK_TIMEOUT_SECONDS") or EXECUTION_LOCK_TIMEOUT_SECONDS)
    except ValueError:
        timeout = EXECUTION_LOCK_TIMEOUT_SECONDS
    _prune_stale_execution_lock(project_root)
    if timeout <= 0 and lock_file_is_locked(lock_path):
        raise DeliveryError(
            code="project_busy",
            message=f"another app-delivery run is already active for {project_root}",
            exit_code=2,
            details={"project": str(project_root), "lock_path": str(lock_path), "lock_owner": read_lock_metadata(lock_path)},
            suggested_action="Wait for the active app-delivery run to finish, or stop the other process before retrying.",
        )
    try:
        with acquire_lock(project_root, name="execution", timeout=max(0.0, timeout)):
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
    except Exception as exc:
        print(f"[app-delivery] warning: project structure refresh failed: {exc}", file=sys.stderr)
    try:
        project_summary(project_root)
    except Exception as exc:
        print(f"[app-delivery] warning: project summary refresh failed: {exc}", file=sys.stderr)


def cmd_status(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    payload = routed_status(project_root)
    _refresh_project_summary_best_effort(project_root)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _watch_status_record_kind(task: Any, *, kind: str, task_status: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "task_id": str(getattr(task, "id", "") or "").strip() or None,
        "task_title": str(getattr(task, "title", "") or "").strip() or None,
        "task_status": task_status,
    }
    if extra:
        payload.update(extra)
    return payload


def _watch_status_snapshot(project_root: Path) -> dict[str, Any]:
    tasks = all_tasks(project_root)
    task_by_id = {task.id: task for task in tasks}
    active_records = sorted(
        [row for row in load_active_task_records(project_root) if isinstance(row, dict)],
        key=lambda row: str(row.get("updated_at") or ""),
        reverse=True,
    )
    live_active_task_ids = {str(row.get("task_id") or "").strip() for row in active_records if str(row.get("task_id") or "").strip()}
    exception_task_ids = [task.id for task in tasks if task.status == "exception"]
    stale_active_ledger_task_ids = [task.id for task in tasks if task.status == "active" and task.id not in live_active_task_ids]

    current: dict[str, Any]
    lifecycle = "idle"

    if (project_root / PAUSE_FILE).exists():
        lifecycle = "paused"
        current = {"kind": "none", "task_id": None, "task_title": None, "task_status": None}
    elif active_records:
        record = active_records[0]
        task_id = str(record.get("task_id") or "").strip()
        task = task_by_id.get(task_id)
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task_id)) if task_id else {}
        runtime_status = str(runtime_state.get("status") or record.get("phase") or (getattr(task, "status", "") if task is not None else "active")).strip() or "active"
        lifecycle = "active"
        if task is not None:
            current = _watch_status_record_kind(task, kind="active", task_status=runtime_status)
        else:
            current = {
                "kind": "active",
                "task_id": task_id or None,
                "task_title": str(record.get("task_title") or "").strip() or None,
                "task_status": runtime_status,
            }
    else:
        review_pending = next((task for task in tasks if task.status == "review_pending"), None)
        blocked = next((task for task in tasks if task.status in {"blocked", "exception"}), None)
        pending = next(
            (
                task
                for task in tasks
                if task.id != FINAL_VERIFY_TASK_ID and task.status == "pending" and getattr(task, "task_kind", "feature") != "repair"
            ),
            None,
        )
        actionable = [
            task
            for task in tasks
            if task.id != FINAL_VERIFY_TASK_ID and task.status != "cancelled" and getattr(task, "task_kind", "feature") != "repair"
        ]
        if review_pending is not None:
            lifecycle = "review_pending"
            current = _watch_status_record_kind(review_pending, kind="review_pending", task_status="review_pending")
        elif blocked is not None:
            lifecycle = "blocked"
            current = _watch_status_record_kind(blocked, kind="blocked", task_status=blocked.status)
        elif pending is not None:
            lifecycle = "pending"
            current = _watch_status_record_kind(pending, kind="next", task_status="pending")
        elif actionable and all(task.status == "verified" for task in actionable):
            lifecycle = "complete"
            current = {"kind": "none", "task_id": None, "task_title": None, "task_status": None}
        else:
            current = {"kind": "none", "task_id": None, "task_title": None, "task_status": None}

    handoff = load_json(project_root / HOST_HANDOFF_FILE, {})
    handoff_payload = None
    if isinstance(handoff, dict):
        handoff_status = str(handoff.get("status") or "").strip()
        handoff_skill = str(handoff.get("skill") or "").strip()
        handoff_task_id = str(handoff.get("task_id") or "").strip()
        if handoff_status or handoff_skill or handoff_task_id:
            handoff_payload = {
                "status": handoff_status or None,
                "skill": handoff_skill or None,
                "task_id": handoff_task_id or None,
                "review_runner_status": str(handoff.get("review_runner_status") or "").strip() or None,
                "review_runner_reason": str(handoff.get("review_runner_reason") or "").strip() or None,
                "runner_pid": handoff.get("runner_pid"),
            }

    return {
        "project": str(project_root),
        "control_status": lifecycle,
        "current": current,
        "exception_task_ids": exception_task_ids,
        "stale_active_ledger_task_ids": stale_active_ledger_task_ids,
        **({"host_handoff": handoff_payload} if handoff_payload is not None else {}),
    }


def _watch_status_event(
    *,
    checked_at: str,
    snapshot: dict[str, Any],
    reason: str,
    error: str | None = None,
) -> dict[str, Any]:
    current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
    handoff = snapshot.get("host_handoff") if isinstance(snapshot.get("host_handoff"), dict) else {}
    task_kind = str(current.get("kind") or "").strip() or None
    if task_kind == "none":
        task_kind = None
    event = {
        "reason": reason,
        "checked_at": checked_at,
        "project": Path(str(snapshot.get("project") or "")).name or str(snapshot.get("project") or ""),
        "control_status": str(snapshot.get("control_status") or "").strip() or None,
        "task_kind": task_kind,
        "task_id": str(current.get("task_id") or "").strip() or None,
        "task_title": str(current.get("task_title") or "").strip() or None,
        "task_status": str(current.get("task_status") or "").strip() or None,
        "exception_task_ids": snapshot.get("exception_task_ids") if isinstance(snapshot.get("exception_task_ids"), list) else [],
        "stale_active_ledger_task_ids": snapshot.get("stale_active_ledger_task_ids") if isinstance(snapshot.get("stale_active_ledger_task_ids"), list) else [],
    }
    if handoff:
        event["host_status"] = str(handoff.get("status") or "").strip() or None
        event["host_skill"] = str(handoff.get("skill") or "").strip() or None
        event["host_task_id"] = str(handoff.get("task_id") or "").strip() or None
        event["review_runner_status"] = str(handoff.get("review_runner_status") or "").strip() or None
        event["review_runner_reason"] = str(handoff.get("review_runner_reason") or "").strip() or None
        event["review_runner_pid"] = handoff.get("runner_pid")
    if error:
        compact_error = " ".join(str(error).split())
        event["error"] = compact_error[:240]
    return event


def _watch_status_emit(event: dict[str, Any]) -> None:
    print("APP_DELIVERY_STATUS_CHANGE " + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)


def cmd_watch_status(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    interval_seconds = max(5, int(getattr(args, "interval_seconds", 30) or 30))
    max_failures = max(1, int(getattr(args, "max_failures", 3) or 3))
    previous_snapshot: dict[str, Any] | None = None
    previous_signature = ""
    failures = 0

    while True:
        try:
            snapshot = _watch_status_snapshot(project_root)
            signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            checked_at = utc_now_iso()
            if previous_snapshot is None:
                _watch_status_emit(_watch_status_event(checked_at=checked_at, snapshot=snapshot, reason="initial"))
            elif signature != previous_signature:
                _watch_status_emit(_watch_status_event(checked_at=checked_at, snapshot=snapshot, reason="changed"))
            previous_snapshot = snapshot
            previous_signature = signature
            failures = 0
            if snapshot["control_status"] in {"complete", "paused"}:
                return 0
        except Exception as exc:
            failures += 1
            if failures >= max_failures:
                _watch_status_emit(
                    _watch_status_event(
                        checked_at=utc_now_iso(),
                        snapshot={
                            "project": str(project_root),
                            "control_status": "watch_error",
                            "current": {"kind": "error", "task_id": None, "task_title": None, "task_status": None},
                        },
                        reason="watch_error",
                        error=str(exc),
                    )
                )
                return 1
        time.sleep(interval_seconds)


def cmd_summary(args: argparse.Namespace) -> int:
    print(json.dumps(project_summary(resolve_project_root(args.project)), indent=2, ensure_ascii=False))
    return 0


def _claim_blockers(project_root: Path, status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    counts = status_payload.get("counts") if isinstance(status_payload.get("counts"), dict) else {}
    for status_name, count in sorted(counts.items()):
        if status_name != "verified" and count:
            blockers.append({"kind": "task_status", "status": status_name, "count": count})
    if isinstance(status_payload.get("active_task"), dict):
        blockers.append({"kind": "active_task", "task_id": status_payload["active_task"].get("id")})
    if isinstance(status_payload.get("review_pending_task"), dict):
        blockers.append({"kind": "review_pending_task", "task_id": status_payload["review_pending_task"].get("id")})
    if isinstance(status_payload.get("next_task"), dict):
        blockers.append({"kind": "next_task", "task_id": status_payload["next_task"].get("id"), "status": status_payload["next_task"].get("status")})
    final_verify = status_payload.get("final_verify") if isinstance(status_payload.get("final_verify"), dict) else {}
    if final_verify and str(final_verify.get("status") or "").strip() not in {"", "pass", "verified"}:
        blockers.append({"kind": "final_verify", "status": final_verify.get("status"), "blocked_reason": final_verify.get("blocked_reason"), "repair_task_id": final_verify.get("repair_task_id"), "will_continue": final_verify.get("will_continue")})
    semantic_findings = scan_production_semantics(project_root)
    if semantic_findings:
        blockers.append({"kind": "production_semantics", "count": len(semantic_findings), "findings": [finding.__dict__ for finding in semantic_findings[:20]]})
    return blockers


def cmd_claim(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    status_payload = routed_status(project_root)
    blockers = _claim_blockers(project_root, status_payload)
    checked_at = utc_now_iso()
    claim_allowed = bool(status_payload.get("delivery_claim_allowed")) and not blockers
    payload = {
        "schema_version": "1",
        "status": "claimed" if claim_allowed else "blocked",
        "project": str(project_root),
        "checked_at": checked_at,
        "claimed_at": checked_at if claim_allowed else None,
        "delivery_claim_allowed": claim_allowed,
        "control_status": status_payload.get("control_status"),
        "blockers": blockers,
    }
    if claim_allowed:
        docs_dir = project_root / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "delivery-claim.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (docs_dir / "delivery-claim.md").write_text(
            "\n".join([
                "# Delivery Claim",
                "",
                "status: claimed",
                f"claimed_at: {checked_at}",
                f"project: {project_root}",
                "",
                "## Evidence",
                "",
                "- Task ledger reports delivery claim allowed.",
                "- No unwaived production semantic findings were found.",
            ]) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if claim_allowed else 1


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
    with _project_execution_guard(resolved, already_locked=False):
        tasks = all_tasks(resolved)
        current = next((task for task in tasks if task.id == task_id), None)
        if current is not None and current.status == "verified" and current.task_kind != "repair":
            raise DeliveryError(
                code="verified_task_repair_forbidden",
                message=(
                    f"task {task_id} is already verified; do not reopen verified feature tasks. "
                    "Repair regressions from the current task or a dedicated repair bundle instead."
                ),
                exit_code=2,
                details={"task_id": task_id, "status": current.status, "task_kind": current.task_kind},
            )
        _guard_final_repair_limit_not_reopened(resolved, current)
        _terminate_runtime_for_task(resolved, task_id)
        updated = reset_task(tasks, task_id)
        save_tasks(resolved, updated)
        if current and current.status_session_id:
            active_session = current_session(resolved)
            if active_session and active_session.id == current.status_session_id:
                retire_session(resolved, active_session)
        reapply_result = try_reapply_task_exception_patch(resolved, task_id)
        runtime_path = task_runtime_state_path(resolved, task_id)
        if runtime_path.exists():
            runtime_path.unlink()
        save_task_runtime_state(
            resolved,
            task_id,
            {
                "force_task_prompt": True,
                "force_task_prompt_reason": "manual_fix",
                **_runtime_state_for_exception_patch_reapply(reapply_result),
            },
        )
        prompt_path = resolved / ".app-delivery-runtime" / "prompts" / f"{task_id}.md"
        if prompt_path.exists():
            prompt_path.unlink()
        review_path = resolved / "docs" / "reviews" / f"code-review-{task_id}.md"
        if review_path.exists():
            review_path.unlink()
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


def _pid_is_running(pid: int) -> bool:
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


def _terminate_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
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
    if pid == os.getpid():
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)


def _terminate_runtime_for_task(project_root: Path | str, task_id: str) -> None:
    project_dir = resolve_project_root(project_root)
    seen_pids: set[int] = set()
    for record in load_active_task_records(project_dir):
        if str(record.get("task_id") or "").strip() != str(task_id or "").strip():
            continue
        for key in ("runtime_pid", "pid"):
            try:
                pid = int(record.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen_pids:
                continue
            seen_pids.add(pid)
            _terminate_pid(pid)
    lock_path = project_dir / ".app-delivery-runtime" / "locks" / "execution.lock"
    lock_details = read_lock_metadata(lock_path)
    try:
        lock_owner_pid = int((lock_details or {}).get("pid") or 0)
    except (TypeError, ValueError):
        lock_owner_pid = 0
    if lock_owner_pid > 0 and lock_owner_pid not in seen_pids and lock_file_is_locked(lock_path):
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


def _prune_stale_execution_lock(project_root: Path | str) -> bool:
    project_dir = resolve_project_root(project_root)
    lock_path = project_dir / ".app-delivery-runtime" / "locks" / "execution.lock"
    lock_details = read_lock_metadata(lock_path)
    if lock_file_is_locked(lock_path):
        return False
    if lock_details:
        with contextlib.suppress(OSError):
            lock_path.unlink()
        return True
    return False


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
    runtime_state = load_task_runtime_state(project_root, task_id)
    completed_at = _parse_iso_datetime(runtime_state.get("completed_at"))
    if completed_at is None:
        return False
    try:
        input_mtime = dt.datetime.fromtimestamp(input_path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return False
    return input_mtime > completed_at


def _run_host_skill_for_control_step(step: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    return None


def _host_handoff_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    path = project_dir / HOST_HANDOFF_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _host_handoff_id(next_step: dict[str, Any], project_root: Path | None = None) -> str:
    payload_data: dict[str, Any] = dict(next_step)
    if project_root is not None:
        skill = str(next_step.get("skill") or "").strip()
        task_id = str(next_step.get("task_id") or "").strip()
        request_path: Path | None = None
        if skill == "code-review" and task_id:
            request_path = code_review_request_path(project_root, task_id)
        elif skill == "final-review":
            request_path = final_review_request_path(project_root)
        if request_path is not None and request_path.exists():
            stat = request_path.stat()
            payload_data["request_path"] = str(request_path)
            payload_data["request_mtime_ns"] = stat.st_mtime_ns
    payload = json.dumps(payload_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _host_handoff_retry_due(existing: dict[str, Any], handoff_id: str, now: dt.datetime) -> bool:
    if str(existing.get("handoff_id") or "") != handoff_id:
        return False
    if str(existing.get("status") or "") != "waiting_for_host":
        return False
    if _nonnegative_int(existing.get("retry_attempts")) >= HOST_HANDOFF_MAX_RETRY_ATTEMPTS:
        return False
    updated_at = _parse_iso_datetime(existing.get("updated_at"))
    if updated_at is None:
        return True
    return (now - updated_at).total_seconds() >= HOST_HANDOFF_RETRY_AFTER_SECONDS


def _save_host_handoff(project_root: Path, next_step: dict[str, Any]) -> dict[str, Any]:
    path = _host_handoff_path(project_root)
    handoff_id = _host_handoff_id(next_step, project_root)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat().replace("+00:00", "Z")
    existing = load_json(path, {})
    existing_status = str(existing.get("status") or "") if isinstance(existing, dict) else ""
    same_waiting_handoff = isinstance(existing, dict) and str(existing.get("handoff_id") or "") == handoff_id and existing_status in {"waiting_for_host", HANDOFF_REVIEW_RUNNING_STATUS, HANDOFF_REVIEW_RUNNER_FAILED_STATUS}
    retry_attempts = _nonnegative_int(existing.get("retry_attempts")) if same_waiting_handoff else 0
    retry_due = _host_handoff_retry_due(existing, handoff_id, now) if isinstance(existing, dict) else False
    if retry_due:
        retry_attempts += 1
    body: dict[str, Any] = {
        "schema_version": "1",
        "handoff_id": handoff_id,
        "status": existing_status if same_waiting_handoff and existing_status in {HANDOFF_REVIEW_RUNNING_STATUS, HANDOFF_REVIEW_RUNNER_FAILED_STATUS} else "waiting_for_host",
        "project": str(project_root),
        "requested_at": str(existing.get("requested_at") or now_iso) if same_waiting_handoff else now_iso,
        "updated_at": now_iso,
        "kind": str(next_step.get("kind") or ""),
        "action": str(next_step.get("action") or ""),
        "skill": str(next_step.get("skill") or ""),
        "task_id": str(next_step.get("task_id") or ""),
        "task_title": str(next_step.get("task_title") or ""),
        "expected_input_path": str(next_step.get("expected_input_path") or ""),
        "message": str(next_step.get("message") or ""),
        "prompt": str(next_step.get("prompt") or ""),
        "import_command": str(next_step.get("import_command") or ""),
        "retry_attempts": retry_attempts,
        "retry_after_seconds": HOST_HANDOFF_RETRY_AFTER_SECONDS,
        "max_retry_attempts": HOST_HANDOFF_MAX_RETRY_ATTEMPTS,
        "next_step": next_step,
    }
    if same_waiting_handoff and isinstance(existing, dict):
        for key in ("retry_requested_at", "retry_reason", "handler", "runner_pid", "runner_started_at", "runner_stdout_log", "runner_stderr_log", "review_runner_status", "review_runner_reason", "review_runner_completed_at", "review_runner_stdout_log", "review_runner_stderr_log", "review_runner_last_activity_at", "review_runner_idle_seconds"):
            if existing.get(key):
                body[key] = existing[key]
    if retry_due:
        body["retry_requested_at"] = now_iso
        body["retry_reason"] = "host handoff remained waiting_for_host after retry interval"
    write_json(path, body)
    return body


def _review_import_outcome(exit_code: int) -> str:
    return "accepted_pass" if exit_code == 0 else "accepted_changes_requested"


def _host_handoff_imported_result(project_root: Path, *, skill: str, task_id: str | None, input_path: Path) -> dict[str, Any] | None:
    handoff = load_json(_host_handoff_path(project_root), {})
    if not isinstance(handoff, dict):
        return None
    if str(handoff.get("status") or "").strip() != "imported":
        return None
    if str(handoff.get("skill") or "").strip() != skill:
        return None
    expected_task_id = str(task_id or "").strip()
    handoff_task_id = str(handoff.get("task_id") or "").strip()
    if expected_task_id and handoff_task_id and handoff_task_id != expected_task_id:
        return None
    recorded_input = str(handoff.get("input_path") or "").strip()
    if recorded_input:
        try:
            if Path(recorded_input).expanduser().resolve() != input_path.expanduser().resolve():
                return None
        except OSError:
            if recorded_input != str(input_path):
                return None
    try:
        exit_code = int(handoff.get("import_exit_code") or 0)
    except (TypeError, ValueError):
        exit_code = 0
    result: dict[str, Any] = {
        "status": "already_imported",
        "skill": skill,
        "input_path": str(input_path),
        "exit_code": exit_code,
        "import_outcome": _review_import_outcome(exit_code) if skill in {"code-review", "final-review"} else ("imported" if exit_code == 0 else "imported_nonzero"),
    }
    if expected_task_id or handoff_task_id:
        result["task_id"] = expected_task_id or handoff_task_id
    return result


def _mark_host_handoff_imported(project_root: Path, *, skill: str, task_id: str | None, input_path: Path, exit_code: int) -> None:
    path = _host_handoff_path(project_root)
    existing = load_json(path, {})
    body = dict(existing) if isinstance(existing, dict) else {}
    body.update(
        {
            "schema_version": "1",
            "status": "imported",
            "project": str(project_root),
            "updated_at": utc_now_iso(),
            "imported_at": utc_now_iso(),
            "skill": skill,
            "task_id": str(task_id or body.get("task_id") or ""),
            "input_path": str(input_path),
            "import_exit_code": exit_code,
            "import_outcome": _review_import_outcome(exit_code) if skill in {"code-review", "final-review"} else ("imported" if exit_code == 0 else "imported_nonzero"),
        }
    )
    write_json(path, body)


def _mark_host_handoff_review_running(project_root: Path, handoff: dict[str, Any], *, pid: int, started_at: str, stdout_log: Path, stderr_log: Path) -> None:
    path = _host_handoff_path(project_root)
    existing = load_json(path, {})
    body = dict(existing) if isinstance(existing, dict) else dict(handoff)
    for key in (
        "review_runner_status",
        "review_runner_reason",
        "review_runner_completed_at",
        "review_runner_stdout_log",
        "review_runner_stderr_log",
        "review_runner_last_activity_at",
        "review_runner_idle_seconds",
        "review_runner_next_retry_at",
    ):
        body.pop(key, None)
    body.update(
        {
            "schema_version": "1",
            "status": HANDOFF_REVIEW_RUNNING_STATUS,
            "project": str(project_root),
            "updated_at": utc_now_iso(),
            "handler": "review_runner",
            "runner_pid": pid,
            "runner_started_at": started_at,
            "runner_stdout_log": str(stdout_log),
            "runner_stderr_log": str(stderr_log),
        }
    )
    write_json(path, body)


def _mark_host_handoff_review_runner_failed(project_root: Path, fallback_state: dict[str, Any], *, reason: str) -> None:
    path = _host_handoff_path(project_root)
    existing = load_json(path, {})
    if not isinstance(existing, dict):
        return
    handoff_id = str(existing.get("handoff_id") or "").strip()
    fallback_handoff_id = str(fallback_state.get("handoff_id") or "").strip()
    if not handoff_id or handoff_id != fallback_handoff_id:
        return
    existing_status = str(existing.get("status") or "").strip()
    if existing_status not in {HANDOFF_REVIEW_RUNNING_STATUS, "waiting_for_host", HANDOFF_REVIEW_RUNNER_FAILED_STATUS}:
        return
    completed_at = str(fallback_state.get("completed_at") or "").strip() or utc_now_iso()
    body = dict(existing)
    body.update(
        {
            "schema_version": "1",
            "status": HANDOFF_REVIEW_RUNNER_FAILED_STATUS,
            "project": str(project_root),
            "updated_at": utc_now_iso(),
            "handler": "review_runner",
            "review_runner_status": "failed",
            "review_runner_reason": reason,
            "review_runner_completed_at": completed_at,
            "review_runner_stdout_log": str(fallback_state.get("stdout_log") or existing.get("runner_stdout_log") or ""),
            "review_runner_stderr_log": str(fallback_state.get("stderr_log") or existing.get("runner_stderr_log") or ""),
            "review_runner_last_activity_at": str(fallback_state.get("last_activity_at") or "").strip() or None,
            "review_runner_idle_seconds": fallback_state.get("idle_seconds"),
            "message": "Review runner failed before importing a fresh code-review artifact; retry may run automatically if attempts remain.",
        }
    )
    write_json(path, body)


def _mark_host_handoff_review_import_failed(project_root: Path, step: dict[str, Any], input_path: Path, exc: DeliveryError) -> None:
    path = _host_handoff_path(project_root)
    existing = load_json(path, {})
    if not isinstance(existing, dict):
        return
    existing_input = str(existing.get("expected_input_path") or "").strip()
    step_input = str(step.get("expected_input_path") or "").strip()
    if existing_input and Path(existing_input).expanduser().resolve() != input_path:
        return
    if step_input and Path(step_input).expanduser().resolve() != input_path:
        return
    existing_status = str(existing.get("status") or "").strip()
    if existing_status not in {HANDOFF_REVIEW_RUNNING_STATUS, "waiting_for_host", HANDOFF_REVIEW_RUNNER_FAILED_STATUS}:
        return
    body = dict(existing)
    review_error_payload = exc.to_payload()
    body.update(
        {
            "schema_version": "1",
            "status": HANDOFF_REVIEW_RUNNER_FAILED_STATUS,
            "project": str(project_root),
            "updated_at": utc_now_iso(),
            "handler": "review_runner",
            "review_runner_status": "failed",
            "review_runner_reason": f"review_import_failed:{exc.code}",
            "review_runner_completed_at": utc_now_iso(),
            "review_runner_input_path": str(input_path),
            "review_runner_error_class": str(review_error_payload.get("error_class") or ""),
            "review_runner_error_message": exc.message,
            "message": "Review runner produced an artifact, but framework import rejected it; inspect the import error and retry with a corrected artifact.",
        }
    )
    write_json(path, body)


def _mark_host_handoff_notice(project_root: Path, next_step: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema_version": "1",
        "status": "notice",
        "project": str(project_root),
        "updated_at": utc_now_iso(),
        "kind": str(next_step.get("kind") or ""),
        "action": str(next_step.get("action") or ""),
        "skill": "",
        "task_id": str(next_step.get("task_id") or ""),
        "expected_input_path": "",
        "message": str(next_step.get("message") or ""),
        "prompt": str(next_step.get("prompt") or ""),
        "import_command": str(next_step.get("import_command") or ""),
        "next_step": next_step,
    }
    write_json(_host_handoff_path(project_root), body)
    return body


def _save_waiting_for_host_step(project_root: Path, next_step: dict[str, Any]) -> None:
    handoff = _save_host_handoff(project_root, next_step) if _host_step_expects_importable_artifact(next_step) else _mark_host_handoff_notice(project_root, next_step)
    handoff_status = str(handoff.get("status") or "")
    save_watchdog_state(
        project_root,
        {
            "pid": os.getpid(),
            "project": str(project_root),
            "status": "waiting_for_host" if handoff_status == "waiting_for_host" else "host_notice",
            "last_action": str(next_step.get("action") or "host_step"),
            "skill": str(next_step.get("skill") or ""),
            "task_id": str(next_step.get("task_id") or ""),
            "expected_input_path": str(next_step.get("expected_input_path") or ""),
            "host_handoff_id": str(handoff.get("handoff_id") or ""),
            "host_handoff_retry_attempts": _nonnegative_int(handoff.get("retry_attempts")),
            **({"host_handoff_retry_requested_at": str(handoff.get("retry_requested_at"))} if handoff.get("retry_requested_at") else {}),
        },
    )


def _host_fallback_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    path = project_dir / HOST_FALLBACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _host_fallback_lock_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    path = project_dir / HOST_FALLBACK_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _host_handoff_wait_seconds(handoff: dict[str, Any], now: dt.datetime | None = None) -> int:
    started_at = _parse_iso_datetime(handoff.get("requested_at")) or _parse_iso_datetime(handoff.get("updated_at"))
    if started_at is None:
        return 0
    current = now or dt.datetime.now(dt.timezone.utc)
    return max(0, int((current - started_at).total_seconds()))


def _path_mtime_iso(path_text: Any) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        return ""


def _newest_iso(values: list[Any]) -> str:
    newest: dt.datetime | None = None
    for value in values:
        parsed = _parse_iso_datetime(value)
        if parsed is None:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest.isoformat().replace("+00:00", "Z") if newest is not None else ""


def _review_runner_activity_at(lock: dict[str, Any], handoff: dict[str, Any]) -> str:
    return _newest_iso(
        [
            lock.get("started_at"),
            lock.get("updated_at"),
            _path_mtime_iso(lock.get("stdout_log")),
            _path_mtime_iso(lock.get("stderr_log")),
            _path_mtime_iso(handoff.get("expected_input_path")),
        ]
    )


def _review_runner_idle_seconds(lock: dict[str, Any], handoff: dict[str, Any], *, now: dt.datetime | None = None) -> int:
    activity_at = _parse_iso_datetime(_review_runner_activity_at(lock, handoff))
    if activity_at is None:
        activity_at = _parse_iso_datetime(lock.get("started_at"))
    if activity_at is None:
        return 0
    current = now or dt.datetime.now(dt.timezone.utc)
    return max(0, int((current - activity_at).total_seconds()))


def _host_fallback_attempt_count(state: dict[str, Any], handoff_id: str) -> int:
    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
    try:
        return max(0, int(attempts.get(handoff_id) or 0))
    except (TypeError, ValueError):
        return 0


def _write_host_fallback_state(project_root: Path, payload: dict[str, Any]) -> None:
    path = _host_fallback_path(project_root)
    existing = load_json(path, {})
    existing_state = dict(existing) if isinstance(existing, dict) else {}
    attempts = existing_state.get("attempts") if isinstance(existing_state.get("attempts"), dict) else {}
    payload_handoff_id = str(payload.get("handoff_id") or "").strip()
    existing_handoff_id = str(existing_state.get("handoff_id") or "").strip()
    if str(payload.get("status") or "").strip() == "running" or (payload_handoff_id and existing_handoff_id and payload_handoff_id != existing_handoff_id):
        state = {"attempts": attempts}
    else:
        state = existing_state
    state.update(payload)
    if str(state.get("status") or "").strip() == "running":
        state.pop("completed_at", None)
    state["updated_at"] = utc_now_iso()
    write_json(path, state)


def _record_host_fallback_attempt(project_root: Path, handoff_id: str) -> int:
    path = _host_fallback_path(project_root)
    existing = load_json(path, {})
    state = dict(existing) if isinstance(existing, dict) else {}
    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
    count = _host_fallback_attempt_count(state, handoff_id) + 1
    attempts[handoff_id] = count
    state["attempts"] = attempts
    state["updated_at"] = utc_now_iso()
    write_json(path, state)
    return count


def _reconcile_host_fallback_lock(project_root: Path) -> dict[str, Any] | None:
    lock_path = _host_fallback_lock_path(project_root)
    lock = load_json(lock_path, {})
    if not isinstance(lock, dict) or str(lock.get("status") or "").strip() != "running":
        return None
    try:
        pid = int(lock.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if process_alive(pid):
        handoff = load_json(_host_handoff_path(project_root), {})
        handoff_id = str(lock.get("handoff_id") or "").strip()
        current_handoff_id = str(handoff.get("handoff_id") or "").strip() if isinstance(handoff, dict) else ""
        current_status = str(handoff.get("status") or "").strip() if isinstance(handoff, dict) else ""
        if current_handoff_id == handoff_id and current_status == "imported":
            _terminate_pid(pid)
            payload = {
                **lock,
                "status": "completed",
                "reason": "handoff_imported_while_fallback_process_alive",
                "completed_at": utc_now_iso(),
            }
            _write_host_fallback_state(project_root, payload)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return payload
        idle_seconds = _review_runner_idle_seconds(lock, handoff if isinstance(handoff, dict) else {})
        idle_timeout = _review_runner_idle_timeout_seconds(project_root)
        if idle_seconds >= idle_timeout:
            _terminate_pid(pid)
            payload = {
                **lock,
                "status": "failed",
                "reason": "review_runner_idle_timeout",
                "completed_at": utc_now_iso(),
                "last_activity_at": _review_runner_activity_at(lock, handoff if isinstance(handoff, dict) else {}),
                "idle_seconds": idle_seconds,
                "idle_timeout_seconds": idle_timeout,
            }
            _write_host_fallback_state(project_root, payload)
            _mark_host_handoff_review_runner_failed(project_root, payload, reason="review_runner_idle_timeout")
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return payload
        return lock

    handoff = load_json(_host_handoff_path(project_root), {})
    handoff_id = str(lock.get("handoff_id") or "").strip()
    current_handoff_id = str(handoff.get("handoff_id") or "").strip() if isinstance(handoff, dict) else ""
    current_status = str(handoff.get("status") or "").strip() if isinstance(handoff, dict) else ""
    if current_handoff_id and current_handoff_id != handoff_id:
        status = "superseded"
        reason = "handoff_changed_after_fallback_exit"
    elif current_status == "imported":
        status = "completed"
        reason = "handoff_imported_after_fallback_exit"
    else:
        status = "failed"
        reason = "fallback_process_exited_before_handoff_imported"
    payload = {
        **lock,
        "status": status,
        "reason": reason,
        "completed_at": utc_now_iso(),
        "last_activity_at": _review_runner_activity_at(lock, handoff if isinstance(handoff, dict) else {}),
    }
    _write_host_fallback_state(project_root, payload)
    if status == "failed":
        _mark_host_handoff_review_runner_failed(project_root, payload, reason=reason)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return payload


def _reconcile_host_fallback_state(project_root: Path) -> dict[str, Any] | None:
    state = load_json(_host_fallback_path(project_root), {})
    if not isinstance(state, dict) or str(state.get("status") or "").strip() != "running":
        return None
    handoff = load_json(_host_handoff_path(project_root), {})
    if not isinstance(handoff, dict):
        return None
    handoff_id = str(state.get("handoff_id") or "").strip()
    if not handoff_id or str(handoff.get("handoff_id") or "").strip() != handoff_id:
        return None
    if str(handoff.get("status") or "").strip() != "imported":
        return None
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and process_alive(pid):
        _terminate_pid(pid)
    payload = {
        **state,
        "status": "completed",
        "reason": "handoff_imported_after_fallback_state_running",
        "completed_at": utc_now_iso(),
        "last_activity_at": _review_runner_activity_at(state, handoff),
    }
    _write_host_fallback_state(project_root, payload)
    return payload


def _reconcile_failed_review_runner_handoff(project_root: Path) -> None:
    fallback_state = load_json(_host_fallback_path(project_root), {})
    if not isinstance(fallback_state, dict):
        return
    if str(fallback_state.get("status") or "").strip() != "failed":
        return
    reason = str(fallback_state.get("reason") or "review_runner_failed").strip() or "review_runner_failed"
    _mark_host_handoff_review_runner_failed(project_root, fallback_state, reason=reason)


def _resolve_hermes_bin() -> str | None:
    for candidate in (
        os.environ.get("APP_DELIVERY_HERMES_BIN"),
        os.environ.get("HERMES_BIN"),
        shutil.which("hermes"),
        str(Path.home() / ".local" / "bin" / "hermes"),
    ):
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def _reap_child_process(process: Any) -> None:
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return
    thread = threading.Thread(target=wait, name=f"app-delivery-reap-{getattr(process, 'pid', 'child')}", daemon=True)
    thread.start()


def _host_fallback_prompt(project_root: Path, handoff: dict[str, Any]) -> str:
    task_id = str(handoff.get("task_id") or "").strip()
    expected_input = str(handoff.get("expected_input_path") or "").strip()
    next_step = handoff.get("next_step") if isinstance(handoff.get("next_step"), dict) else {}
    import_command = str((next_step.get("import_command") if isinstance(next_step, dict) else "") or handoff.get("import_command") or "").strip()
    return (
        "Use the app-delivery skill to consume the current host handoff exactly once.\n"
        f"Project: {project_root}\n"
        f"Handoff file: {project_root / HOST_HANDOFF_FILE}\n"
        f"Task id: {task_id}\n"
        "Requested host skill: code-review\n"
        f"Expected JSON output: {expected_input}\n"
        f"Import command: {import_command}\n\n"
        "Rules:\n"
        "- Do not edit target-project implementation code.\n"
        "- Read the code-review request for the task id from .app-delivery-runtime/review-requests/.\n"
        "- Produce the canonical code-review JSON at the expected output path.\n"
        "- Run the exact import command after writing the JSON.\n"
        "- If the import command fails, leave the JSON file in place and report the failure.\n"
        "- Return only a concise summary of what happened.\n"
    )


def _start_code_review_host_fallback(project_root: Path, handoff: dict[str, Any], *, reason: str) -> dict[str, Any]:
    handoff_id = str(handoff.get("handoff_id") or "").strip()
    task_id = str(handoff.get("task_id") or "").strip()
    hermes_bin = _resolve_hermes_bin()
    if not hermes_bin:
        result = {"status": "failed", "reason": "hermes_binary_missing", "handoff_id": handoff_id, "task_id": task_id}
        _write_host_fallback_state(project_root, result)
        return result

    lock_path = _host_fallback_lock_path(project_root)
    _reconcile_host_fallback_lock(project_root)
    lock = load_json(lock_path, {})
    if isinstance(lock, dict) and str(lock.get("handoff_id") or "") == handoff_id:
        try:
            pid = int(lock.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if process_alive(pid):
            return {"status": "running", "reason": "existing_fallback_running", "handoff_id": handoff_id, "task_id": task_id, "pid": pid}
        failed_state = {"status": "failed", "reason": "previous_fallback_exited_without_clearing_handoff", "handoff_id": handoff_id, "task_id": task_id, "pid": pid, "completed_at": utc_now_iso()}
        _write_host_fallback_state(project_root, failed_state)
        _mark_host_handoff_review_runner_failed(project_root, failed_state, reason="previous_fallback_exited_without_clearing_handoff")

    fallback_state = load_json(_host_fallback_path(project_root), {})
    attempts = _host_fallback_attempt_count(fallback_state if isinstance(fallback_state, dict) else {}, handoff_id)
    max_attempts = _host_fallback_max_attempts(project_root)
    if attempts >= max_attempts:
        return {"status": "skipped", "reason": "max_attempts_reached", "handoff_id": handoff_id, "task_id": task_id, "attempts": attempts, "max_attempts": max_attempts}

    logs_dir = project_root / ".app-delivery-runtime" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    safe_handoff_id = handoff_id or hashlib.sha256(json.dumps(handoff, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    stdout_path = logs_dir / f"host-fallback-{safe_handoff_id}.out.log"
    stderr_path = logs_dir / f"host-fallback-{safe_handoff_id}.err.log"
    command = [hermes_bin, "--oneshot", _host_fallback_prompt(project_root, handoff), "--skills", "app-delivery"]
    env = os.environ.copy()
    env.setdefault("OPC_HOME", str(Path.home() / "opc"))
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            env=env,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    _reap_child_process(process)

    attempt = _record_host_fallback_attempt(project_root, handoff_id)
    lock_payload = {
        "schema_version": "1",
        "status": "running",
        "handoff_id": handoff_id,
        "task_id": task_id,
        "skill": "code-review",
        "pid": process.pid,
        "started_at": started_at,
        "reason": reason,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "command": command,
    }
    write_json(lock_path, lock_payload)
    _write_host_fallback_state(project_root, {**lock_payload, "attempt": attempt})
    _mark_host_handoff_review_running(project_root, handoff, pid=process.pid, started_at=started_at, stdout_log=stdout_path, stderr_log=stderr_path)
    return {"status": "started", "handoff_id": handoff_id, "task_id": task_id, "pid": process.pid, "attempt": attempt, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path)}


def _maybe_start_code_review_host_fallback(project_root: Path) -> dict[str, Any]:
    _reconcile_host_fallback_lock(project_root)
    _reconcile_host_fallback_state(project_root)
    _reconcile_failed_review_runner_handoff(project_root)
    if not _host_fallback_enabled_for(project_root):
        return {"status": "skipped", "reason": "host_fallback_disabled"}
    handoff = load_json(_host_handoff_path(project_root), {})
    if not isinstance(handoff, dict):
        return {"status": "skipped", "reason": "host_handoff_missing"}
    handoff_status = str(handoff.get("status") or "").strip()
    if handoff_status not in {"waiting_for_host", HANDOFF_REVIEW_RUNNING_STATUS, HANDOFF_REVIEW_RUNNER_FAILED_STATUS}:
        return {"status": "skipped", "reason": "host_handoff_not_waiting", "handoff_status": handoff_status}
    if str(handoff.get("skill") or "").strip() != "code-review":
        return {"status": "skipped", "reason": "unsupported_host_skill", "skill": str(handoff.get("skill") or "")}
    task_id = str(handoff.get("task_id") or "").strip()
    if not task_id:
        return {"status": "skipped", "reason": "task_id_missing"}

    next_step = handoff.get("next_step") if isinstance(handoff.get("next_step"), dict) else handoff
    result = _execute_host_control_step_if_ready(next_step, project_root)
    if result is not None:
        return {"status": "imported_ready_artifact", "result": result}

    wait_seconds = _host_handoff_wait_seconds(handoff)
    after_seconds = _host_fallback_after_seconds(project_root)
    if wait_seconds < after_seconds:
        return {"status": "skipped", "reason": "not_due", "wait_seconds": wait_seconds, "after_seconds": after_seconds, "task_id": task_id}
    return _start_code_review_host_fallback(project_root, handoff, reason=f"waiting_for_host exceeded {after_seconds} seconds")


def _execute_host_control_step_if_ready(step: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    expected_input = str(step.get("expected_input_path") or "").strip()
    if not expected_input:
        return None
    input_path = Path(expected_input).expanduser().resolve()
    if not input_path.exists():
        return None

    skill = str(step.get("skill") or "").strip()
    task_id = str(step.get("task_id") or "").strip()
    if skill == "code-review" and task_id:
        request_path = code_review_request_path(project_root, task_id)
        if request_path.exists() and not _review_input_matches_latest_runtime_attempt(project_root, task_id, input_path):
            return None
    if skill == "final-review":
        request_path = final_review_request_path(project_root)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns:
            return None

    if skill == "code-review":
        if not task_id:
            raise DeliveryError(code="control_review_task_missing", message="host review step is missing task_id", exit_code=2, details={"step": step})
        payload, loaded_input_path = load_stage_payload(project_root, "code-review", str(input_path), expected_type=dict)
        assert isinstance(payload, dict)
        try:
            exit_code = import_task_review(project_root, task_id, payload, loaded_input_path)
        except DeliveryError as exc:
            already_imported = _host_handoff_imported_result(project_root, skill="code-review", task_id=task_id, input_path=loaded_input_path)
            if exc.code == "review_task_not_pending" and already_imported is not None:
                return already_imported
            _mark_host_handoff_review_import_failed(project_root, step, loaded_input_path, exc)
            raise
    elif skill == "final-review":
        payload, loaded_input_path = load_stage_payload(project_root, "final-review", str(input_path), expected_type=dict)
        assert isinstance(payload, dict)
        exit_code = import_final_review(project_root, payload, loaded_input_path)
    elif skill == "spec-review":
        payload, loaded_input_path = load_stage_payload(project_root, "spec-review", str(input_path), expected_type=dict, required_fields=["requirements", "acceptance_scenarios"])
        assert isinstance(payload, dict)
        exit_code = import_spec_review(project_root, payload, loaded_input_path)
    elif skill == "arch-design":
        payload, loaded_input_path = load_stage_payload(project_root, "arch-design", str(input_path), expected_type=dict, required_fields=["architecture_md", "shared_components_md", "ui_required"])
        assert isinstance(payload, dict)
        exit_code = import_arch_design(project_root, payload, loaded_input_path)
    elif skill == "ui-design":
        payload, loaded_input_path = load_stage_payload(project_root, "ui-design", str(input_path), expected_type=dict, required_fields=["template_selection_md", "design_system_md", "page_archetypes_md", "states_md"])
        assert isinstance(payload, dict)
        exit_code = import_ui_design(project_root, payload, loaded_input_path)
    elif skill == "project-context-sync":
        payload, loaded_input_path = load_stage_payload(project_root, "project-context-sync", str(input_path), expected_type=dict, required_fields=["test_plan"])
        assert isinstance(payload, dict)
        exit_code = import_context_sync(project_root, payload, loaded_input_path)
    elif skill == "task-decompose":
        payload, loaded_input_path = load_stage_payload(project_root, "task-decompose", str(input_path), expected_type=dict, required_fields=["items", "validation_gates", "delivery_complexity"])
        assert isinstance(payload, dict)
        exit_code = import_decompose(project_root, payload, loaded_input_path)
    else:
        return None

    result: dict[str, Any] = {"status": "imported", "skill": skill, "input_path": str(loaded_input_path), "exit_code": exit_code}
    if skill in {"code-review", "final-review"}:
        result["import_outcome"] = _review_import_outcome(exit_code)
    if task_id:
        result["task_id"] = task_id
    _mark_host_handoff_imported(project_root, skill=skill, task_id=task_id or None, input_path=loaded_input_path, exit_code=exit_code)
    return result


def _execute_host_control_step(step: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    result = _execute_host_control_step_if_ready(step, project_root)
    if result is not None:
        return result
    try:
        host_execution = _run_host_skill_for_control_step(step, project_root)
    except DeliveryError as exc:
        result = _execute_host_control_step_if_ready(step, project_root)
        if result is not None:
            result["host_execution"] = {"status": "failed_but_existing_artifact_imported", "code": exc.code, "message": exc.message}
            return result
        raise
    if host_execution is None:
        return None
    result = _execute_host_control_step_if_ready(step, project_root)
    if result is None:
        raise DeliveryError(
            code="host_skill_output_missing",
            message=f"host skill {step.get('skill')} completed but did not produce an importable artifact",
            exit_code=2,
            details={"step": step, "host_execution": host_execution},
            suggested_action="Check the host skill output and ensure it writes the canonical JSON artifact to expected_input_path.",
        )
    result["host_execution"] = host_execution
    return result


def _execute_framework_control_step(step: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    action = str(step.get("action") or "").strip()
    project_root = resolve_project_root(args.project)
    if action == "run_scaffold":
        return _run_scaffold_once(project_root)
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
            raise DeliveryError(code="stalled_recovery_task_missing", message="recover_stalled step is missing task_id", exit_code=2, details={"step": step})
        return _run_stalled_recovery_once(
            project_root,
            task_id=task_id,
            runtime=getattr(args, "runtime", None),
            runtime_attention=step,
            spawn_watchdog_after=False,
        )
    if action == "auto_repair_exception":
        task_id = str(step.get("task_id") or "").strip()
        if not task_id:
            raise DeliveryError(code="auto_repair_task_missing", message="auto exception repair step is missing task_id", exit_code=2, details={"step": step})
        mark_auto_exception_repair_attempted(project_root, task_id)
        return _run_fix_once(
            project_root,
            task_id=task_id,
            runtime=getattr(args, "runtime", None),
            no_start=True,
            spawn_watchdog_after=False,
        )
    raise DeliveryError(
        code="control_step_unsupported",
        message=f"unsupported framework control action: {action}",
        exit_code=2,
        details={"step": step},
    )


def cmd_control(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    goal = str(args.goal or "auto").strip().lower()
    control_goal = "auto" if goal == "step" else goal

    if control_goal == "repair" and not str(getattr(args, "task_id", "") or "").strip():
        raise DeliveryError(
            code="repair_task_missing",
            message="control goal=repair requires --task-id",
            exit_code=2,
        )

    executed_steps: list[dict[str, Any]] = []

    if control_goal == "repair":
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
        control_goal = "auto"

    snapshot = run_control(
        project_root,
        goal=control_goal,
        requirements_path=getattr(args, "requirements", None),
        repair_task_id=getattr(args, "task_id", None),
    )
    executed_steps.extend(snapshot.pop("executed_steps", []))

    safety = 0
    while goal in {"auto", "step"} and snapshot.get("must_continue"):
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
            snapshot = run_control(
                project_root,
                goal="auto",
                requirements_path=getattr(args, "requirements", None),
            )
            executed_steps.extend(snapshot.pop("executed_steps", []))
            if goal == "step":
                break
            continue
        if owner != "framework":
            break
        result = _execute_framework_control_step(next_step, args)
        executed_steps.append({
            "owner": "framework",
            "action": action,
            "result": result,
        })
        snapshot = run_control(
            project_root,
            goal="auto",
            requirements_path=getattr(args, "requirements", None),
        )
        executed_steps.extend(snapshot.pop("executed_steps", []))
        if goal == "step":
            break

    if executed_steps:
        snapshot["executed_steps"] = executed_steps

    deferred_to_host = any(step.get("deferred_to_host") for step in executed_steps)
    if deferred_to_host:
        next_step = snapshot.get("next_step") if isinstance(snapshot.get("next_step"), dict) else {}
        if _host_step_expects_importable_artifact(next_step):
            snapshot["host_handoff"] = _save_host_handoff(project_root, next_step)
        else:
            snapshot["host_handoff"] = _mark_host_handoff_notice(project_root, next_step)
            save_watchdog_state(
                project_root,
                {
                    "pid": 0,
                    "project": str(project_root),
                    "status": "host_notice",
                    "last_action": str(next_step.get("action") or "host_notice"),
                    "task_id": str(next_step.get("task_id") or ""),
                },
            )
    if goal == "auto" and _watchdog_enabled_for(project_root):
        next_step = snapshot.get("next_step") if isinstance(snapshot.get("next_step"), dict) else {}
        if deferred_to_host:
            if _host_step_expects_importable_artifact(next_step):
                spawn_watchdog(project_root)
        else:
            control_status = str(snapshot.get("control_status") or "").strip()
            if control_status in {"in_progress", "running"}:
                spawn_watchdog(project_root)
    elif goal == "status" and _watchdog_enabled_for(project_root) and _snapshot_needs_watchdog(snapshot):
        snapshot["watchdog_pid"] = spawn_watchdog(project_root)

    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    if goal == "step" and deferred_to_host:
        return 1
    if goal == "auto" and deferred_to_host:
        next_step = snapshot.get("next_step") if isinstance(snapshot.get("next_step"), dict) else {}
        if _watchdog_enabled_for(project_root) and _host_step_expects_importable_artifact(next_step):
            return 0
        return 1
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
    if str(result.get("status") or "").strip() == "deferred":
        raise DeliveryError(
            code="verify_project_incomplete",
            message=str(result.get("summary") or "final verification deferred until all non-final tasks are verified"),
            exit_code=2,
            details=result,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("passed") else 1


def cmd_gate(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload = accept_gate(project_root, args.gate_id, reason=args.reason, actor="host")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    task_id = str(args.task_id or "").strip()
    action = str(args.action or "").strip()
    reason = str(args.reason or "").strip()
    if not reason:
        raise DeliveryError(
            code="manual_task_action_reason_required",
            message="manual task actions require --reason",
            exit_code=2,
            details={"project": str(project_root), "task_id": task_id, "action": action},
        )
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        tasks = all_tasks(project_root)
        task = next((row for row in tasks if row.id == task_id), None)
        if task is None:
            raise DeliveryError(
                code="task_missing",
                message=f"task does not exist: {task_id}",
                exit_code=2,
                details={"project": str(project_root), "task_id": task_id},
            )
        now = utc_now_iso()
        if action == "accept":
            tasks = mark_task(
                tasks,
                task_id,
                "verified",
                review_status=task.review_status or "pass",
                reviewed_at=task.reviewed_at or now,
                verified_at=now,
                completed_at=task.completed_at or now,
                blocked_reason=f"manual host acceptance: {reason}",
                attempts=max(task.attempts, 1),
            )
            save_tasks(project_root, tasks)
            save_task_runtime_state(
                project_root,
                task_id,
                {
                    "manual_override": {"action": "accept", "reason": reason, "actor": "host", "accepted_at": now},
                    "review_repair_limit_reached": False,
                    "final_repair_limit_reached": False,
                    "review_changes_requested_count": 0,
                    "failure_count": 0,
                    "failure_signature": "",
                    "failure_kind": "",
                    "failure_message": "",
                },
            )
        elif action == "reset-repair":
            if task.status == "verified" and task.task_kind != "repair":
                raise DeliveryError(
                    code="verified_task_repair_forbidden",
                    message=(
                        f"task {task_id} is already verified; do not reopen verified feature tasks. "
                        "Repair regressions from the current task or a dedicated repair bundle instead."
                    ),
                    exit_code=2,
                    details={"task_id": task_id, "status": task.status, "task_kind": task.task_kind},
                )
            _guard_final_repair_limit_not_reopened(project_root, task)
            tasks = reset_task(tasks, task_id, blocked_reason=f"manual repair reset: {reason}")
            save_tasks(project_root, tasks)
            save_task_runtime_state(
                project_root,
                task_id,
                {
                    "manual_override": {"action": "reset-repair", "reason": reason, "actor": "host", "reset_at": now},
                    "review_repair_limit_reached": False,
                    "final_repair_limit_reached": False,
                    "review_changes_requested_count": 0,
                    "review_repair_limit_report": None,
                    "failure_count": 0,
                    "failure_signature": "",
                    "failure_kind": "",
                    "failure_message": "",
                },
            )
            reapply_result = try_reapply_task_exception_patch(project_root, task_id)
            save_task_runtime_state(project_root, task_id, _runtime_state_for_exception_patch_reapply(reapply_result))
        else:
            raise DeliveryError(
                code="manual_task_action_invalid",
                message=f"unsupported task action: {action}",
                exit_code=2,
                details={"supported_actions": ["accept", "reset-repair"]},
            )
        runtime_state = load_task_runtime_state(project_root, task_id)
    print(json.dumps({"status": "ok", "task_id": task_id, "action": action, "runtime_state": runtime_state}, indent=2, ensure_ascii=False))
    return 0


def cmd_code_review(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    import_status = "imported"
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "code-review", args.input, expected_type=dict)
        assert isinstance(payload, dict)
        try:
            import_exit_code = import_task_review(project_root, args.task_id, payload, input_path)
        except DeliveryError as exc:
            already_imported = _host_handoff_imported_result(project_root, skill="code-review", task_id=args.task_id, input_path=input_path)
            if exc.code == "review_task_not_pending" and already_imported is not None:
                import_exit_code = int(already_imported.get("exit_code") or 0)
                import_status = "already_imported"
            else:
                raise
        else:
            _mark_host_handoff_imported(project_root, skill="code-review", task_id=args.task_id, input_path=input_path, exit_code=import_exit_code)
        _refresh_project_summary_best_effort(project_root)
    if _watchdog_enabled_for(project_root):
        spawn_watchdog(project_root)
    print(json.dumps({"status": import_status, "stage": "code-review", "task_id": args.task_id, "input_path": str(input_path), "import_exit_code": import_exit_code, "import_outcome": _review_import_outcome(import_exit_code)}, indent=2, ensure_ascii=False))
    return 0


def cmd_final_review(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    with _project_execution_guard(project_root, already_locked=_command_locked(args)):
        payload, input_path = load_stage_payload(project_root, "final-review", args.input, expected_type=dict)
        assert isinstance(payload, dict)
        import_exit_code = import_final_review(project_root, payload, input_path)
        _mark_host_handoff_imported(project_root, skill="final-review", task_id="T-FINAL", input_path=input_path, exit_code=import_exit_code)
    if _watchdog_enabled_for(project_root):
        spawn_watchdog(project_root)
    print(json.dumps({"status": "imported", "stage": "final-review", "input_path": str(input_path), "import_exit_code": import_exit_code, "import_outcome": _review_import_outcome(import_exit_code)}, indent=2, ensure_ascii=False))
    return 0


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
        payload, input_path = load_stage_payload(project_root, "project-context-sync", args.input, expected_type=dict, required_fields=["test_plan"])
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
            hermes_bin="hermes",
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
    if str(result.get("status") or "").strip() == "exception":
        result.setdefault("fix_status", "not_resumed")
        result.setdefault(
            "next_actions",
            [
                "Inspect the exception task status and review artifact.",
                "Use `app-delivery task --action reset-repair --reason ...` only after deciding another repair attempt is justified.",
            ],
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1
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
        if control_status == "paused":
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "paused", "last_action": "paused"})
            return 0
        if control_status == "complete":
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "done", "last_action": "complete"})
            return 0
        if control_status == "blocked":
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "blocked", "last_action": "blocked"})
            return 0
        runtime_attention = payload.get("runtime_attention") if isinstance(payload.get("runtime_attention"), dict) else None
        active_task = payload.get("active_task") if isinstance(payload.get("active_task"), dict) else None
        if runtime_attention and runtime_attention.get("suspected") and active_task:
            task_id = str(active_task.get("id") or "").strip()
            if task_id:
                save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "running", "last_action": "stalled_recovery", "task_id": task_id})
                _run_stalled_recovery_once(project_root, task_id=task_id, runtime=None, runtime_attention=runtime_attention, spawn_watchdog_after=False)
                continue
        if payload.get("must_continue"):
            next_step = payload.get("next_step") if isinstance(payload.get("next_step"), dict) else {}
            owner = str(next_step.get("owner") or "framework").strip()
            if owner == "host":
                if not _host_step_expects_importable_artifact(next_step):
                    _save_waiting_for_host_step(project_root, next_step)
                    return 0
                result = _execute_host_control_step_if_ready(next_step, project_root)
                if result is not None:
                    save_watchdog_state(
                        project_root,
                        {
                            "pid": os.getpid(),
                            "project": str(project_root),
                            "status": "running",
                            "last_action": "host_artifact_imported",
                            "skill": str(next_step.get("skill") or ""),
                            "task_id": str(next_step.get("task_id") or ""),
                            "input_path": str(result.get("input_path") or ""),
                            "import_exit_code": result.get("exit_code"),
                        },
                    )
                    continue
                _save_waiting_for_host_step(project_root, next_step)
                fallback_result = _maybe_start_code_review_host_fallback(project_root) if _host_step_expects_importable_artifact(next_step) else {"status": "skipped", "reason": "host_notice_without_import_artifact"}
                if str(fallback_result.get("status") or "") in {"started", "running", "failed", "imported_ready_artifact"}:
                    save_watchdog_state(
                        project_root,
                        {
                            "pid": os.getpid(),
                            "project": str(project_root),
                            "status": "waiting_for_host",
                            "last_action": "host_fallback",
                            "skill": str(next_step.get("skill") or ""),
                            "task_id": str(next_step.get("task_id") or ""),
                            "host_fallback": fallback_result,
                        },
                    )
                time.sleep(interval_seconds)
                continue
            save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "running", "last_action": "resume"})
            cmd_control(
                argparse.Namespace(
                    project=str(project_root),
                    goal="auto",
                    requirements=None,
                    task_id=None,
                    hermes_bin=getattr(args, "hermes_bin", "hermes"),
                    runtime=None,
                    max_auto_tasks=None,
                    max_control_steps=CONTROL_STEP_LIMIT,
                )
            )
            continue
        save_watchdog_state(project_root, {"pid": os.getpid(), "project": str(project_root), "status": "running", "last_action": "idle"})
        time.sleep(interval_seconds)


def cmd_fix(args: argparse.Namespace) -> int:
    if getattr(args, "background", False) and getattr(args, "no_start", False):
        raise DeliveryError(
            code="fix_mode_invalid",
            message="--background cannot be combined with --no-start/--no-run",
            exit_code=2,
            details={"task_id": args.task_id},
        )
    if getattr(args, "background", False):
        _run_fix_once(
            args.project,
            task_id=args.task_id,
            runtime=getattr(args, "runtime", None),
            no_start=True,
            spawn_watchdog_after=False,
        )
        project_root = resolve_project_root(args.project)
        command = [sys.executable, "-m", "delivery", "control", "--goal", "auto", "--project", str(project_root)]
        if getattr(args, "runtime", None):
            command.extend(["--runtime", str(args.runtime)])
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(
            json.dumps(
                {
                    "status": "background_started",
                    "project": str(project_root),
                    "task_id": str(args.task_id),
                    "pid": process.pid,
                    "command": command,
                    "status_command": f"app-delivery control --goal status --project {project_root}",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    result = _run_fix_once(
        args.project,
        task_id=args.task_id,
        runtime=getattr(args, "runtime", None),
        no_start=getattr(args, "no_start", False),
        spawn_watchdog_after=True,
    )
    if result is None:
        return 0
    if str(result.get("status") or "").strip() == "exception":
        result.setdefault("fix_status", "not_resumed")
        result.setdefault(
            "next_actions",
            [
                "Inspect the exception task status and review artifact.",
                "Use `app-delivery task --action reset-repair --reason ...` only after deciding another repair attempt is justified.",
            ],
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_host_fallback(args: argparse.Namespace) -> int:
    project_root = resolve_project_root(args.project)
    result = _maybe_start_code_review_host_fallback(project_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if str(result.get("status") or "") in {"started", "running", "skipped", "imported_ready_artifact"} else 1


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

    gate = subparsers.add_parser("gate")
    gate.add_argument("--project", required=True)
    gate.add_argument("--gate-id", required=True)
    gate.add_argument("--action", choices=["accept"], default="accept")
    gate.add_argument("--reason", required=True)
    gate.set_defaults(func=cmd_gate)

    task_action = subparsers.add_parser("task")
    task_action.add_argument("--project", required=True)
    task_action.add_argument("--task-id", required=True)
    task_action.add_argument("--action", choices=["accept", "reset-repair"], required=True)
    task_action.add_argument("--reason", required=True)
    task_action.set_defaults(func=cmd_task)

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

    claim = subparsers.add_parser("claim")
    claim.add_argument("--project", required=True)
    claim.set_defaults(func=cmd_claim)

    control = subparsers.add_parser("control")
    control.add_argument("--project", required=True)
    control.add_argument("--goal", choices=["auto", "status", "pause", "repair", "step"], default="auto")
    control.add_argument("--requirements")
    control.add_argument("--task-id")
    control.add_argument("--runtime")
    control.add_argument("--hermes-bin", default=os.environ.get("APP_DELIVERY_HERMES_BIN", "hermes"))
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
    fix.add_argument("--no-run", dest="no_start", action="store_true")
    fix.add_argument("--background", action="store_true")
    fix.set_defaults(func=cmd_fix)

    status = subparsers.add_parser("status")
    status.add_argument("--project", required=True)
    status.set_defaults(func=cmd_status)

    watch_status = subparsers.add_parser("watch-status")
    watch_status.add_argument("--project", required=True)
    watch_status.add_argument("--interval-seconds", type=int, default=30)
    watch_status.add_argument("--max-failures", type=int, default=3)
    watch_status.set_defaults(func=cmd_watch_status)

    watchdog_run = subparsers.add_parser("watchdog-run")
    watchdog_run.add_argument("--project", required=True)
    watchdog_run.add_argument("--interval-seconds", type=int, default=30)
    watchdog_run.set_defaults(func=cmd_watchdog_run)

    host_fallback = subparsers.add_parser("host-fallback")
    host_fallback.add_argument("--project", required=True)
    host_fallback.set_defaults(func=cmd_host_fallback)

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