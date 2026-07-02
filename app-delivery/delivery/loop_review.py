from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import DeliveryError
from .gates import refresh_gates
from .loop_gitops import git_commit_explicit_paths, git_commit_task
from .builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_REQUIRED_SECTIONS, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_REPORT_PATH, PREFINAL_AUDIT_REQUIRED_SECTIONS, PREFINAL_AUDIT_TASK_ID
from .runtime_config import resolve_project_root
from .review_artifacts import (
    _archive_review_round,
    _deferred_review_payload,
    _final_review_path,
    _write_final_review_artifact,
    _write_request_if_changed,
    _write_review_artifact,
    _write_review_repair_limit_report,
    _write_task_contract_deferral_artifacts,
    code_review_request_path,
    final_review_input_path,
    final_review_request_path,
    review_input_path,
)
from .review_payload import (
    REVIEW_SCHEMA,
    _parse_review_payload,
    _review_requests_task_contract_repair,
    _review_requests_task_decompose_repair,
    _validate_pass_review_matrix,
)
from .review_prompts import build_code_review_request, build_final_review_request, build_validation_code_review_request
from .production_semantics import mock_only_browser_e2e_issues
from .session import current_session, retire_session, save_current_session
from .state import ensure_runtime_dirs, load_task_runtime_state, load_test_results, normalize_task_runtime_state, project_paths, save_task_runtime_state, utc_now_iso
from .task import Task, all_tasks, mark_task, next_generated_task_id, save_tasks
from .task_contract_repair import TaskContractRepairBlocked, TaskContractRepairError, apply_task_contract_repair, defer_acceptance_scenarios


MAX_CODE_REVIEW_REPAIR_ATTEMPTS = 4
MAX_FINAL_REPAIR_ITERATIONS = 3
FINAL_VERIFICATION_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"


def _latest_task_test_result(project_root: Path | str, task_id: str) -> dict[str, Any] | None:
    payload = load_test_results(project_root)
    rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    normalized_task_id = str(task_id or "").strip()
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id") or "").strip() == normalized_task_id:
            return row
    return None


def _test_report_relative_path(task_id: str) -> str:
    safe_task_id = str(task_id or "task").strip().replace(":", "-") or "task"
    return f"docs/reviews/test-report-{safe_task_id}.md"


def _format_failed_test_summary(row: dict[str, Any]) -> str:
    test_files = row.get("test_files") if isinstance(row.get("test_files"), list) else []
    failures = row.get("failures") if isinstance(row.get("failures"), list) else []
    failed_specs: list[str] = []
    for failure in failures[:5]:
        if not isinstance(failure, dict):
            continue
        spec = str(failure.get("test") or "").strip()
        message = str(failure.get("message") or "").strip()
        if spec and message:
            failed_specs.append(f"{spec}: {message[:240]}")
        elif spec:
            failed_specs.append(spec)
    if failed_specs:
        return "; ".join(failed_specs)
    if test_files:
        return ", ".join(str(spec) for spec in test_files[:8])
    return "latest task validation failed"


def _prefinal_audit_artifact_issues(project_root: Path | str) -> list[str]:
    project_dir = resolve_project_root(project_root)
    report_path = project_dir / PREFINAL_AUDIT_REPORT_PATH
    if not report_path.is_file():
        return [f"required audit report is missing: {PREFINAL_AUDIT_REPORT_PATH}"]
    try:
        report_text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"required audit report cannot be read: {PREFINAL_AUDIT_REPORT_PATH} ({exc})"]
    if not report_text.strip():
        return [f"required audit report is empty: {PREFINAL_AUDIT_REPORT_PATH}"]
    missing_sections = [section for section in PREFINAL_AUDIT_REQUIRED_SECTIONS if section not in report_text]
    if missing_sections:
        return ["required audit report is missing sections: " + ", ".join(missing_sections)]
    return []


