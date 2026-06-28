from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .state import load_gates, load_test_results, save_gates, utc_now_iso
from .task import FINAL_VERIFY_TASK_ID, SHARED_FOUNDATION_TASK_ID, SCAFFOLD_TASK_ID, Task, all_tasks
from .verify import infer_test_types


def normalize_complexity_override(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("task-decompose payload must include a delivery_complexity object")
    tier = str(payload.get("tier") or "").strip().upper()
    if tier not in {"S", "M", "L", "XL"}:
        raise ValueError("delivery_complexity.tier must be one of S/M/L/XL")
    signals = payload.get("signals") if isinstance(payload.get("signals"), dict) else {}
    normalized = {
        "tier": tier,
        "score": int(payload.get("score") or 0),
        "signals": signals,
    }
    rationale = str(payload.get("rationale") or "").strip()
    if rationale:
        normalized["rationale"] = rationale
    normalized["source"] = "task-decompose"
    return normalized


def normalize_stage_gates(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("task-decompose payload must include validation_gates as a list")
    normalized: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        gate_id = str(row.get("id") or "").strip()
        kind = str(row.get("kind") or "module").strip()
        title = str(row.get("title") or gate_id or "Validation gate").strip()
        if not gate_id:
            continue
        normalized.append(
            {
                "id": gate_id,
                "kind": kind,
                "title": title,
                "status": str(row.get("status") or "pending").strip() or "pending",
                "scope_tasks": [str(value).strip() for value in row.get("scope_tasks", []) if str(value).strip()],
                "scope_requirements": [str(value).strip() for value in row.get("scope_requirements", []) if str(value).strip()],
                "required_test_types": [str(value).strip() for value in row.get("required_test_types", []) if str(value).strip()],
                "source": "task-decompose",
            }
        )
    return normalized


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _safe_gate_suffix(gate_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(gate_id or "").strip())
    return normalized or "gate"


def _gate_report_path(project_root: Path | str, gate_id: str) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    reviews_dir = project_dir / "docs" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    return reviews_dir / f"gate-report-{_safe_gate_suffix(gate_id)}.md"


def _gate_scope_requirements(gate: dict[str, Any], scope_rows: list[Task]) -> list[str]:
    return _dedupe_strings(
        [str(value).strip() for value in gate.get("scope_requirements", []) if str(value).strip()]
        + [requirement_id for task in scope_rows for requirement_id in task.requirements]
    )


def _gate_result_lane_key(row: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    task_id = str(row.get("task_id") or "").strip()
    test_types = tuple(_normalized_result_test_types(row))
    requirement_ids = tuple(sorted(str(value).strip() for value in row.get("requirement_ids", []) if str(value).strip()))
    return task_id, test_types, requirement_ids


def _normalized_result_test_types(row: dict[str, Any]) -> list[str]:
    explicit = [str(value).strip() for value in row.get("test_types", []) if str(value).strip()]
    inferred = infer_test_types([str(value).strip() for value in row.get("test_files", []) if str(value).strip()])
    return sorted({*explicit, *inferred})


def _matched_gate_results(
    results: list[dict[str, Any]],
    *,
    scope_task_ids: set[str],
    scope_requirement_ids: set[str],
) -> list[dict[str, Any]]:
    latest_by_lane: dict[tuple[str, tuple[str, ...], tuple[str, ...]], dict[str, Any]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("task_id") or "").strip()
        requirement_ids = {
            str(value).strip()
            for value in row.get("requirement_ids", [])
            if str(value).strip()
        }
        if task_id in scope_task_ids or bool(scope_requirement_ids.intersection(requirement_ids)):
            lane_key = _gate_result_lane_key(row)
            if lane_key in latest_by_lane:
                latest_by_lane.pop(lane_key)
            latest_by_lane[lane_key] = row
    return list(latest_by_lane.values())


def _gate_has_scope_binding(gate: dict[str, Any]) -> bool:
    scope_tasks = [str(value).strip() for value in gate.get("scope_tasks", []) if str(value).strip()]
    scope_requirements = [str(value).strip() for value in gate.get("scope_requirements", []) if str(value).strip()]
    return bool(scope_tasks or scope_requirements)


def _gate_repair_candidates(
    gate: dict[str, Any],
    tasks: list[Task],
    *,
    scope_rows: list[Task],
    scope_requirement_ids: set[str],
    matched_results: list[dict[str, Any]],
    missing_test_types: list[str],
    deferred_provider_ids: set[str] | None = None,
) -> list[str]:
    by_id = {task.id: task for task in tasks}
    candidates: list[str] = []
    provider_ids = set(deferred_provider_ids or set())

    def add(task_id: str) -> None:
        task = by_id.get(str(task_id).strip())
        if task is None:
            return
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}:
            return
        if task.id in candidates:
            return
        candidates.append(task.id)

    for task in scope_rows:
        if provider_ids and missing_test_types and task.status in {"review_pending", "pending", "active"}:
            continue
        if task.status in {"blocked", "exception", "review_pending", "pending", "active"}:
            add(task.id)
    for row in matched_results:
        if not bool(row.get("passed")):
            add(str(row.get("task_id") or ""))

    if missing_test_types:
        for task in tasks:
            if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}:
                continue
            if provider_ids:
                if task.id in provider_ids:
                    add(task.id)
                continue
            if task.id in {scope_task.id for scope_task in scope_rows}:
                add(task.id)
                continue
            if scope_requirement_ids.intersection(task.requirements):
                add(task.id)
    return candidates


def _deferred_test_type_providers(
    tasks: list[Task],
    *,
    scope_task_ids: set[str],
    scope_requirement_ids: set[str],
    missing_test_types: list[str],
) -> list[Task]:
    if not missing_test_types:
        return []
    missing = {str(value).strip() for value in missing_test_types if str(value).strip()}
    providers: list[Task] = []
    for task in tasks:
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}:
            continue
        if task.id in scope_task_ids or task.status in {"verified", "cancelled"}:
            continue
        if not scope_requirement_ids.intersection(task.requirements):
            continue
        inferred = {str(value).strip() for value in infer_test_types(task.output_tests) if str(value).strip()}
        if missing.intersection(inferred):
            providers.append(task)
    return providers


