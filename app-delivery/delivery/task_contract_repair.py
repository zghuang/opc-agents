from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from .task import Task, next_generated_task_id


class TaskContractRepairError(ValueError):
    pass


class TaskContractRepairBlocked(RuntimeError):
    pass


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _load_requirements_payload(project_root: Path) -> dict[str, Any]:
    path = project_root / "docs" / "requirements.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _acceptance_source_requirements(project_root: Path, scenario_id: str) -> list[str]:
    payload = _load_requirements_payload(project_root)
    rows = payload.get("acceptance_scenarios") if isinstance(payload.get("acceptance_scenarios"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "").strip() != scenario_id:
            continue
        return [str(value).strip() for value in row.get("source_requirement_ids", []) if str(value).strip()]
    return []


def _candidate_indices(tasks: list[Task]) -> dict[str, int]:
    return {task.id: index for index, task in enumerate(tasks)}


def _explicit_target(tasks: list[Task], source_task: Task, operation: dict[str, Any]) -> Task | None:
    target_id = str(operation.get("to_task_id") or "").strip()
    if not target_id:
        return None
    target = next((task for task in tasks if task.id == target_id), None)
    if target is None:
        return None
    if target.id == source_task.id:
        raise TaskContractRepairError("target task must differ from source task")
    return target


def _target_for_operation(tasks: list[Task], source_task: Task, operation: dict[str, Any]) -> Task | None:
    return _explicit_target(tasks, source_task, operation)


def _ensure_target_mutable(target: Task) -> None:
    if target.status in {"verified", "cancelled", "exception", "active"}:
        raise TaskContractRepairBlocked(f"target task {target.id} is {target.status}; create a follow-up task instead of mutating it")
    if target.task_kind == "repair":
        raise TaskContractRepairBlocked(f"target task {target.id} is a repair task and cannot own moved acceptance scenarios")


def _minimal_followup_contract(operation: dict[str, Any], scenario_id: str, requirement_ids: list[str]) -> dict[str, Any]:
    followup = operation.get("followup_task") if isinstance(operation.get("followup_task"), dict) else {}
    title = str(followup.get("title") or f"Acceptance Scenario Follow-up: {scenario_id}").strip()
    output_tests = [str(value).strip() for value in followup.get("output_tests", []) if str(value).strip()]
    output_paths = [str(value).strip() for value in followup.get("output_paths", []) if str(value).strip()]
    if not output_tests or not output_paths:
        raise TaskContractRepairBlocked(
            f"no safe target task for {scenario_id}, and followup_task must provide non-empty output_tests and output_paths"
        )
    requirements = _dedupe([str(value).strip() for value in followup.get("requirements", []) if str(value).strip()] + requirement_ids)
    dependencies = [str(value).strip() for value in followup.get("dependencies", []) if str(value).strip()]
    task_kind = str(followup.get("task_kind") or "feature").strip() or "feature"
    intent = followup.get("intent") if isinstance(followup.get("intent"), dict) else {}
    return {
        "title": title,
        "task_kind": task_kind,
        "requirements": requirements,
        "acceptance_scenarios": [scenario_id],
        "dependencies": dependencies,
        "output_tests": output_tests,
        "output_paths": output_paths,
        "intent": intent,
    }


def _insert_followup_task(tasks: list[Task], source_task: Task, contract: dict[str, Any]) -> tuple[list[Task], str]:
    task_id = next_generated_task_id(tasks)
    dependencies = list(contract.get("dependencies", [])) or [source_task.id]
    followup = Task.from_dict({**contract, "id": task_id, "status": "pending", "dependencies": dependencies, "attempts": 0})
    inserted = False
    updated: list[Task] = []
    for task in tasks:
        if task.id in {FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID} and not inserted:
            updated.append(followup)
            inserted = True
        if task.id in {FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID} and task.status not in {"verified", "cancelled", "exception"}:
            data = task.to_dict()
            data["dependencies"] = _dedupe([*data.get("dependencies", []), task_id])
            updated.append(Task.from_dict(data))
        else:
            updated.append(task)
    if not inserted:
        updated.append(followup)
    return updated, task_id


def _operation_acceptance_ids(source_task: Task, operation: dict[str, Any]) -> list[str]:
    if str(operation.get("op") or "").strip() != "move_acceptance_scenario":
        return []
    scenario_id = str(operation.get("id") or "").strip()
    if scenario_id and scenario_id in source_task.acceptance_scenarios:
        return [scenario_id]
    return []


def acceptance_ids_for_contract_repair(source_task: Task, assessment: dict[str, Any]) -> list[str]:
    operation_ids = [
        scenario_id
        for operation in assessment.get("operations", [])
        if isinstance(operation, dict)
        for scenario_id in _operation_acceptance_ids(source_task, operation)
    ]
    affected_ids = [str(value).strip() for value in assessment.get("affected_acceptance_ids", []) if str(value).strip()]
    return _dedupe([*operation_ids, *(scenario_id for scenario_id in affected_ids if scenario_id in source_task.acceptance_scenarios)])


def defer_acceptance_scenarios(
    tasks: list[Task],
    source_task: Task,
    assessment: dict[str, Any],
    *,
    reason: str,
    review_artifact: str,
    created_at: str,
) -> tuple[list[Task], dict[str, Any]]:
    scenario_ids = acceptance_ids_for_contract_repair(source_task, assessment)
    if not scenario_ids:
        raise TaskContractRepairError("task contract deferral requires at least one source acceptance scenario")
    records = [
        {
            "id": scenario_id,
            "from_task": source_task.id,
            "reason": reason,
            "review_artifact": review_artifact,
            "policy": "delivery_continue",
            "created_at": created_at,
            "to_task_hint": next(
                (
                    str(operation.get("to_task_hint") or "").strip()
                    for operation in assessment.get("operations", [])
                    if isinstance(operation, dict) and str(operation.get("id") or "").strip() == scenario_id
                ),
                "",
            ) or None,
        }
        for scenario_id in scenario_ids
    ]
    updated: list[Task] = []
    for task in tasks:
        data = task.to_dict()
        if task.id == source_task.id:
            data["acceptance_scenarios"] = [scenario_id for scenario_id in task.acceptance_scenarios if scenario_id not in set(scenario_ids)]
        updated.append(Task.from_dict(data))
    return updated, {"deferred_acceptance_scenarios": records}


def _apply_move_acceptance_scenario(
    project_root: Path,
    tasks: list[Task],
    source_task: Task,
    operation: dict[str, Any],
) -> tuple[list[Task], dict[str, Any]]:
    scenario_id = str(operation.get("id") or "").strip()
    if not scenario_id:
        raise TaskContractRepairError("move_acceptance_scenario operation requires id")
    from_task = str(operation.get("from_task") or source_task.id).strip()
    if from_task != source_task.id:
        raise TaskContractRepairError(f"move_acceptance_scenario from_task must be {source_task.id}")
    if scenario_id not in source_task.acceptance_scenarios:
        raise TaskContractRepairError(f"{scenario_id} is not declared on source task {source_task.id}")
    requirement_ids = _dedupe(
        [str(value).strip() for value in operation.get("requirement_ids", []) if str(value).strip()]
        + _acceptance_source_requirements(project_root, scenario_id)
    )
    target = _target_for_operation(tasks, source_task, operation)
    index_by_id = _candidate_indices(tasks)
    source_index = index_by_id.get(source_task.id, -1)
    if target is not None:
        if index_by_id.get(target.id, -1) <= source_index:
            target = None
        elif target.status in {"verified", "cancelled", "exception", "active"} or target.task_kind == "repair":
            target = None
        else:
            _ensure_target_mutable(target)
            updated: list[Task] = []
            for task in tasks:
                data = task.to_dict()
                if task.id == source_task.id:
                    data["acceptance_scenarios"] = [value for value in task.acceptance_scenarios if value != scenario_id]
                elif task.id == target.id:
                    data["acceptance_scenarios"] = _dedupe([*task.acceptance_scenarios, scenario_id])
                    data["requirements"] = _dedupe([*task.requirements, *requirement_ids])
                    data["status"] = "pending"
                    data["review_status"] = None
                    data["review_artifact"] = None
                    data["reviewed_at"] = None
                    data["verified_at"] = None
                    data["blocked_reason"] = f"task contract received moved acceptance scenario {scenario_id} from {source_task.id}"
                updated.append(Task.from_dict(data))
            return updated, {"operation": "move_acceptance_scenario", "acceptance_id": scenario_id, "target_task_id": target.id, "mode": "moved_to_existing_task"}

    contract = _minimal_followup_contract(operation, scenario_id, requirement_ids)
    without_scenario: list[Task] = []
    for task in tasks:
        data = task.to_dict()
        if task.id == source_task.id:
            data["acceptance_scenarios"] = [value for value in task.acceptance_scenarios if value != scenario_id]
        without_scenario.append(Task.from_dict(data))
    updated, followup_id = _insert_followup_task(without_scenario, source_task, contract)
    return updated, {"operation": "move_acceptance_scenario", "acceptance_id": scenario_id, "target_task_id": followup_id, "mode": "created_followup_task"}


def apply_task_contract_repair(
    project_root: Path | str,
    tasks: list[Task],
    source_task: Task,
    assessment: dict[str, Any],
) -> tuple[list[Task], dict[str, Any]]:
    project_dir = Path(project_root).expanduser().resolve()
    operations = assessment.get("operations") if isinstance(assessment.get("operations"), list) else []
    if not operations:
        raise TaskContractRepairBlocked("task contract repair requested without operations")
    updated = list(tasks)
    applied: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise TaskContractRepairError("task contract operation must be an object")
        op_name = str(operation.get("op") or "").strip()
        current_source = next((task for task in updated if task.id == source_task.id), None)
        if current_source is None:
            raise TaskContractRepairError(f"source task disappeared during contract repair: {source_task.id}")
        if op_name == "move_acceptance_scenario":
            try:
                updated, result = _apply_move_acceptance_scenario(project_dir, updated, current_source, operation)
            except TaskContractRepairBlocked as exc:
                raise TaskContractRepairBlocked(str(exc)) from exc
            applied.append(result)
            continue
        raise TaskContractRepairError(f"unsupported task contract operation: {op_name or '<empty>'}")
    return updated, {"applied_operations": applied}