def _frontend_api_audit_artifact_issues(project_root: Path | str) -> list[str]:
    project_dir = resolve_project_root(project_root)
    report_path = project_dir / FRONTEND_API_AUDIT_REPORT_PATH
    if not report_path.is_file():
        return [f"required frontend/API audit report is missing: {FRONTEND_API_AUDIT_REPORT_PATH}"]
    try:
        report_text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"required frontend/API audit report cannot be read: {FRONTEND_API_AUDIT_REPORT_PATH} ({exc})"]
    if not report_text.strip():
        return [f"required frontend/API audit report is empty: {FRONTEND_API_AUDIT_REPORT_PATH}"]
    missing_sections = [section for section in FRONTEND_API_AUDIT_REQUIRED_SECTIONS if section not in report_text]
    if missing_sections:
        return ["required frontend/API audit report is missing sections: " + ", ".join(missing_sections)]
    return []


def _review_pass_precondition_errors(project_root: Path | str, task: Task) -> list[str]:
    errors: list[str] = []
    latest_result = _latest_task_test_result(project_root, task.id)
    if latest_result is not None and not bool(latest_result.get("passed")):
        timestamp = str(latest_result.get("timestamp") or "unknown").strip() or "unknown"
        errors.append(
            "latest task validation failed "
            f"at {timestamp}; report={_test_report_relative_path(task.id)}; "
            f"failures={_format_failed_test_summary(latest_result)}"
        )
    if task.id == PREFINAL_AUDIT_TASK_ID:
        errors.extend(_prefinal_audit_artifact_issues(project_root))
    if task.id == FRONTEND_API_AUDIT_TASK_ID:
        errors.extend(_frontend_api_audit_artifact_issues(project_root))
    errors.extend(mock_only_browser_e2e_issues(project_root, task.output_tests))
    return errors


def _enforce_review_pass_preconditions(project_root: Path | str, task: Task, input_path: Path) -> None:
    errors = _review_pass_precondition_errors(project_root, task)
    if not errors:
        return
    raise DeliveryError(
        code="review_pass_preconditions_failed",
        message="review status=pass rejected because machine-verifiable task preconditions are not satisfied",
        exit_code=2,
        details={
            "project": str(resolve_project_root(project_root)),
            "task_id": task.id,
            "input_path": str(input_path),
            "precondition_errors": errors,
        },
        suggested_action="Repair the task, rerun its declared validation until the latest task test report passes, and ensure required artifacts exist before importing a pass review.",
    )


def write_code_review_request(
    project_root: Path | str,
    task: Task,
    *,
    scope_report: dict[str, Any] | None = None,
) -> str:
    request_path = code_review_request_path(project_root, task.id)
    _write_request_if_changed(
        request_path,
        build_code_review_request(project_root, task, scope_report=scope_report) + "\n",
    )
    return str(request_path.relative_to(resolve_project_root(project_root)))


def write_final_review_request(
    project_root: Path | str,
    *,
    results_summary: str,
    requirement_coverage: dict[str, Any],
    missing_test_types: list[tuple[str, str]],
) -> str:
    request_path = final_review_request_path(project_root)
    _write_request_if_changed(
        request_path,
        build_final_review_request(
            project_root,
            results_summary=results_summary,
            requirement_coverage=requirement_coverage,
            missing_test_types=missing_test_types,
        )
        + "\n",
    )
    return str(request_path.relative_to(resolve_project_root(project_root)))


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


def _final_repair_iteration_count(tasks: list[Task]) -> int:
    return sum(
        1
        for task in tasks
        if task.task_kind == "repair"
        and (
            task.title.startswith(FINAL_VERIFICATION_REPAIR_TASK_PREFIX)
            or task.title.startswith(FINAL_REVIEW_REPAIR_TASK_PREFIX)
        )
    )