def _implicit_scope_rows(tasks: list[Task], scope_rows: list[Task], scope_requirement_ids: set[str]) -> list[Task]:
    if scope_rows or not scope_requirement_ids:
        return scope_rows
    inferred: list[Task] = []
    for task in tasks:
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}:
            continue
        if not scope_requirement_ids.intersection(task.requirements):
            continue
        inferred.append(task)
    return inferred


def _write_gate_report(
    project_root: Path | str,
    gate: dict[str, Any],
    *,
    scope_rows: list[Task],
    related_results: list[dict[str, Any]],
    observed_test_types: list[str],
    missing_test_types: list[str],
    repair_candidates: list[str],
    reason: str | None,
) -> str:
    report_path = _gate_report_path(project_root, str(gate.get("id") or "gate"))
    lines = [
        f"status: {gate.get('status', 'pending')}",
        "report_type: validation_gate",
        f"gate_id: {gate.get('id', '-')}",
        f"gate_kind: {gate.get('kind', '-')}",
        f"generated_at: {utc_now_iso()}",
        "",
        f"# Validation Gate - {gate.get('title', gate.get('id', 'Gate'))}",
        "",
        "## Summary",
        "",
        f"- Status: {gate.get('status', 'pending')}",
        f"- Required test types: {', '.join(gate.get('required_test_types', [])) or 'none'}",
        f"- Observed test types: {', '.join(observed_test_types) or 'none'}",
        f"- Missing test types: {', '.join(missing_test_types) or 'none'}",
        f"- Repair candidates: {', '.join(repair_candidates) or 'none'}",
    ]
    if reason:
        lines.append(f"- Reason: {reason}")
    lines.extend(["", "## Scope Tasks", ""])
    if scope_rows:
        for task in scope_rows:
            lines.append(f"- {task.id}: {task.status} — {task.title}")
    else:
        lines.append("- none")
    lines.extend(["", "## Related Test Results", ""])
    if related_results:
        for row in related_results:
            task_id = str(row.get("task_id") or "-")
            status = "pass" if bool(row.get("passed")) else "fail"
            test_types = ", ".join(str(value).strip() for value in row.get("test_types", []) if str(value).strip()) or "none"
            lines.append(f"- {task_id}: {status} ({test_types})")
    else:
        lines.append("- none")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(Path(project_root).expanduser().resolve()))


