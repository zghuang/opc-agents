from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_plane_host import build_planning_host_step, build_review_host_step
from .errors import DeliveryError
from .loop import PAUSE_FILE
from .loop_reporting import status as runtime_status
from .project_readiness import (
    architecture_ready,
    blocking_clarifications_present,
    context_ready,
    discover_requirements_source,
    needs_scaffold,
    requirements_ready,
    ui_ready,
    ui_required,
    work_item_contract_errors,
    work_items_ready,
)
from .review_artifacts import code_review_request_path, final_review_request_path
from .runtime_config import resolve_project_root
from .state import load_json, load_task_runtime_state, normalize_task_runtime_state, project_paths, write_json
from .task import FINAL_VERIFY_TASK_ID, all_tasks


CONTROL_GOALS = {"auto", "status", "pause", "repair", "step"}
FINAL_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"


@dataclass(frozen=True)
class ControlStep:
    kind: str
    action: str
    owner: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "owner": self.owner,
            **self.payload,
        }


def _step(kind: str, action: str, owner: str, **payload: Any) -> ControlStep:
    return ControlStep(kind=kind, action=action, owner=owner, payload=payload)


def _host_input_matches_latest_runtime_attempt(project_root: Path, task_id: str, input_path: Path) -> bool:
    runtime_state = load_task_runtime_state(project_root, task_id)
    completed_at = _parse_iso_datetime(runtime_state.get("completed_at"))
    if completed_at is None:
        return False
    try:
        input_mtime = dt.datetime.fromtimestamp(input_path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return False
    return input_mtime > completed_at


def _host_input_import_ready(project_root: Path, next_step: dict[str, Any]) -> bool:
    expected_input = str(next_step.get("expected_input_path") or "").strip()
    if not expected_input:
        return False
    input_path = Path(expected_input).expanduser()
    if not input_path.exists():
        return False

    skill = str(next_step.get("skill") or "").strip()
    task_id = str(next_step.get("task_id") or "").strip()
    if skill == "code-review" and task_id:
        request_path = code_review_request_path(project_root, task_id)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns:
            return _host_input_matches_latest_runtime_attempt(project_root, task_id, input_path)
    if skill == "final-review":
        request_path = final_review_request_path(project_root)
        if request_path.exists() and input_path.stat().st_mtime_ns <= request_path.stat().st_mtime_ns:
            return False
    return True


def _annotate_host_response_readiness(project_root: Path, next_step: dict[str, Any] | None) -> None:
    if not isinstance(next_step, dict):
        return
    if str(next_step.get("owner") or "").strip() != "host":
        return
    expected_input = str(next_step.get("expected_input_path") or "").strip()
    if not expected_input:
        return
    if _host_input_import_ready(project_root, next_step):
        next_step["host_response_ready"] = True
        next_step["host_response_status"] = "ready_unimported"
        next_step["message"] = (
            f"Host artifact already exists at {expected_input}; run control --goal auto to import it before requesting host work again."
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


def _planning_blocker_code(project_root: Path) -> str | None:
    if not requirements_ready(project_root):
        return "requirements_missing"
    if blocking_clarifications_present(project_root):
        return "clarification_blocking"
    if not architecture_ready(project_root):
        return "architecture_missing"
    if ui_required(project_root) and not ui_ready(project_root):
        return "ui_design_missing"
    if not context_ready(project_root):
        return "context_missing"
    if not work_items_ready(project_root):
        return "work_items_missing"
    if work_item_contract_errors(project_root):
        return "work_items_contract_invalid"
    return None


def _auto_exception_repair_state_path(project_root: Path) -> Path:
    return project_paths(project_root).runtime_dir / "auto-exception-repair.json"


def _auto_exception_repair_state(project_root: Path) -> dict[str, Any]:
    payload = load_json(_auto_exception_repair_state_path(project_root), {})
    return payload if isinstance(payload, dict) else {}


def mark_auto_exception_repair_attempted(project_root: Path | str, task_id: str) -> None:
    project_dir = resolve_project_root(project_root)
    state = _auto_exception_repair_state(project_dir)
    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
    task_label = str(task_id or "").strip()
    attempts[task_label] = {"attempted_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    state["attempts"] = attempts
    write_json(_auto_exception_repair_state_path(project_dir), state)


def _auto_exception_repair_already_attempted(project_root: Path, task_id: str) -> bool:
    attempts = _auto_exception_repair_state(project_root).get("attempts")
    return isinstance(attempts, dict) and str(task_id or "").strip() in attempts


def _exception_blocking_pending_ids(tasks: list[Any], exception_id: str) -> list[str]:
    by_id = {task.id: task for task in tasks}

    def depends_on_exception(task_id: str, seen: set[str]) -> bool:
        if task_id in seen:
            return False
        seen.add(task_id)
        task = by_id.get(task_id)
        if task is None:
            return False
        for dependency_id in task.dependencies:
            if dependency_id == exception_id:
                return True
            dependency = by_id.get(dependency_id)
            if dependency is not None and depends_on_exception(dependency.id, seen):
                return True
        return False

    return [task.id for task in tasks if task.status == "pending" and task.id != FINAL_VERIFY_TASK_ID and depends_on_exception(task.id, set())]


def _auto_exception_repair_candidate(project_root: Path, tasks: list[Any]) -> tuple[Any, list[str]] | None:
    exceptions = [task for task in tasks if task.status == "exception"]
    pending = [task for task in tasks if task.status == "pending" and task.id != FINAL_VERIFY_TASK_ID]
    if not exceptions or not pending:
        return None
    fallback_candidate: tuple[Any, list[str]] | None = None
    for task in exceptions:
        if _auto_exception_repair_already_attempted(project_root, task.id):
            continue
        blocked = _exception_blocking_pending_ids(tasks, task.id)
        if blocked and len(blocked) == len(pending):
            return task, blocked
        if blocked and fallback_candidate is None:
            fallback_candidate = (task, blocked)
    return fallback_candidate


def _gate_exception_repair_candidate(project_root: Path, tasks_by_id: dict[str, Any], repair_candidates: list[str]) -> Any | None:
    for task_id in repair_candidates:
        task = tasks_by_id.get(task_id)
        if task is None or task.status != "exception":
            continue
        if _auto_exception_repair_already_attempted(project_root, task.id):
            continue
        return task
    return None


def _is_final_repair_task_payload(task: dict[str, Any]) -> bool:
    title = str(task.get("title") or "").strip()
    return (
        title.startswith(FINAL_REPAIR_TASK_PREFIX)
        or title.startswith(FINAL_REVIEW_REPAIR_TASK_PREFIX)
    )


def _final_repair_limit_blocks_continuation(task: dict[str, Any], final_runtime_state: dict[str, Any]) -> bool:
    return _is_final_repair_task_payload(task) and bool(final_runtime_state.get("final_repair_limit_reached"))


def routed_status(project_root: Path | str, *, requirements_path: str | None = None) -> dict[str, Any]:
    project_dir = resolve_project_root(project_root)
    base = runtime_status(project_dir)
    tasks = all_tasks(project_dir)
    invalid_verified = base.get("invalid_verified_tasks") if isinstance(base.get("invalid_verified_tasks"), dict) else {}
    gates_payload = base.get("gates") if isinstance(base.get("gates"), dict) else {}
    gate_rows = gates_payload.get("gates") if isinstance(gates_payload.get("gates"), list) else []
    blocked_gates = [gate for gate in gate_rows if isinstance(gate, dict) and str(gate.get("status") or "").strip() == "blocked"]
    task_by_id = {task.id: task for task in tasks}
    actionable = [
        task
        for task in tasks
        if task.id != FINAL_VERIFY_TASK_ID and task.status != "cancelled" and getattr(task, "task_kind", "feature") != "repair"
    ]
    all_actionable_verified = bool(actionable) and all(task.status == "verified" for task in actionable)
    final_task = task_by_id.get(FINAL_VERIFY_TASK_ID)
    final_task_status = final_task.status if final_task is not None else None
    final_runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_dir, FINAL_VERIFY_TASK_ID))
    blocker_code = _planning_blocker_code(project_dir)
    contract_errors = work_item_contract_errors(project_dir) if blocker_code == "work_items_contract_invalid" else {}
    archived_requirements = discover_requirements_source(project_dir, explicit_path=requirements_path)

    next_step: dict[str, Any] | None = None
    must_continue = False
    control_status = "blocked"
    delivery_claim_allowed = False

    if (project_dir / PAUSE_FILE).exists():
        control_status = "paused"
    elif blocker_code is not None:
        next_step = build_planning_host_step(
            code=blocker_code,
            project_root=project_dir,
            requirements_path=str(archived_requirements) if archived_requirements is not None else requirements_path,
            contract_errors=contract_errors,
        )
        requires_user_input = bool(next_step.get("requires_user_input")) if isinstance(next_step, dict) else False
        must_continue = bool(next_step is not None and not requires_user_input)
        control_status = "blocked" if next_step is None or requires_user_input else "in_progress"
    elif isinstance(base.get("review_pending_task"), dict):
        task = base["review_pending_task"]
        next_step = build_review_host_step(
            project_root=project_dir,
            task_id=str(task.get("id") or "").strip(),
            title=str(task.get("title") or "").strip(),
        )
        must_continue = next_step is not None
        control_status = "in_progress" if must_continue else "blocked"
    elif isinstance(base.get("active_task"), dict):
        task = base["active_task"]
        runtime_state = task.get("runtime_state") if isinstance(task.get("runtime_state"), dict) else {}
        runtime_state_status = str(runtime_state.get("status") or "").strip()
        runtime_completed_at = str(runtime_state.get("completed_at") or "").strip()
        runtime_attention = base.get("runtime_attention") if isinstance(base.get("runtime_attention"), dict) else None
        if _final_repair_limit_blocks_continuation(task, final_runtime_state):
            next_step = None
            must_continue = False
            control_status = "blocked"
        elif runtime_attention and runtime_attention.get("suspected"):
            next_step = _step(
                "framework",
                "recover_stalled",
                "framework",
                message=str(runtime_attention.get("message") or "Runtime appears stalled; recover the current task."),
                project=str(project_dir),
                task_id=str(task.get("id") or "").strip(),
                attention_kind=str(runtime_attention.get("kind") or "").strip() or None,
            ).to_dict()
            must_continue = True
            control_status = "in_progress"
        elif runtime_state_status in {"completed", "failed", "interrupted"}:
            if runtime_state_status == "completed":
                message = (
                    f"Continue framework validation and repair flow for {task.get('id')}; "
                    f"the last runtime attempt already completed{(' at ' + runtime_completed_at) if runtime_completed_at else ''}."
                )
            elif runtime_state_status == "failed":
                message = f"Resume repair flow for {task.get('id')}; the last runtime attempt failed and left the task pending continuation."
            else:
                message = f"Resume the active task {task.get('id')}; the last runtime attempt was interrupted and left the task pending continuation."
            next_step = _step(
                "framework",
                "run_resume",
                "framework",
                message=message,
                project=str(project_dir),
                task_id=str(task.get("id") or "").strip(),
                task_title=str(task.get("title") or "").strip(),
                runtime_status=runtime_state_status,
                runtime_completed_at=runtime_completed_at or None,
            ).to_dict()
            must_continue = True
            control_status = "in_progress"
        else:
            next_step = None
            must_continue = False
            control_status = "running"
    elif final_task_status == "blocked" and all_actionable_verified:
        repair_task_id = str(final_runtime_state.get("repair_task_id") or "").strip()
        repair_task = task_by_id.get(repair_task_id) if repair_task_id else None
        repair_candidates = [
            str(task_id).strip()
            for task_id in final_runtime_state.get("repair_candidates", [])
            if str(task_id).strip()
        ]
        repair_report = str(final_runtime_state.get("repair_report_artifact") or "").strip() or None
        if repair_task is not None and repair_task.status == "exception":
            next_step = None
            must_continue = False
            control_status = "blocked"
        elif repair_task is not None and repair_task.status != "verified":
            next_step = _step(
                "framework",
                "run_resume",
                "framework",
                message=f"Final verification failed; continue dedicated repair task {repair_task.id} without reopening verified tasks.",
                project=str(project_dir),
                task_id=repair_task.id,
                task_title=repair_task.title,
                repair_candidates=repair_candidates,
                repair_task_id=repair_task.id,
                repair_report=repair_report,
            ).to_dict()
            must_continue = True
            control_status = "in_progress"
        elif repair_task is not None and repair_task.status == "verified" and str(final_runtime_state.get("final_verify_status") or "").strip() == "repair_required":
            repair_verified_at = _parse_iso_datetime(getattr(repair_task, "verified_at", None))
            final_updated_at = _parse_iso_datetime(final_runtime_state.get("updated_at"))
            if repair_verified_at is not None and (final_updated_at is None or final_updated_at < repair_verified_at):
                next_step = _step(
                    "framework",
                    "run_final_verify",
                    "framework",
                    message="The dedicated repair task is verified; rerun final verification to refresh release status.",
                    project=str(project_dir),
                    repair_candidates=repair_candidates,
                    repair_task_id=repair_task.id,
                    repair_report=repair_report,
                ).to_dict()
                must_continue = True
                control_status = "in_progress"
            else:
                next_step = None
                must_continue = False
                control_status = "blocked"
        else:
            next_step = None
            must_continue = False
            control_status = "blocked"
    elif needs_scaffold(project_dir):
        next_step = _step(
            "framework",
            "run_scaffold",
            "framework",
            message="Project is fully planned but scaffold has not been verified yet.",
            project=str(project_dir),
        ).to_dict()
        must_continue = True
        control_status = "in_progress"
    elif bool(base.get("final_verify_ready")):
        next_step = _step(
            "framework",
            "run_final_verify",
            "framework",
            message="All actionable tasks are verified; run final verification.",
            project=str(project_dir),
        ).to_dict()
        must_continue = True
        control_status = "in_progress"
    elif isinstance(base.get("next_task"), dict) and str(base["next_task"].get("id") or "").strip() != FINAL_VERIFY_TASK_ID:
        task = base["next_task"]
        runtime_state = task.get("runtime_state") if isinstance(task.get("runtime_state"), dict) else {}
        runtime_state_status = str(runtime_state.get("status") or "").strip()
        runtime_completed_at = str(runtime_state.get("completed_at") or "").strip()
        if _final_repair_limit_blocks_continuation(task, final_runtime_state):
            next_step = None
            must_continue = False
            control_status = "blocked"
        elif runtime_state_status == "completed":
            message = (
                f"Continue framework validation and repair flow for {task.get('id')}; "
                f"the last runtime attempt already completed{(' at ' + runtime_completed_at) if runtime_completed_at else ''}."
            )
        elif runtime_state_status == "failed":
            message = (
                f"Resume repair flow for {task.get('id')}; the last runtime attempt failed and left the task pending continuation."
            )
        else:
            message = f"Continue the next runnable task {task.get('id')} through the implementation loop."
        next_step = _step(
            "framework",
            "run_resume",
            "framework",
            message=message,
            project=str(project_dir),
            task_id=str(task.get("id") or "").strip(),
            task_title=str(task.get("title") or "").strip(),
            runtime_status=runtime_state_status or None,
            runtime_completed_at=runtime_completed_at or None,
        ).to_dict()
        must_continue = True
        control_status = "in_progress"
    elif blocked_gates:
        gate = blocked_gates[0]
        repair_candidates = [
            str(task_id).strip()
            for task_id in gate.get("repair_candidates", [])
            if str(task_id).strip()
        ]
        repair_report = str(gate.get("report_artifact") or "").strip() or None
        if repair_candidates:
            gate_id = str(gate.get("id") or "").strip() or None
            blocked_reason = str(gate.get("blocked_reason") or "").strip() or None
            exception_repair_task = _gate_exception_repair_candidate(project_dir, task_by_id, repair_candidates)
            if exception_repair_task is not None:
                next_step = _step(
                    "framework",
                    "auto_repair_exception",
                    "framework",
                    message=f"Validation gate {gate_id} is blocked by exception task {exception_repair_task.id}; reapply its exception patch and retry once.",
                    project=str(project_dir),
                    task_id=exception_repair_task.id,
                    task_title=exception_repair_task.title,
                    repair_candidates=repair_candidates,
                    repair_report=repair_report,
                    gate_id=gate_id,
                    blocked_reason=blocked_reason,
                ).to_dict()
            else:
                auto_repair = _auto_exception_repair_candidate(project_dir, tasks)
                if auto_repair is not None:
                    exception_task, blocked_pending_ids = auto_repair
                    next_step = _step(
                        "framework",
                        "auto_repair_exception",
                        "framework",
                        message=f"Validation gate {gate_id} is blocked while exception task {exception_task.id} blocks pending continuation; reapply its exception patch and retry once.",
                        project=str(project_dir),
                        task_id=exception_task.id,
                        task_title=exception_task.title,
                        blocked_pending_task_ids=blocked_pending_ids,
                        repair_candidates=repair_candidates,
                        repair_report=repair_report,
                        gate_id=gate_id,
                        blocked_reason=blocked_reason,
                    ).to_dict()
                else:
                    next_step = _step(
                        "host_notice",
                        "review_gate_blocker",
                        "host",
                        message=f"Validation gate {gate.get('id')} found unresolved evidence. Review the gate report and decide whether to trigger a repair task.",
                        project=str(project_dir),
                        task_id=repair_candidates[0],
                        repair_candidates=repair_candidates,
                        repair_report=repair_report,
                        gate_id=gate_id,
                        blocked_reason=blocked_reason,
                    ).to_dict()
            must_continue = True
            control_status = "in_progress"
        else:
            gate_id = str(gate.get("id") or "").strip() or None
            blocked_reason = str(gate.get("blocked_reason") or "").strip() or None
            auto_repair = _auto_exception_repair_candidate(project_dir, tasks)
            if auto_repair is not None:
                exception_task, blocked_pending_ids = auto_repair
                next_step = _step(
                    "framework",
                    "auto_repair_exception",
                    "framework",
                    message=f"Validation gate {gate_id} is blocked while exception task {exception_task.id} blocks pending continuation; reapply its exception patch and retry once.",
                    project=str(project_dir),
                    task_id=exception_task.id,
                    task_title=exception_task.title,
                    blocked_pending_task_ids=blocked_pending_ids,
                    repair_candidates=repair_candidates,
                    repair_report=repair_report,
                    gate_id=gate_id,
                    blocked_reason=blocked_reason,
                ).to_dict()
                must_continue = True
                control_status = "in_progress"
            else:
                next_step = None
                must_continue = False
                control_status = "blocked"
    elif isinstance(base.get("next_task"), dict):
        task = base["next_task"]
        runtime_state = task.get("runtime_state") if isinstance(task.get("runtime_state"), dict) else {}
        runtime_state_status = str(runtime_state.get("status") or "").strip()
        runtime_completed_at = str(runtime_state.get("completed_at") or "").strip()
        if runtime_state_status == "completed":
            message = (
                f"Continue framework validation and repair flow for {task.get('id')}; "
                f"the last runtime attempt already completed{(' at ' + runtime_completed_at) if runtime_completed_at else ''}."
            )
        elif runtime_state_status == "failed":
            message = (
                f"Resume repair flow for {task.get('id')}; the last runtime attempt failed and left the task pending continuation."
            )
        else:
            message = f"Continue the next runnable task {task.get('id')} through the implementation loop."
        next_step = _step(
            "framework",
            "run_resume",
            "framework",
            message=message,
            project=str(project_dir),
            task_id=str(task.get("id") or "").strip(),
            task_title=str(task.get("title") or "").strip(),
            runtime_status=runtime_state_status or None,
            runtime_completed_at=runtime_completed_at or None,
        ).to_dict()
        must_continue = True
        control_status = "in_progress"
    else:
        auto_repair = _auto_exception_repair_candidate(project_dir, tasks)
        if auto_repair is not None:
            exception_task, blocked_pending_ids = auto_repair
            next_step = _step(
                "framework",
                "auto_repair_exception",
                "framework",
                message=f"All pending tasks are blocked by exception task {exception_task.id}; reapply its exception patch and retry once.",
                project=str(project_dir),
                task_id=exception_task.id,
                task_title=exception_task.title,
                blocked_pending_task_ids=blocked_pending_ids,
            ).to_dict()
            must_continue = True
            control_status = "in_progress"
        else:
            actionable_counts = base.get("counts") if isinstance(base.get("counts"), dict) else {}
            any_actionable = any(
                actionable_counts.get(key, 0)
                for key in ["pending", "active", "review_pending", "blocked", "exception"]
            )
            delivery_claim_allowed = final_task_status == "verified" and not any_actionable
            control_status = "complete" if delivery_claim_allowed else "blocked"

    _annotate_host_response_readiness(project_dir, next_step)

    continue_instruction = ""
    if must_continue and isinstance(next_step, dict):
        owner = str(next_step.get("owner") or "framework")
        action = str(next_step.get("action") or "")
        target = str(next_step.get("task_id") or "").strip()
        detail = f", task_id={target}" if target else ""
        continue_instruction = (
            f"Do not stop or ask whether to continue. Immediately execute next_step: owner={owner}, action={action}{detail}. "
            "Stop only if that deterministic action fails or requires a genuine human decision."
        )

    waiting_for: dict[str, Any] | None = None
    blocking_condition: dict[str, Any] | None = None
    if not must_continue:
        if control_status == "running" and isinstance(base.get("active_task"), dict):
            active = base["active_task"]
            task_id = str(active.get("id") or "").strip()
            phase = str(active.get("phase") or "implementation").strip() or "implementation"
            waiting_for = {
                "kind": "active_task_completion",
                "task_id": task_id or None,
                "phase": phase,
                "message": f"Waiting for active task {task_id or '<unknown>'} completion.",
            }
        elif control_status == "blocked" and next_step is None:
            blocking_condition = {
                "kind": "no_routable_next_step",
                "message": "No deterministic framework or host next_step is currently routable from this state.",
            }

    return {
        **base,
        "control_status": control_status,
        "delivery_claim_allowed": delivery_claim_allowed,
        "next_step": next_step,
        "must_continue": must_continue,
        "waiting_for": waiting_for,
        "blocking_condition": blocking_condition,
        "continue_instruction": continue_instruction,
        "planning_blocker_code": blocker_code,
        "requirements_path": str(archived_requirements) if archived_requirements is not None else None,
    }


def run_control(
    project_root: Path | str,
    *,
    goal: str,
    requirements_path: str | None = None,
    repair_task_id: str | None = None,
) -> dict[str, Any]:
    normalized_goal = str(goal or "").strip().lower()
    if normalized_goal not in CONTROL_GOALS:
        raise DeliveryError(
            code="control_goal_invalid",
            message=f"unsupported control goal: {goal}",
            exit_code=2,
            details={"supported_goals": sorted(CONTROL_GOALS)},
        )

    project_dir = resolve_project_root(project_root)
    if normalized_goal == "pause":
        (project_dir / PAUSE_FILE).write_text("paused\n", encoding="utf-8")
        return routed_status(project_dir, requirements_path=requirements_path)

    if normalized_goal == "repair":
        if not repair_task_id:
            raise DeliveryError(
                code="repair_task_missing",
                message="repair goal requires --task-id",
                exit_code=2,
            )
        return {
            **routed_status(project_dir, requirements_path=requirements_path),
            "requested_repair_task_id": repair_task_id,
        }

    snapshot = routed_status(project_dir, requirements_path=requirements_path)
    if normalized_goal == "status":
        return snapshot
    return snapshot