def _final_review_repair_candidates(tasks: list[Task], review_payload: dict[str, Any]) -> list[Task]:
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    requirement_ids = {
        str(value).strip()
        for finding in findings
        if isinstance(finding, dict)
        for value in finding.get("requirement_ids", [])
        if str(value).strip()
    }
    acceptance_ids = {
        str(value).strip()
        for finding in findings
        if isinstance(finding, dict)
        for value in finding.get("acceptance_ids", [])
        if str(value).strip()
    }
    eligible = [
        task
        for task in tasks
        if task.id != FINAL_VERIFY_TASK_ID and task.task_kind != "repair" and task.status == "verified"
    ]
    matched = [
        task
        for task in eligible
        if requirement_ids.intersection(task.requirements) or acceptance_ids.intersection(task.acceptance_scenarios)
    ]
    if matched or not findings:
        return matched
    return eligible


def _create_final_review_repair_task(project_root: Path, tasks: list[Task], review_payload: dict[str, Any], review_artifact: str) -> tuple[list[Task], str | None, list[str]]:
    source_tasks = _final_review_repair_candidates(tasks, review_payload)
    if not source_tasks:
        return tasks, None, []
    repair_candidate_ids = [task.id for task in source_tasks]
    if _final_repair_iteration_count(tasks) >= MAX_FINAL_REPAIR_ITERATIONS:
        return tasks, None, repair_candidate_ids

    repair_task_id = next_generated_task_id(tasks)
    requirements = _dedupe_strings([requirement for task in source_tasks for requirement in task.requirements])
    acceptance_scenarios = _dedupe_strings([scenario for task in source_tasks for scenario in task.acceptance_scenarios])
    output_tests = _dedupe_strings([test for task in source_tasks for test in task.output_tests])[:10]
    output_paths = _dedupe_strings([path for task in source_tasks for path in task.output_paths] + [review_artifact])[:20]
    dependencies = [
        task.id
        for task in tasks
        if task.id not in {FINAL_VERIFY_TASK_ID, repair_task_id} and task.status == "verified"
    ]
    title = FINAL_REVIEW_REPAIR_TASK_PREFIX
    if repair_candidate_ids:
        title = f"{FINAL_REVIEW_REPAIR_TASK_PREFIX} ({', '.join(repair_candidate_ids[:3])}{'...' if len(repair_candidate_ids) > 3 else ''})"
    summary = str(review_payload.get("summary") or "final review changes requested").strip() or "final review changes requested"
    repair_task = Task.from_dict(
        {
            "id": repair_task_id,
            "title": title,
            "status": "pending",
            "task_kind": "repair",
            "requirements": requirements,
            "acceptance_scenarios": acceptance_scenarios,
            "dependencies": dependencies,
            "output_tests": output_tests,
            "output_paths": output_paths or [review_artifact],
            "blocked_reason": f"final review requested changes: {summary}; see {review_artifact}",
            "attempts": 0,
        }
    )
    updated: list[Task] = []
    inserted = False
    for task in tasks:
        if task.id == FINAL_VERIFY_TASK_ID and not inserted:
            updated.append(repair_task)
            inserted = True
        if task.id == FINAL_VERIFY_TASK_ID:
            data = task.to_dict()
            dependencies = list(data.get("dependencies", []))
            if repair_task_id not in dependencies:
                dependencies.append(repair_task_id)
            data["dependencies"] = dependencies
            updated.append(Task.from_dict(data))
        else:
            updated.append(task)
    if not inserted:
        updated.append(repair_task)
    return updated, repair_task_id, repair_candidate_ids