def refresh_gates(project_root: Path | str) -> dict[str, Any]:
    payload = load_gates(project_root)
    tasks = all_tasks(project_root)
    by_id = {task.id: task for task in tasks}
    results_payload = load_test_results(project_root)
    results = results_payload.get("results") if isinstance(results_payload.get("results"), list) else []
    now = utc_now_iso()
    updated_gates: list[dict[str, Any]] = []

    for gate in payload.get("gates", []):
        if not isinstance(gate, dict):
            continue
        gate_id = str(gate.get("id") or "").strip()
        scope_task_ids = [str(value).strip() for value in gate.get("scope_tasks", []) if str(value).strip()]
        scope_rows = [by_id[task_id] for task_id in scope_task_ids if task_id in by_id]
        scope_requirement_ids = set(_gate_scope_requirements(gate, scope_rows))
        effective_scope_rows = _implicit_scope_rows(tasks, scope_rows, scope_requirement_ids)
        related_results = _matched_gate_results(results, scope_task_ids=set(scope_task_ids), scope_requirement_ids=scope_requirement_ids)
        observed_test_types = sorted(
            {
                str(test_type).strip()
                for row in related_results
                if bool(row.get("passed"))
                for test_type in _normalized_result_test_types(row)
                if str(test_type).strip()
            }
        )
        required_test_types = [str(value).strip() for value in gate.get("required_test_types", []) if str(value).strip()]
        missing_test_types = [test_type for test_type in required_test_types if test_type not in observed_test_types]
        deferred_providers = _deferred_test_type_providers(
            tasks,
            scope_task_ids=set(scope_task_ids),
            scope_requirement_ids=scope_requirement_ids,
            missing_test_types=missing_test_types,
        )

        status = "pending"
        reason: str | None = None
        if len(scope_rows) != len(scope_task_ids):
            status = "blocked"
            missing_tasks = [task_id for task_id in scope_task_ids if task_id not in by_id]
            reason = f"gate scope references missing tasks: {', '.join(missing_tasks)}"
        elif not _gate_has_scope_binding(gate):
            status = "pending"
            reason = "gate scope mapping is missing from task-decompose output"
        elif any(task.status in {"blocked", "exception"} for task in effective_scope_rows):
            status = "blocked"
            blocking_ids = [task.id for task in effective_scope_rows if task.status in {"blocked", "exception"}]
            reason = f"scope tasks are not verified: {', '.join(blocking_ids)}"
        elif any(task.status == "review_pending" for task in effective_scope_rows):
            status = "review_pending"
            reason = "scope tasks are waiting for external review"
        elif any(task.status == "active" for task in effective_scope_rows):
            status = "active"
            reason = "scope tasks are still running"
        elif any(task.status == "pending" for task in effective_scope_rows):
            status = "pending"
            reason = "scope tasks are not complete yet"
        elif any(not bool(row.get("passed")) for row in related_results):
            status = "blocked"
            failed_task_ids = _dedupe_strings([str(row.get("task_id") or "") for row in related_results if not bool(row.get("passed"))])
            reason = f"related validation results failed: {', '.join(failed_task_ids) or gate_id}"
        elif missing_test_types:
            if deferred_providers:
                if any(task.status == "review_pending" for task in deferred_providers):
                    status = "review_pending"
                    reason = "related validation tasks are waiting for external review"
                elif any(task.status == "active" for task in deferred_providers):
                    status = "active"
                    reason = "related validation tasks are still running"
                else:
                    status = "pending"
                    reason = "waiting for related validation tasks to satisfy missing test types"
            else:
                status = "blocked"
                reason = f"missing required test types: {', '.join(missing_test_types)}"
        else:
            status = "verified"

        repair_candidates = _gate_repair_candidates(
            gate,
            tasks,
            scope_rows=scope_rows,
            scope_requirement_ids=scope_requirement_ids,
            matched_results=related_results,
            missing_test_types=missing_test_types,
            deferred_provider_ids={task.id for task in deferred_providers},
        )
        updated_gate = dict(gate)
        updated_gate["status"] = status
        updated_gate["scope_requirements"] = list(scope_requirement_ids)
        updated_gate["observed_test_types"] = observed_test_types
        updated_gate["missing_test_types"] = missing_test_types
        updated_gate["repair_candidates"] = repair_candidates
        updated_gate["updated_at"] = now
        if status == "verified":
            updated_gate["verified_at"] = str(gate.get("verified_at") or now)
            updated_gate["blocked_reason"] = None
        else:
            updated_gate["verified_at"] = None
            updated_gate["blocked_reason"] = reason
        updated_gate["report_artifact"] = _write_gate_report(
            project_root,
            {**updated_gate, "status": status},
            scope_rows=scope_rows,
            related_results=related_results,
            observed_test_types=observed_test_types,
            missing_test_types=missing_test_types,
            repair_candidates=repair_candidates,
            reason=reason,
        )
        updated_gates.append(updated_gate)

    payload["gates"] = updated_gates
    save_gates(project_root, payload)
    return payload