def import_task_review(project_root: Path | str, task_id: str, payload: dict[str, Any], input_path: Path) -> int:
    project_dir = resolve_project_root(project_root)
    task = next((row for row in all_tasks(project_dir) if row.id == task_id), None)
    if task is None:
        raise DeliveryError(
            code="review_task_missing",
            message=f"task does not exist for review import: {task_id}",
            exit_code=2,
            details={"project": str(project_dir), "task_id": task_id, "input_path": str(input_path)},
        )
    if task.status != "review_pending":
        raise DeliveryError(
            code="review_task_not_pending",
            message=f"task is not awaiting external review: {task_id}",
            exit_code=2,
            details={"project": str(project_dir), "task_id": task_id, "status": task.status, "input_path": str(input_path)},
        )
    parsed = _parse_review_payload(json.dumps(payload, ensure_ascii=False))
    try:
        parsed = _validate_pass_review_matrix(task, parsed)
    except ValueError as exc:
        raise DeliveryError(
            code="review_assessment_invalid",
            message=str(exc),
            exit_code=2,
            details={"project": str(project_dir), "task_id": task_id, "input_path": str(input_path)},
        ) from exc
    review_status = str(parsed.get("status") or "changes_requested").strip()
    if review_status.casefold() == "pass":
        _enforce_review_pass_preconditions(project_dir, task, input_path)
    reviewed_at = utc_now_iso()
    persisted_input = review_input_path(project_dir, task_id)
    persisted_input_content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    persisted_input.write_text(persisted_input_content, encoding="utf-8")
    review_artifact = _write_review_artifact(project_dir, task, parsed)
    _archive_review_round(
        project_dir,
        review_name=f"code-review-{task_id}",
        request_path=code_review_request_path(project_dir, task_id),
        input_extension=".json",
        input_content=persisted_input_content,
        artifact_path=project_paths(project_dir).project_root / review_artifact,
        reviewed_at=reviewed_at,
    )
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_dir, task_id))
    pending_scope_report = runtime_state.get("pending_scope_report") if isinstance(runtime_state.get("pending_scope_report"), dict) else {}
    accepted_scope_paths = [
        str(path).strip()
        for path in pending_scope_report.get("out_of_scope", [])
        if str(path).strip()
    ]
    staged_scope_paths = [
        str(path).strip()
        for path in pending_scope_report.get("staged_paths", [])
        if str(path).strip()
    ]
    if review_status.casefold() == "pass":
        if staged_scope_paths:
            commit_sha = git_commit_explicit_paths(
                project_dir,
                [*staged_scope_paths, *accepted_scope_paths, review_artifact],
                f"feat({task.id}): {task.title}",
            )
        else:
            commit_sha = git_commit_task(
                project_dir,
                task,
                f"feat({task.id}): {task.title}",
                extra_paths=[*accepted_scope_paths, review_artifact],
            )
        tasks = mark_task(
            all_tasks(project_dir),
            task.id,
            "verified",
            git_commit=commit_sha,
            status_session_id=task.status_session_id,
            completed_at=task.completed_at or reviewed_at,
            review_status="pass",
            review_artifact=review_artifact,
            reviewed_at=reviewed_at,
            verified_at=reviewed_at,
            blocked_reason=None,
            attempts=max(task.attempts, 1),
        )
        save_tasks(project_dir, tasks)
        save_task_runtime_state(
            project_dir,
            task_id,
            {
                "accepted_scope_paths": accepted_scope_paths,
                "pending_scope_report": None,
                "review_changes_requested_count": 0,
                "review_repair_limit_reached": False,
            },
        )
        active_session = current_session(project_dir)
        if active_session is not None and active_session.id == task.status_session_id:
            active_session.current_task_id = None
            active_session.title = active_session.runtime
            save_current_session(project_dir, active_session)
        refresh_gates(project_dir)
        return 0

    review_changes_requested_count = int(runtime_state.get("review_changes_requested_count") or 0) + 1
    review_repair_state = {
        "review_changes_requested_count": review_changes_requested_count,
        "review_repair_limit": MAX_CODE_REVIEW_REPAIR_ATTEMPTS,
        "last_changes_requested_review_artifact": review_artifact,
        "last_changes_requested_reviewed_at": reviewed_at,
    }
    if review_changes_requested_count >= MAX_CODE_REVIEW_REPAIR_ATTEMPTS:
        exception_report = _write_review_repair_limit_report(
            project_dir,
            task,
            review_payload=parsed,
            review_artifact=review_artifact,
            review_count=review_changes_requested_count,
            review_limit=MAX_CODE_REVIEW_REPAIR_ATTEMPTS,
        )
        tasks = mark_task(
            all_tasks(project_dir),
            task.id,
            "exception",
            git_commit=None,
            status_session_id=task.status_session_id,
            started_at=None,
            completed_at=None,
            review_status=review_status,
            review_artifact=exception_report,
            reviewed_at=reviewed_at,
            verified_at=None,
            blocked_reason=(
                f"code review requested changes {review_changes_requested_count} time(s), "
                f"reaching the configured limit of {MAX_CODE_REVIEW_REPAIR_ATTEMPTS}; see {exception_report}"
            ),
            attempts=max(task.attempts, 1),
        )
        save_tasks(project_dir, tasks)
        active_session = current_session(project_dir)
        if active_session is not None and active_session.id == task.status_session_id:
            retire_session(project_dir, active_session)
        save_task_runtime_state(
            project_dir,
            task_id,
            {
                **review_repair_state,
                "review_repair_limit_reached": True,
                "review_repair_limit_report": exception_report,
                "review_requested_scope_paths": accepted_scope_paths,
                "pending_scope_report": None,
            },
        )
        refresh_gates(project_dir)
        return 2

    if _review_requests_task_contract_repair(parsed):
        task_contract_assessment = parsed["task_contract_assessment"]
        tasks_before_repair = all_tasks(project_dir)
        try:
            repaired_tasks, repair_result = apply_task_contract_repair(project_dir, tasks_before_repair, task, task_contract_assessment)
        except TaskContractRepairError as exc:
            invalid_reason = str(exc)
            tasks = mark_task(
                all_tasks(project_dir),
                task.id,
                "pending",
                git_commit=None,
                status_session_id=task.status_session_id,
                started_at=None,
                completed_at=None,
                review_status=review_status,
                review_artifact=review_artifact,
                reviewed_at=reviewed_at,
                verified_at=None,
                blocked_reason=(
                    f"code review requested invalid task-contract repair ({invalid_reason}); "
                    f"treating as implementation repair. {str(parsed.get('summary') or '').strip()}"
                ).strip(),
                attempts=max(task.attempts, 1),
            )
            save_tasks(project_dir, tasks)
            active_session = current_session(project_dir)
            if active_session is not None and active_session.id == task.status_session_id:
                retire_session(project_dir, active_session)
            save_task_runtime_state(
                project_dir,
                task_id,
                {
                    **review_repair_state,
                    "review_requested_scope_paths": accepted_scope_paths,
                    "task_contract_repair_invalid": {
                        "error": invalid_reason,
                        "assessment": task_contract_assessment,
                    },
                    "task_contract_blocker": None,
                    "pending_scope_report": None,
                },
            )
            refresh_gates(project_dir)
            return 2
        except RuntimeError as exc:
            if exc.__class__.__name__ != "TaskContractRepairBlocked":
                raise
            notes = str(task_contract_assessment.get("notes") or parsed.get("summary") or str(exc)).strip()
            deferred_tasks, deferral_result = defer_acceptance_scenarios(
                tasks_before_repair,
                task,
                task_contract_assessment,
                reason=f"{notes}; local repair could not assign a safe target or follow-up task: {exc}",
                review_artifact=review_artifact,
                created_at=reviewed_at,
            )
            deferral_artifacts = _write_task_contract_deferral_artifacts(project_dir, deferral_result)
            deferred_parsed = _deferred_review_payload(parsed, deferral_result, deferral_artifacts)
            review_artifact = _write_review_artifact(project_dir, task, deferred_parsed)
            staged_paths = [*staged_scope_paths, *accepted_scope_paths, review_artifact, *deferral_artifacts]
            if staged_paths:
                commit_sha = git_commit_explicit_paths(
                    project_dir,
                    staged_paths,
                    f"feat({task.id}): {task.title}",
                )
            else:
                commit_sha = git_commit_task(
                    project_dir,
                    task,
                    f"feat({task.id}): {task.title}",
                    extra_paths=[review_artifact, *deferral_artifacts],
                )
            tasks = mark_task(
                deferred_tasks,
                task.id,
                "verified",
                git_commit=commit_sha,
                status_session_id=task.status_session_id,
                completed_at=task.completed_at or reviewed_at,
                review_status="pass",
                review_artifact=review_artifact,
                reviewed_at=reviewed_at,
                verified_at=reviewed_at,
                blocked_reason=(
                    "verified with deferred acceptance scenario assignment; "
                    f"see {deferral_artifacts[1]}"
                ),
                attempts=max(task.attempts, 1),
            )
            save_tasks(project_dir, tasks)
            active_session = current_session(project_dir)
            if active_session is not None and active_session.id == task.status_session_id:
                retire_session(project_dir, active_session)
            save_task_runtime_state(
                project_dir,
                task_id,
                {
                    **review_repair_state,
                    "review_requested_scope_paths": accepted_scope_paths,
                    "task_contract_deferred_acceptance": deferral_result,
                    "task_contract_deferred_artifacts": list(deferral_artifacts),
                    "task_contract_deferred_at": reviewed_at,
                    "task_contract_blocker": None,
                    "pending_scope_report": None,
                },
            )
            refresh_gates(project_dir)
            return 0
        repaired_current = next((row for row in repaired_tasks if row.id == task.id), None)
        if repaired_current is not None:
            repaired_tasks = mark_task(
                repaired_tasks,
                task.id,
                "pending",
                git_commit=None,
                status_session_id=task.status_session_id,
                started_at=None,
                completed_at=None,
                review_status=None,
                review_artifact=review_artifact,
                reviewed_at=reviewed_at,
                verified_at=None,
                blocked_reason=str(parsed.get("summary") or "task contract repaired; rerun review/validation for updated task contract").strip() or "task contract repaired; rerun review/validation for updated task contract",
                attempts=max(task.attempts, 1),
            )
        save_tasks(project_dir, repaired_tasks)
        active_session = current_session(project_dir)
        if active_session is not None and active_session.id == task.status_session_id:
            retire_session(project_dir, active_session)
        save_task_runtime_state(
            project_dir,
            task_id,
            {
                **review_repair_state,
                "review_requested_scope_paths": accepted_scope_paths,
                "task_contract_repair": repair_result,
                "task_contract_repaired_at": reviewed_at,
                "task_contract_blocker": None,
                "pending_scope_report": None,
            },
        )
        refresh_gates(project_dir)
        return 2

    if _review_requests_task_decompose_repair(parsed):
        task_contract_assessment = parsed["task_contract_assessment"]
        notes = str(task_contract_assessment.get("notes") or parsed.get("summary") or "task contract review requested task-decompose repair").strip()
        blocked_reason = f"task contract requires task-decompose repair: {notes}"
        tasks = mark_task(
            all_tasks(project_dir),
            task.id,
            "blocked",
            git_commit=None,
            status_session_id=task.status_session_id,
            started_at=None,
            completed_at=None,
            review_status=review_status,
            review_artifact=review_artifact,
            reviewed_at=reviewed_at,
            verified_at=None,
            blocked_reason=blocked_reason,
            attempts=max(task.attempts, 1),
        )
        save_tasks(project_dir, tasks)
        active_session = current_session(project_dir)
        if active_session is not None and active_session.id == task.status_session_id:
            retire_session(project_dir, active_session)
        save_task_runtime_state(
            project_dir,
            task_id,
            {
                **review_repair_state,
                "review_requested_scope_paths": accepted_scope_paths,
                "task_contract_blocker": task_contract_assessment,
                "task_contract_blocked_at": reviewed_at,
            },
        )
        refresh_gates(project_dir)
        return 2

    tasks = mark_task(
        all_tasks(project_dir),
        task.id,
        "pending",
        git_commit=None,
        status_session_id=task.status_session_id,
        started_at=None,
        completed_at=None,
        review_status=review_status,
        review_artifact=review_artifact,
        reviewed_at=reviewed_at,
        verified_at=None,
        blocked_reason=str(parsed.get("summary") or "review changes requested").strip() or "review changes requested",
        attempts=max(task.attempts, 1),
    )
    save_tasks(project_dir, tasks)
    active_session = current_session(project_dir)
    if active_session is not None and active_session.id == task.status_session_id:
        retire_session(project_dir, active_session)
    save_task_runtime_state(
        project_dir,
        task_id,
        {
            **review_repair_state,
            "review_requested_scope_paths": accepted_scope_paths,
        },
    )
    refresh_gates(project_dir)
    return 2


def import_final_review(project_root: Path | str, payload: dict[str, Any], input_path: Path) -> int:
    project_dir = resolve_project_root(project_root)
    task = next((row for row in all_tasks(project_dir) if row.id == FINAL_VERIFY_TASK_ID), None)
    if task is None:
        raise DeliveryError(
            code="final_review_task_missing",
            message="T-FINAL is missing; cannot import final review",
            exit_code=2,
            details={"project": str(project_dir), "input_path": str(input_path)},
        )
    if task.status != "review_pending":
        raise DeliveryError(
            code="final_review_not_pending",
            message="T-FINAL is not awaiting external review",
            exit_code=2,
            details={"project": str(project_dir), "status": task.status, "input_path": str(input_path)},
        )
    parsed = _parse_review_payload(json.dumps(payload, ensure_ascii=False))
    reviewed_at = utc_now_iso()
    persisted_input = final_review_input_path(project_dir)
    persisted_input_content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    persisted_input.write_text(persisted_input_content, encoding="utf-8")
    review_artifact = _write_final_review_artifact(project_dir, parsed)
    _archive_review_round(
        project_dir,
        review_name="final-review",
        request_path=final_review_request_path(project_dir),
        input_extension=".json",
        input_content=persisted_input_content,
        artifact_path=project_paths(project_dir).project_root / review_artifact,
        reviewed_at=reviewed_at,
    )
    review_status = str(parsed.get("status") or "changes_requested").strip()
    if review_status.casefold() == "pass":
        tasks = mark_task(
            all_tasks(project_dir),
            FINAL_VERIFY_TASK_ID,
            "verified",
            review_status="pass",
            review_artifact=review_artifact,
            reviewed_at=reviewed_at,
            verified_at=reviewed_at,
            blocked_reason=None,
        )
        save_tasks(project_dir, tasks)
        save_task_runtime_state(
            project_dir,
            FINAL_VERIFY_TASK_ID,
            {
                "final_verify_status": "pass",
            },
        )
        refresh_gates(project_dir)
        return 0

    current_tasks = all_tasks(project_dir)
    current_tasks, repair_task_id, repair_candidates = _create_final_review_repair_task(project_dir, current_tasks, parsed, review_artifact)
    blocked_reason = str(parsed.get("summary") or "final review changes requested").strip() or "final review changes requested"
    if repair_task_id:
        blocked_reason = f"final review created repair task {repair_task_id}; preserve verified tasks and repair through that bundle"
    elif repair_candidates:
        blocked_reason = f"final review reached maximum repair iterations ({MAX_FINAL_REPAIR_ITERATIONS}); remaining findings require manual escalation"
    tasks = mark_task(
        current_tasks,
        FINAL_VERIFY_TASK_ID,
        "blocked",
        review_status=review_status,
        review_artifact=review_artifact,
        reviewed_at=reviewed_at,
        blocked_reason=blocked_reason,
    )
    save_tasks(project_dir, tasks)
    save_task_runtime_state(
        project_dir,
        FINAL_VERIFY_TASK_ID,
        {
            "repair_candidates": repair_candidates,
            "repair_task_id": repair_task_id,
            "repair_report_artifact": review_artifact,
            "final_repair_limit_reached": bool(repair_candidates and not repair_task_id),
            "final_verify_status": "repair_required" if repair_task_id else "blocked",
        },
    )
    refresh_gates(project_dir)
    return 2


__all__ = [
    "REVIEW_SCHEMA",
    "build_code_review_request",
    "build_final_review_request",
    "code_review_request_path",
    "final_review_request_path",
    "review_input_path",
    "final_review_input_path",
    "import_task_review",
    "import_final_review",
    "_parse_review_payload",
    "_write_review_artifact",
    "_write_final_review_artifact",
    "write_code_review_request",
    "write_final_review_request",
]