def validate_validation_tasks(tasks: list[Task], complexity: dict[str, Any]) -> None:
    tier = str(complexity.get("tier") or "S")
    if tier not in {"M", "L", "XL"}:
        return
    validation_tasks = [
        task
        for task in tasks
        if task.id not in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}
        and task.task_kind == "validation"
    ]
    if not validation_tasks:
        raise ValueError("task-decompose must include at least one validation task when delivery_complexity.tier is M or higher")


def validate_gate_references(tasks: list[Task], gates: list[dict[str, Any]]) -> None:
    task_ids = {
        task.id
        for task in tasks
        if task.id not in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}
    }
    for gate in gates:
        if not _gate_has_scope_binding(gate):
            raise ValueError(f"validation_gates entry must declare scope_tasks or scope_requirements: {gate.get('id')}")
        for task_id in gate.get("scope_tasks", []):
            if task_id not in task_ids:
                raise ValueError(f"validation_gates references unknown task id: {task_id}")


def sync_gates(project_root: Path | str, tasks: list[Task] | None = None, *, stage_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(stage_payload, dict):
        raise ValueError("task-decompose stage payload must be a JSON object")
    task_rows = tasks if tasks is not None else all_tasks(project_root)
    complexity = normalize_complexity_override(stage_payload.get("delivery_complexity"))
    gates = normalize_stage_gates(stage_payload.get("validation_gates"))
    validate_validation_tasks(task_rows, complexity)
    validate_gate_references(task_rows, gates)
    payload = {
        "schema_version": "1",
        "project": Path(project_root).expanduser().resolve().name,
        "complexity": complexity,
        "gates": gates,
    }
    existing = load_gates(project_root)
    if existing.get("gates"):
        existing_by_id = {
            str(gate.get("id") or "").strip(): gate
            for gate in existing.get("gates", [])
            if isinstance(gate, dict)
        }
        for gate in payload["gates"]:
            existing_gate = existing_by_id.get(gate["id"])
            if existing_gate is not None:
                gate["status"] = str(existing_gate.get("status") or gate["status"])
    save_gates(project_root, payload)
    return refresh_gates(project_root)