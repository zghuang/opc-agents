from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from .config_validation import config_validation_summary, validate_project_config_files
from .environment_repair import (
    ENVIRONMENT_REPAIR_TASK_PREFIX,
    build_environment_repair_plan,
    has_environment_failures,
)
from .errors import DeliveryError
from .gates import refresh_gates
from .loop_gitops import (
    blocking_verified_task_issues,
    clear_task_exception_patch_conflict,
    ensure_git_repo,
    git_changed_paths,
    git_commit_task,
    git_commit_timestamp,
    git_head_sha,
    git_latest_task_commit,
    git_stage_task_snapshot,
    is_runtime_protected_framework_artifact,
    park_task_exception_changes,
    repair_invalid_verified_tasks,
    restore_paths_to_head,
    review_artifact_status,
    task_scope_delta,
    task_scoped_changed_paths,
)
from .loop_reporting import PAUSE_FILE, project_summary, render_release_evidence
from .loop_review import write_code_review_request, write_final_review_request
from .loop_task_prompt import (
    build_fix_prompt,
    build_stalled_recovery_prompt,
    build_task_prompt,
)
from .production_semantics import (
    SemanticFinding,
    scan_production_semantics,
    write_semantic_scan_report,
)
from .runtime_config import resolve_runtime
from .scaffold import scaffold_project
from .session import (
    RuntimeErrorResponse,
    RuntimeSession,
    current_session,
    execute_in_session,
    retire_session,
    save_current_session,
    start_task_session,
    touch_session,
)
from .stack_contracts import PYTHON_REACT_CONTRACT, backend_test_root
from .state import (
    clear_task_runtime_failure,
    ensure_runtime_dirs,
    load_task_runtime_state,
    normalize_task_runtime_state,
    prune_stale_active_task_records,
    remember_task_runtime_failure,
    repeated_task_runtime_failure_block,
    save_task_runtime_state,
    utc_now_iso,
)
from .system_gap_ledger import system_gap_ledger_issues
from .task import (
    FINAL_VERIFY_TASK_ID,
    PREFINAL_AUDIT_TASK_ID,
    SCAFFOLD_OUTPUT_PATHS,
    SCAFFOLD_TASK_ID,
    SHARED_FOUNDATION_TASK_ID,
    Task,
    all_tasks,
    check_requirements_coverage,
    check_test_type_coverage,
    mark_task,
    next_generated_task_id,
    pick_next_task,
    reset_task,
    save_tasks,
)
from .test_env import (
    project_has_browser_e2e,
    warm_browser_e2e_environment,
    warm_shared_test_environment,
)
from .verify import (
    infer_final_repair_candidates,
    is_command_test_spec,
    run_full_suite,
    run_task_tests,
    test_results_to_summary,
    write_final_repair_report,
)

MAX_TEST_FIX_ATTEMPTS = 3
MAX_STALLED_RUNTIME_RECOVERIES = 2
MAX_FINAL_REPAIR_ITERATIONS = 3
FINAL_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"


def _final_release_hard_gates(
    *,
    full_suite_passed: bool,
    requirement_coverage: dict[str, Any],
    missing_test_types: list[tuple[str, str]],
    non_verified_gates: list[dict[str, Any]],
    semantic_findings: list[SemanticFinding],
    system_gap_issues: list[str],
) -> list[dict[str, Any]]:
    return [
        {
            "id": "system_gap_fix",
            "status": "pass" if not system_gap_issues else "fail",
            "evidence": ["docs/reviews/system-gap-fix.json"],
        },
        {
            "id": "full_suite",
            "status": "pass" if full_suite_passed else "fail",
            "evidence": ["docs/test-results.json"],
        },
        {
            "id": "requirement_coverage",
            "status": "pass" if requirement_coverage.get("all_covered") else "fail",
            "evidence": ["docs/work-items.json", "docs/requirements.json"],
        },
        {
            "id": "test_type_coverage",
            "status": "pass" if not missing_test_types else "fail",
            "evidence": ["docs/test-results.json", "docs/gates.json"],
        },
        {
            "id": "validation_gates",
            "status": "pass" if not non_verified_gates else "fail",
            "evidence": ["docs/gates.json"],
        },
        {
            "id": "production_semantics",
            "status": "pass" if not semantic_findings else "fail",
            "evidence": ["docs/reviews/production-semantic-scan.md"],
        },
    ]


def _refresh_project_summary_after_task_update(project_root: Path | str) -> None:
    try:
        project_summary(project_root)
    except Exception:
        return


def _write_exception_report(project_root: Path | str, task: Task, summary: str, *, patch_relative_path: str | None = None) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    review_dir = project_dir / "docs" / "reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    report_path = review_dir / f"exception-report-{task.id}.md"
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_dir, task.id))
    runtime_fields = {
        "session_id": runtime_state.get("session_id") or task.status_session_id,
        "status": runtime_state.get("status"),
        "failure_kind": runtime_state.get("failure_kind"),
        "failure_count": runtime_state.get("failure_count"),
        "last_tool_name": runtime_state.get("last_tool_name"),
        "last_tool_at": runtime_state.get("last_tool_at"),
        "last_mutation_at": runtime_state.get("last_mutation_at"),
        "stalled_recovery_count": runtime_state.get("stalled_recovery_count"),
        "log_file": runtime_state.get("log_file"),
    }
    runtime_evidence = [
        f"- {key}: {value}"
        for key, value in runtime_fields.items()
        if value is not None and value != "" and value != []
    ]
    failure_message = str(runtime_state.get("failure_message") or "").strip()
    task_contract_blocker = runtime_state.get("task_contract_blocker") if isinstance(runtime_state.get("task_contract_blocker"), dict) else None
    lines = [
        "status: exception",
        "report_type: exception",
        f"work_item: {task.id}",
        "",
        "# Exception Report",
        "",
        "## Summary",
        "",
        summary or "No summary provided.",
        "",
        "## Runtime Evidence",
        "",
        *(runtime_evidence or ["- none recorded"]),
    ]
    if failure_message:
        lines.extend(["", "## Failure Message", "", failure_message])
    if task.review_artifact or task.review_status:
        lines.extend(
            [
                "",
                "## Review State",
                "",
                f"- review_status: {task.review_status or 'none'}",
                f"- review_artifact: {task.review_artifact or 'none'}",
            ]
        )
    if task_contract_blocker:
        lines.extend(
            [
                "",
                "## Task Contract Blocker",
                "",
                f"- recommended_action: {task_contract_blocker.get('recommended_action', '-')}",
                f"- issue_type: {task_contract_blocker.get('issue_type', '-')}",
                f"- notes: {task_contract_blocker.get('notes', '')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Next Handling",
            "",
            "- Inspect the task blocked_reason and runtime state.",
            "- Reapply the exception patch through `app-delivery fix --task-id <id>` before attempting another repair turn.",
            "- Determine whether the failure is task-local, framework-level, or a contract gap before resuming.",
        ]
    )
    if patch_relative_path:
        lines.extend(["", "## Exception Patch", "", f"- {patch_relative_path}"])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(project_dir))


def _preserved_framework_review_artifact_paths(task: Task, runtime_state: dict[str, Any]) -> list[str]:
    candidates = [
        getattr(task, "review_artifact", None),
        runtime_state.get("last_changes_requested_review_artifact"),
        runtime_state.get("review_repair_limit_report"),
    ]
    paths: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = str(candidate or "").strip()
        if not path or path in seen:
            continue
        if is_runtime_protected_framework_artifact(path):
            seen.add(path)
            paths.append(path)
    return paths


def _dedupe_task_ids(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _format_missing_test_types(missing_test_types: list[tuple[str, str]]) -> str:
    values = [f"{requirement_id}:{test_type}" for requirement_id, test_type in missing_test_types[:5]]
    if len(missing_test_types) > 5:
        values.append("...")
    return ", ".join(values) or "none"


def _semantic_repair_candidates(tasks: list[Task], findings: list[SemanticFinding]) -> list[str]:
    candidates: list[str] = []
    for finding in findings:
        path = str(finding.path or "").strip().rstrip("/")
        if not path:
            continue
        for task in tasks:
            if task.status != "verified" or task.task_kind == "repair" or task.id == FINAL_VERIFY_TASK_ID:
                continue
            for scope in task.output_paths:
                normalized = str(scope or "").strip().rstrip("/")
                if normalized and (path == normalized or path.startswith(normalized + "/") or normalized.startswith(path + "/")):
                    candidates.append(task.id)
                    break
    return _dedupe_task_ids(candidates)


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


def _normalize_final_repair_test_spec(project_root: Path | str, spec: str) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    normalized = str(spec or "").strip().lstrip("./")
    if not normalized:
        return ""
    if normalized.startswith(("backend/", "frontend/", "mock-server/")) or is_command_test_spec(normalized):
        return normalized
    frontend_root = project_dir / "frontend"
    if frontend_root.joinpath("package.json").exists() and normalized.startswith(("src/", "e2e/")):
        return f"frontend/{normalized}"
    backend_tests_root = project_dir / "backend" / "tests"
    if backend_tests_root.exists() and normalized.startswith("tests/") and not project_dir.joinpath("tests").exists():
        return f"backend/{normalized}"
    return normalized


def _failed_test_specs_for_repair(project_root: Path | str, results: list[Any]) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for result in results:
        if getattr(result, "passed", False):
            continue
        for test_file in getattr(result, "test_files", []) or []:
            normalized = _normalize_final_repair_test_spec(project_root, str(test_file or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            specs.append(normalized)
    return specs[:10]


def _supplement_test_specs_for_missing_types(project_root: Path | str, missing_types: list[str], existing_specs: list[str]) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    supplemental_specs: list[str] = []
    supplemental_seen: set[str] = set()

    def add_supplemental(spec: str) -> None:
        normalized = str(spec).strip()
        if not normalized or normalized in supplemental_seen:
            return
        supplemental_seen.add(normalized)
        supplemental_specs.append(normalized)

    frontend_dir = project_dir / "frontend"
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
    if "integration" in missing_types and project_dir.joinpath(*backend_tests_root.split("/")).exists():
        add_supplemental(f"{backend_tests_root}/")
    if "contract" in missing_types and project_dir.joinpath(PYTHON_REACT_CONTRACT.mock_server_root, "tests").exists():
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


def _ensure_final_repair_task(
    project_root: Path | str,
    tasks: list[Task],
    *,
    repair_candidates: list[str],
    results: list[Any],
    missing_test_types: list[tuple[str, str]],
    non_verified_gates: list[dict[str, Any]],
) -> tuple[list[Task], str | None, bool]:
    by_id = {task.id: task for task in tasks}
    final_runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID))
    current_repair_task_id = str(final_runtime_state.get("repair_task_id") or "").strip()
    current_repair_task = by_id.get(current_repair_task_id) if current_repair_task_id else None
    if current_repair_task is not None and current_repair_task.status == "exception":
        return tasks, current_repair_task.id, False
    if current_repair_task is not None and current_repair_task.status != "verified":
        repair_task_id = current_repair_task.id
    else:
        existing_final_repairs = [
            task
            for task in tasks
            if task.task_kind == "repair"
            and (
                task.title.startswith(FINAL_REPAIR_TASK_PREFIX)
                or task.title.startswith(FINAL_REVIEW_REPAIR_TASK_PREFIX)
            )
        ]
        if len(existing_final_repairs) >= MAX_FINAL_REPAIR_ITERATIONS:
            return tasks, current_repair_task.id if current_repair_task is not None else None, True
        repair_task_id = next_generated_task_id(tasks)

    source_tasks = [by_id[task_id] for task_id in repair_candidates if task_id in by_id]
    requirements = _dedupe_task_ids(
        [requirement_id for task in source_tasks for requirement_id in task.requirements]
        + [requirement_id for requirement_id, _ in missing_test_types]
    )
    output_tests = _failed_test_specs_for_repair(project_root, results)
    if not output_tests:
        output_tests = _dedupe_task_ids([spec for task in source_tasks for spec in task.output_tests])[:10]
    supplemental_missing_types = [test_type for _, test_type in missing_test_types]
    for gate in non_verified_gates:
        gate_missing_types = gate.get("missing_test_types") if isinstance(gate.get("missing_test_types"), list) else []
        for test_type in gate_missing_types:
            normalized = str(test_type).strip()
            if normalized and normalized not in supplemental_missing_types:
                supplemental_missing_types.append(normalized)
    output_tests = _supplement_test_specs_for_missing_types(project_root, supplemental_missing_types, output_tests)
    output_paths = _collapse_repair_scope_paths([path for task in source_tasks for path in task.output_paths])
    if not output_paths:
        output_paths = _collapse_repair_scope_paths(
            [str(gate.get("report_artifact") or "").strip() for gate in non_verified_gates if str(gate.get("report_artifact") or "").strip()]
        )
    if not output_paths:
        output_paths = ["docs/reviews/"]
    dependencies = [
        task.id
        for task in tasks
        if task.id not in {FINAL_VERIFY_TASK_ID, repair_task_id}
        and task.status == "verified"
    ]
    title = FINAL_REPAIR_TASK_PREFIX
    if repair_candidates:
        title = f"{FINAL_REPAIR_TASK_PREFIX} ({', '.join(repair_candidates[:3])}{'...' if len(repair_candidates) > 3 else ''})"
    failed_specs = _failed_test_specs_for_repair(project_root, results)
    gate_summaries: list[str] = []
    for gate in non_verified_gates:
        gate_id = str(gate.get("id") or "").strip()
        missing_types = ", ".join(str(value).strip() for value in gate.get("missing_test_types", []) if str(value).strip())
        if gate_id or missing_types:
            gate_summaries.append(f"{gate_id or 'gate'} missing {missing_types or 'required evidence'}")
    missing_type_summary = ", ".join(f"{requirement_id}:{test_type}" for requirement_id, test_type in missing_test_types[:8])
    blocked_reason_parts = ["final verification failed; read docs/reviews/final-repair-report.md first"]
    if repair_candidates:
        blocked_reason_parts.append(f"repair candidates: {', '.join(repair_candidates[:8])}")
    if failed_specs:
        blocked_reason_parts.append(f"failed tests: {', '.join(failed_specs[:8])}")
    if missing_type_summary:
        blocked_reason_parts.append(f"missing test evidence: {missing_type_summary}")
    if gate_summaries:
        blocked_reason_parts.append(f"blocked gates: {'; '.join(gate_summaries[:5])}")

    replacement = Task.from_dict(
        {
            "id": repair_task_id,
            "title": title,
            "status": current_repair_task.status if current_repair_task is not None and current_repair_task.status != "verified" else "pending",
            "task_kind": "repair",
            "requirements": requirements,
            "acceptance_scenarios": [],
            "dependencies": dependencies,
            "output_tests": output_tests,
            "output_paths": output_paths,
            "blocked_reason": "; ".join(blocked_reason_parts),
            "attempts": current_repair_task.attempts if current_repair_task is not None and current_repair_task.status != "verified" else 0,
        }
    )

    updated: list[Task] = []
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
    save_task_runtime_state(
        project_root,
        repair_task_id,
        {
            "force_task_prompt": True,
            "force_task_prompt_reason": "final_verification_repair",
        },
    )
    return updated, repair_task_id, False


def _invalidate_prefinal_audit_after_repair(tasks: list[Task], repair_task_id: str | None) -> list[Task]:
    if not repair_task_id:
        return tasks
    updated: list[Task] = []
    for task in tasks:
        if task.id == PREFINAL_AUDIT_TASK_ID:
            data = task.to_dict()
            dependencies = list(data.get("dependencies", []))
            if repair_task_id not in dependencies:
                dependencies.append(repair_task_id)
            data.update(
                {
                    "status": "pending",
                    "dependencies": dependencies,
                    "git_commit": None,
                    "completed_at": None,
                    "review_status": None,
                    "review_artifact": None,
                    "reviewed_at": None,
                    "verified_at": None,
                    "blocked_reason": "System Gap Fix must re-scan after final repair task " + repair_task_id,
                }
            )
            updated.append(Task.from_dict(data))
            continue
        if task.id == FINAL_VERIFY_TASK_ID:
            data = task.to_dict()
            dependencies = list(data.get("dependencies", []))
            if repair_task_id not in dependencies:
                dependencies.append(repair_task_id)
            data["dependencies"] = dependencies
            updated.append(Task.from_dict(data))
            continue
        updated.append(task)
    return updated


def _requeue_system_gap_fix(tasks: list[Task], reason: str) -> list[Task]:
    updated: list[Task] = []
    for task in tasks:
        if task.id != PREFINAL_AUDIT_TASK_ID:
            updated.append(task)
            continue
        data = task.to_dict()
        data.update(
            {
                "status": "pending",
                "git_commit": None,
                "completed_at": None,
                "review_status": None,
                "review_artifact": None,
                "reviewed_at": None,
                "verified_at": None,
                "blocked_reason": reason,
            }
        )
        updated.append(Task.from_dict(data))
    return updated


def _preserve_task_session(project_root: Path | str, session: RuntimeSession) -> None:
    session.last_heartbeat = utc_now_iso()
    save_current_session(project_root, session)


def _release_session_after_exception(project_root: Path | str, session: RuntimeSession) -> None:
    _preserve_task_session(project_root, session)


def recover(project_root: Path | str) -> list[Task]:
    prune_stale_active_task_records(project_root)
    tasks = all_tasks(project_root)
    original_tasks = [task.to_dict() for task in tasks]
    tasks, _ = repair_invalid_verified_tasks(project_root, tasks)
    reconciled: list[Task] = []
    for task in tasks:
        if task.status not in {"active", "pending"}:
            reconciled.append(task)
            continue
        if task_scoped_changed_paths(project_root, task):
            reconciled.append(task)
            continue
        review_status, review_artifact = review_artifact_status(project_root, task.id)
        if review_status != "pass":
            reconciled.append(task)
            continue
        commit_sha = git_latest_task_commit(project_root, task.id)
        if not commit_sha:
            reconciled.append(task)
            continue
        commit_ts = git_commit_timestamp(project_root, commit_sha) or utc_now_iso()
        reconciled_task = Task.from_dict(
            {
                **task.to_dict(),
                "status": "verified",
                "git_commit": commit_sha,
                "status_session_id": None,
                "completed_at": task.completed_at or commit_ts,
                "review_status": "pass",
                "review_artifact": review_artifact,
                "reviewed_at": task.reviewed_at or commit_ts,
                "verified_at": task.verified_at or commit_ts,
                "verification_source": "reconcile_review_evidence",
                "verification_actor": "framework",
                "verification_reason": "reconciled existing pass review and task-owned commit",
                "blocked_reason": None,
                "attempts": max(task.attempts, 1),
            }
        )
        reconciled.append(reconciled_task)
    tasks = reconciled
    active_session = current_session(project_root)
    active_session_id = str(active_session.id or "").strip() if active_session is not None else ""
    active_session_task_id = str(active_session.current_task_id or "").strip() if active_session is not None else ""
    active_reset_ids: list[str] = []
    for task in tasks:
        if task.status != "active":
            continue
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
        runtime_status = str(runtime_state.get("status") or "").strip()
        task_session_id = str(task.status_session_id or "").strip()
        session_matches = bool(active_session_task_id) and active_session_task_id == task.id and (
            not task_session_id or task_session_id == active_session_id
        )
        if runtime_status == "running" or session_matches:
            continue
        active_reset_ids.append(task.id)
    reset_ids: list[str] = list(active_reset_ids)
    reset_ids.extend(
        task.id
        for task in tasks
        if task.status == "pending"
        and any(
            [
                str(task.status_session_id or "").strip(),
                str(task.started_at or "").strip(),
                str(task.completed_at or "").strip(),
            ]
        )
    )
    updated = tasks
    if reset_ids:
        seen: set[str] = set()
        for task_id in reset_ids:
            if task_id in seen:
                continue
            current = next((task for task in updated if task.id == task_id), None)
            updated = reset_task(updated, task_id)
            if current is not None and str(current.review_status or "").strip().casefold() == "changes_requested":
                updated = mark_task(
                    updated,
                    task_id,
                    "pending",
                    review_status=current.review_status,
                    review_artifact=current.review_artifact,
                    reviewed_at=current.reviewed_at,
                    blocked_reason=current.blocked_reason,
                    attempts=current.attempts,
                )
            seen.add(task_id)
        save_tasks(project_root, updated)
    elif [task.to_dict() for task in updated] != original_tasks:
        save_tasks(project_root, updated)

    if active_session:
        tracked_task_id = str(active_session.current_task_id or "").strip()
        if tracked_task_id and any(
            task.id == tracked_task_id and task.status in {"pending", "active", "review_pending", "exception"}
            for task in updated
        ):
            _preserve_task_session(project_root, active_session)
        else:
            retire_session(project_root, active_session)
    return updated


class DeliveryLoop:
    def __init__(self, project_root: Path | str, *, runtime: str | None = None, max_tasks_per_session: int = 4) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.runtime = resolve_runtime(runtime, project_root=self.project_root)
        self.max_tasks_per_session = max_tasks_per_session
        ensure_runtime_dirs(self.project_root)
        ensure_git_repo(self.project_root)

    def all_done(self, tasks: list[Task]) -> bool:
        actionable = [task for task in tasks if task.id != FINAL_VERIFY_TASK_ID and task.status != "cancelled"]
        return all(task.status == "verified" for task in actionable)

    def _exception_task_ids(self, tasks: list[Task]) -> list[str]:
        return [task.id for task in tasks if task.status == "exception"]

    def _blocked_task_ids(self, tasks: list[Task]) -> list[str]:
        return [task.id for task in tasks if task.status == "blocked"]

    def _review_pending_task_ids(self, tasks: list[Task]) -> list[str]:
        return [task.id for task in tasks if task.status == "review_pending"]

    def _next_active_task_to_resume(self, tasks: list[Task]) -> Task | None:
        active_tasks = [task for task in tasks if task.status == "active" and task.id != FINAL_VERIFY_TASK_ID]
        if not active_tasks:
            return None
        active_session = current_session(self.project_root)
        tracked_task_id = str(active_session.current_task_id or "").strip() if active_session is not None else ""
        if tracked_task_id:
            tracked = next((task for task in active_tasks if task.id == tracked_task_id), None)
            if tracked is not None:
                return tracked
        return active_tasks[0]

    def _next_exception_task_to_retry(self, tasks: list[Task]) -> Task | None:
        pending_tasks = [
            task
            for task in tasks
            if task.status == "pending" and task.id != FINAL_VERIFY_TASK_ID
        ]
        if not pending_tasks:
            return None
        by_id = {task.id: task for task in tasks}
        candidate_ids: list[str] = []
        for task in pending_tasks:
            for dependency_id in task.dependencies:
                dependency = by_id.get(dependency_id)
                if dependency is not None and dependency.status == "exception" and dependency.id not in candidate_ids:
                    candidate_ids.append(dependency.id)
        for task_id in candidate_ids:
            candidate = by_id.get(task_id)
            if candidate is None:
                continue
            if int(candidate.attempts or 0) > 0:
                continue
            if repeated_task_runtime_failure_block(self.project_root, candidate.id) is None:
                return candidate
        return None

    def _pause_requested(self) -> bool:
        return (self.project_root / PAUSE_FILE).exists()

    def _session_for_task(self, task: Task) -> RuntimeSession:
        session = start_task_session(self.project_root, self.runtime, task_id=task.id, title=task.title)
        touch_session(self.project_root, session, task_id=task.id, title=task.title)
        return session

    def _sync_active_task_session_id(self, task_id: str, session: RuntimeSession) -> None:
        session_id = str(session.id or "").strip()
        if not session_id:
            return
        tasks = all_tasks(self.project_root)
        current = next((row for row in tasks if row.id == task_id), None)
        if current is None or current.status != "active" or current.status_session_id == session_id:
            return
        tasks = mark_task(tasks, task_id, "active", status_session_id=session_id)
        save_tasks(self.project_root, tasks)

    def _execute_session_prompt(self, task_id: str, session: RuntimeSession, prompt: str) -> None:
        execute_in_session(self.project_root, session, prompt)
        self._sync_active_task_session_id(task_id, session)

    def _prepare_stalled_runtime_recovery(self, task: Task, session: RuntimeSession, exc: RuntimeErrorResponse) -> tuple[bool, str]:
        runtime_state = normalize_task_runtime_state(load_task_runtime_state(self.project_root, task.id))
        recovery_count = int(runtime_state.get("stalled_recovery_count") or 0) + 1
        if recovery_count > MAX_STALLED_RUNTIME_RECOVERIES:
            return self._block_task_for_runtime_failure(task.id, session, exc)
        resolved_session_id = str(exc.session_id or session.id or task.status_session_id or "").strip()
        runtime_attention = {
            "kind": "stalled_runtime",
            "message": ((exc.output or str(exc)).strip() or "runtime stalled without useful progress"),
            "session_id": resolved_session_id or None,
        }
        recovery_prompt = build_stalled_recovery_prompt(
            self.project_root,
            task,
            runtime_state=runtime_state,
            runtime_attention=runtime_attention,
        )
        tasks = reset_task(all_tasks(self.project_root), task.id, blocked_reason="stalled runtime recovery in progress")
        save_tasks(self.project_root, tasks)
        session.id = resolved_session_id
        session.current_task_id = task.id
        session.last_heartbeat = utc_now_iso()
        save_current_session(self.project_root, session)
        save_task_runtime_state(
            self.project_root,
            task.id,
            {
                "status": "interrupted",
                "completed_at": None,
                "exit_code": None,
                "session_id": resolved_session_id or None,
                "recovery_prompt": recovery_prompt,
                "recovery_reason": "stalled_runtime",
                "recovery_requested_at": utc_now_iso(),
                "stalled_recovery_count": recovery_count,
            },
        )
        return False, "stalled_recovery"

    def run_builtin_scaffold(self) -> dict[str, Any]:
        tasks = all_tasks(self.project_root)
        scaffold_task = next((task for task in tasks if task.id == SCAFFOLD_TASK_ID), None)
        if scaffold_task and scaffold_task.status == "verified":
            current_head = git_head_sha(self.project_root)
            if scaffold_task.git_commit or current_head:
                return {"status": "already_verified", "task_id": SCAFFOLD_TASK_ID, "commit": scaffold_task.git_commit or current_head}
            tasks = reset_task(tasks, scaffold_task.id)
            save_tasks(self.project_root, tasks)
            scaffold_task = next((task for task in tasks if task.id == SCAFFOLD_TASK_ID), None)
        if scaffold_task:
            tasks = mark_task(tasks, scaffold_task.id, "active", git_commit=git_head_sha(self.project_root), blocked_reason=None)
            save_tasks(self.project_root, tasks)
        scaffold_project(self.project_root)
        config_failures = validate_project_config_files(self.project_root)
        if config_failures:
            summary = config_validation_summary(config_failures)
            if scaffold_task:
                exception_report = _write_exception_report(self.project_root, scaffold_task, summary)
                tasks = mark_task(
                    all_tasks(self.project_root),
                    scaffold_task.id,
                    "exception",
                    completed_at=None,
                    verified_at=None,
                    review_artifact=exception_report,
                    reviewed_at=utc_now_iso(),
                    blocked_reason=summary,
                    attempts=1,
                )
                save_tasks(self.project_root, tasks)
            return {"status": "exception", "task_id": SCAFFOLD_TASK_ID, "reason": summary, "config_failures": config_failures}
        review_payload = {
            "status": "pass",
            "summary": "Built-in scaffold executed deterministically.",
            "findings": [],
        }
        if scaffold_task:
            tasks = all_tasks(self.project_root)
            from .loop_review import _write_review_artifact

            review_artifact = _write_review_artifact(self.project_root, scaffold_task, review_payload)
            commit_sha = git_commit_task(
                self.project_root,
                scaffold_task,
                "chore(T000): scaffold project",
                extra_paths=[*SCAFFOLD_OUTPUT_PATHS, review_artifact],
            )
            tasks = mark_task(
                tasks,
                scaffold_task.id,
                "verified",
                completed_at=utc_now_iso(),
                review_status="pass",
                review_artifact=review_artifact,
                reviewed_at=utc_now_iso(),
                git_commit=commit_sha,
                verification_source="builtin_scaffold",
                verification_actor="framework",
                verification_reason="built-in scaffold completed and passed deterministic review",
                blocked_reason=None,
                attempts=1,
            )
            save_tasks(self.project_root, tasks)
        else:
            commit_sha = git_head_sha(self.project_root)
        return {"status": "verified", "task_id": SCAFFOLD_TASK_ID, "commit": commit_sha}

    def _execute_task(self, task: Task) -> tuple[bool, str]:
        session = self._session_for_task(task)
        tasks = all_tasks(self.project_root)
        tasks = mark_task(tasks, task.id, "active", status_session_id=session.id, git_commit=None, blocked_reason=None)
        save_tasks(self.project_root, tasks)
        try:
            if task.id == SHARED_FOUNDATION_TASK_ID:
                shared_env = warm_shared_test_environment(self.project_root, reason="shared foundation")
                if not shared_env.ready:
                    tasks = all_tasks(self.project_root)
                    exception_report = _write_exception_report(self.project_root, task, shared_env.summary)
                    tasks = mark_task(
                        tasks,
                        task.id,
                        "exception",
                        status_session_id=task.status_session_id or str(session.id or "").strip() or None,
                        completed_at=None,
                        verified_at=None,
                        blocked_reason=shared_env.summary,
                        review_artifact=exception_report,
                        reviewed_at=utc_now_iso(),
                        attempts=max(int(task.attempts or 0), 1),
                    )
                    save_tasks(self.project_root, tasks)
                    _release_session_after_exception(self.project_root, session)
                    return False, "exception"
                if project_has_browser_e2e(self.project_root):
                    browser_env = warm_browser_e2e_environment(self.project_root, reason="shared foundation")
                    if not browser_env.ready:
                        tasks = all_tasks(self.project_root)
                        exception_report = _write_exception_report(self.project_root, task, browser_env.summary)
                        tasks = mark_task(
                            tasks,
                            task.id,
                            "exception",
                            status_session_id=task.status_session_id or str(session.id or "").strip() or None,
                            completed_at=None,
                            verified_at=None,
                            blocked_reason=browser_env.summary,
                            review_artifact=exception_report,
                            reviewed_at=utc_now_iso(),
                            attempts=max(int(task.attempts or 0), 1),
                        )
                        save_tasks(self.project_root, tasks)
                        _release_session_after_exception(self.project_root, session)
                        return False, "exception"
            runtime_state = normalize_task_runtime_state(load_task_runtime_state(self.project_root, task.id))
            baseline_changed_paths_value = runtime_state.get("initial_changed_paths")
            if isinstance(baseline_changed_paths_value, list):
                baseline_changed_paths = [str(path).strip() for path in baseline_changed_paths_value if str(path).strip()]
            else:
                baseline_changed_paths = git_changed_paths(self.project_root)
                save_task_runtime_state(
                    self.project_root,
                    task.id,
                    {"initial_changed_paths": baseline_changed_paths},
                )
                runtime_state = normalize_task_runtime_state(load_task_runtime_state(self.project_root, task.id))
            runtime_completed = str(runtime_state.get("status") or "").strip() == "completed"
            review_requested_repair = str(task.review_status or "").strip().casefold() == "changes_requested"
            recovery_prompt = str(runtime_state.get("recovery_prompt") or "").strip()
            force_task_prompt = bool(runtime_state.get("force_task_prompt"))
            if recovery_prompt:
                try:
                    self._execute_session_prompt(task.id, session, recovery_prompt)
                except RuntimeErrorResponse as exc:
                    if exc.kind in {"stalled_runtime", "runtime_interrupted"}:
                        return self._prepare_stalled_runtime_recovery(task, session, exc)
                    return self._block_task_for_runtime_failure(task.id, session, exc)
                save_task_runtime_state(
                    self.project_root,
                    task.id,
                    {
                        "recovery_prompt": None,
                        "recovery_reason": "",
                        "recovery_requested_at": None,
                        "recovery_resumed_at": utc_now_iso(),
                    },
                )
            elif force_task_prompt or review_requested_repair or (not runtime_completed and not task_scoped_changed_paths(self.project_root, task)):
                prompt = build_task_prompt(self.project_root, task)
                try:
                    self._execute_session_prompt(task.id, session, prompt)
                except RuntimeErrorResponse as exc:
                    if exc.kind in {"stalled_runtime", "runtime_interrupted"}:
                        return self._prepare_stalled_runtime_recovery(task, session, exc)
                    return self._block_task_for_runtime_failure(task.id, session, exc)
                if force_task_prompt:
                    save_task_runtime_state(
                        self.project_root,
                        task.id,
                        {
                            "force_task_prompt": False,
                            "force_task_prompt_consumed_at": utc_now_iso(),
                        },
                    )
            elif runtime_completed:
                clear_task_runtime_failure(self.project_root, task.id)
            last_block_reason = "failed after retry budget was exhausted"
            review_status: str | None = None
            cycle_count = 0
            test_fix_attempts = 0
            while True:
                cycle_count += 1
                current = next((current for current in all_tasks(self.project_root) if current.id == task.id), None)
                if current is None:
                    return False, "blocked"
                test_result = run_task_tests(self.project_root, current.to_dict(), attempt=cycle_count)
                if test_result.passed:
                    test_report_artifact = f"docs/reviews/test-report-{current.id}.md"
                    preserved_paths = [
                        *baseline_changed_paths,
                        *_preserved_framework_review_artifact_paths(current, runtime_state),
                    ]
                    baseline_path_set = {str(path).strip() for path in baseline_changed_paths if str(path).strip()}
                    runtime_written_protected_paths = [
                        path
                        for path in git_changed_paths(self.project_root)
                        if is_runtime_protected_framework_artifact(path)
                        and path != test_report_artifact
                        and path not in baseline_path_set
                    ]
                    if runtime_written_protected_paths:
                        restore_paths_to_head(self.project_root, runtime_written_protected_paths)
                    scope_report = task_scope_delta(
                        self.project_root,
                        current,
                        extra_paths=[test_report_artifact],
                        preserved_paths=preserved_paths,
                    )
                    protected_out_of_scope = [path for path in scope_report["out_of_scope"] if is_runtime_protected_framework_artifact(path)]
                    if protected_out_of_scope:
                        restore_paths_to_head(self.project_root, protected_out_of_scope)
                        scope_report = task_scope_delta(
                            self.project_root,
                            current,
                            extra_paths=[test_report_artifact],
                            preserved_paths=preserved_paths,
                        )
                    save_task_runtime_state(
                        self.project_root,
                        current.id,
                        {"pending_scope_report": scope_report},
                    )
                    git_stage_task_snapshot(
                        self.project_root,
                        current,
                        extra_paths=[
                            test_report_artifact,
                            *[path for path in scope_report["out_of_scope"] if not is_runtime_protected_framework_artifact(path)],
                        ],
                        preserved_paths=preserved_paths,
                    )
                    clear_task_exception_patch_conflict(self.project_root, current.id, remove_patch=True)
                    try:
                        write_code_review_request(self.project_root, current, scope_report=scope_report)
                    except RuntimeError as exc:
                        last_block_reason = str(exc)
                        break
                    tasks = all_tasks(self.project_root)
                    tasks = mark_task(
                        tasks,
                        current.id,
                        "review_pending",
                        completed_at=utc_now_iso(),
                        review_status=None,
                        review_artifact=None,
                        reviewed_at=None,
                        verified_at=None,
                        blocked_reason=None,
                        status_session_id=str(session.id or "").strip() or current.status_session_id,
                        attempts=cycle_count,
                    )
                    save_tasks(self.project_root, tasks)
                    clear_task_runtime_failure(self.project_root, current.id)
                    _refresh_project_summary_after_task_update(self.project_root)
                    return False, "review_pending"
                fix_prompt = build_fix_prompt(self.project_root, current, test_results_to_summary([test_result]))
                last_block_reason = test_results_to_summary([test_result]) or "tests failed after retry"
                test_fix_attempts += 1
                if test_fix_attempts > MAX_TEST_FIX_ATTEMPTS:
                    break
                try:
                    self._execute_session_prompt(current.id, session, fix_prompt)
                except RuntimeErrorResponse as exc:
                    if exc.kind in {"stalled_runtime", "runtime_interrupted"}:
                        return self._prepare_stalled_runtime_recovery(current, session, exc)
                    return self._block_task_for_runtime_failure(task.id, session, exc)
            tasks = all_tasks(self.project_root)
            patch_relative_path = park_task_exception_changes(self.project_root, task)
            patch_notice = f" [exception patch: {patch_relative_path}]" if patch_relative_path else ""
            exception_report = _write_exception_report(self.project_root, task, last_block_reason, patch_relative_path=patch_relative_path)
            tasks = mark_task(
                tasks,
                task.id,
                "exception",
                status_session_id=task.status_session_id or str(session.id or "").strip() or None,
                completed_at=None,
                verified_at=None,
                blocked_reason=f"{last_block_reason}{patch_notice}",
                review_status=review_status,
                review_artifact=exception_report,
                reviewed_at=utc_now_iso(),
                attempts=cycle_count,
            )
            save_tasks(self.project_root, tasks)
            save_task_runtime_state(
                self.project_root,
                task.id,
                {
                    "escalation": {
                        "status": "required",
                        "kind": "test_fix_limit",
                        "reason": last_block_reason,
                        "escalated_at": utc_now_iso(),
                        "exception_report": exception_report,
                    }
                },
            )
            _release_session_after_exception(self.project_root, session)
            return False, "exception"
        except Exception as exc:
            tasks = all_tasks(self.project_root)
            patch_relative_path = park_task_exception_changes(self.project_root, task)
            patch_notice = f" [exception patch: {patch_relative_path}]" if patch_relative_path else ""
            exception_report = _write_exception_report(self.project_root, task, f"framework task execution error: {exc}", patch_relative_path=patch_relative_path)
            tasks = mark_task(
                tasks,
                task.id,
                "exception",
                status_session_id=task.status_session_id or str(session.id or "").strip() or None,
                completed_at=None,
                verified_at=None,
                blocked_reason=f"framework task execution error: {exc}{patch_notice}",
                review_artifact=exception_report,
                reviewed_at=utc_now_iso(),
                attempts=max(int(task.attempts or 0), 1),
            )
            save_tasks(self.project_root, tasks)
            _release_session_after_exception(self.project_root, session)
            return False, "exception"

    def _block_task_for_runtime_failure(self, task_id: str, session: RuntimeSession, exc: RuntimeErrorResponse) -> tuple[bool, str]:
        current = next((task for task in all_tasks(self.project_root) if task.id == task_id), None)
        resolved_session_id = str(exc.session_id or session.id or (current.status_session_id if current is not None else "") or "").strip() or None
        failure_prefix = f"[{exc.kind}] " if exc.kind else ""
        failure_message = ((exc.output or str(exc)).strip()[:4000] or str(exc))
        remember_task_runtime_failure(
            self.project_root,
            task_id,
            kind=exc.kind,
            message=failure_message,
            session_id=resolved_session_id,
        )
        patch_relative_path = park_task_exception_changes(self.project_root, current) if current is not None else None
        patch_notice = f" [exception patch: {patch_relative_path}]" if patch_relative_path else ""
        tasks = all_tasks(self.project_root)
        repeated_block = repeated_task_runtime_failure_block(self.project_root, task_id)
        blocked_reason = repeated_block["message"] if isinstance(repeated_block, dict) else (failure_prefix + failure_message)
        exception_task = current if current is not None else Task(task_id, task_id, "exception", [], [], [], [], [])
        exception_report = _write_exception_report(self.project_root, exception_task, blocked_reason, patch_relative_path=patch_relative_path)
        tasks = mark_task(
            tasks,
            task_id,
            "exception",
            status_session_id=resolved_session_id,
            blocked_reason=blocked_reason + patch_notice,
            review_artifact=exception_report,
            reviewed_at=utc_now_iso(),
            attempts=3,
        )
        save_tasks(self.project_root, tasks)
        save_task_runtime_state(
            self.project_root,
            task_id,
            {
                "escalation": {
                    "status": "required",
                    "kind": "runtime_failure",
                    "reason": blocked_reason,
                    "escalated_at": utc_now_iso(),
                    "session_id": resolved_session_id,
                    "failure_kind": exc.kind,
                    "exception_report": exception_report,
                }
            },
        )
        _release_session_after_exception(self.project_root, session)
        return False, "exception"

    def final_verify(self, *, suite_mode: str = "all") -> dict[str, Any]:
        if suite_mode not in {"all", "non-verified"}:
            raise DeliveryError(code="verify_mode_invalid", message=f"unsupported verify mode: {suite_mode}", exit_code=2)
        tasks = all_tasks(self.project_root)
        incomplete_non_final_feature_ids = [
            task.id
            for task in tasks
            if task.id != FINAL_VERIFY_TASK_ID and task.task_kind != "repair" and task.status not in {"verified", "cancelled"}
        ]
        if incomplete_non_final_feature_ids:
            return {
                "passed": False,
                "status": "deferred",
                "summary": "final verification deferred until all non-final feature tasks are verified",
                "repair_candidates": [],
                "repair_task_id": None,
                "repair_report": None,
                "deferred_task_ids": incomplete_non_final_feature_ids,
            }
        invalid_verified = blocking_verified_task_issues(self.project_root, tasks)
        invalid_verified.pop(FINAL_VERIFY_TASK_ID, None)
        if invalid_verified:
            repair_candidates = list(invalid_verified)
            tasks, repair_task_id, repair_limit_reached = _ensure_final_repair_task(
                self.project_root,
                tasks,
                repair_candidates=repair_candidates,
                results=[],
                missing_test_types=[],
                non_verified_gates=[],
            )
            if repair_task_id and not repair_limit_reached:
                tasks = _invalidate_prefinal_audit_after_repair(tasks, repair_task_id)
            tasks = mark_task(
                tasks,
                FINAL_VERIFY_TASK_ID,
                "blocked",
                completed_at=None,
                review_status=None,
                review_artifact=None,
                reviewed_at=None,
                verified_at=None,
                blocked_reason=(
                    f"final verification found invalid verified-task evidence; repair through {repair_task_id}"
                    if repair_task_id
                    else "final verification found invalid verified-task evidence and cannot create another repair bundle"
                ),
            )
            save_tasks(self.project_root, tasks)
            save_task_runtime_state(
                self.project_root,
                FINAL_VERIFY_TASK_ID,
                {
                    "final_verify_status": "repair_required" if repair_task_id else "blocked",
                    "final_verify_invalid_verified_tasks": invalid_verified,
                    "repair_candidates": repair_candidates,
                    "repair_task_id": repair_task_id,
                    "final_repair_limit_reached": repair_limit_reached,
                },
            )
            return {
                "passed": False,
                "status": "repair_required" if repair_task_id else "blocked",
                "summary": "final verification found invalid verified-task evidence",
                "repair_candidates": repair_candidates,
                "repair_task_id": repair_task_id,
                "repair_report": None,
                "invalid_verified_tasks": invalid_verified,
            }
        results = run_full_suite(self.project_root, mode=suite_mode)
        summary = test_results_to_summary(results)
        gates_payload = refresh_gates(self.project_root)
        gate_rows = gates_payload.get("gates") if isinstance(gates_payload.get("gates"), list) else []
        non_verified_gates = [gate for gate in gate_rows if isinstance(gate, dict) and str(gate.get("status") or "").strip() != "verified"]
        requirements_payload = json.loads((self.project_root / "docs" / "requirements.json").read_text(encoding="utf-8"))
        requirement_coverage = check_requirements_coverage(self.project_root, requirements_payload)
        missing_test_types = check_test_type_coverage(self.project_root)
        semantic_findings = scan_production_semantics(self.project_root)
        system_gap_issues = system_gap_ledger_issues(self.project_root)
        environment_blocked = has_environment_failures(results)
        has_system_gap_fix = any(task.id == PREFINAL_AUDIT_TASK_ID for task in tasks)
        effective_system_gap_issues = system_gap_issues if has_system_gap_fix else []
        hard_gates = _final_release_hard_gates(
            full_suite_passed=all(result.passed for result in results),
            requirement_coverage=requirement_coverage,
            missing_test_types=missing_test_types,
            non_verified_gates=non_verified_gates,
            semantic_findings=semantic_findings,
            system_gap_issues=effective_system_gap_issues,
        )
        if effective_system_gap_issues and not environment_blocked:
            tasks = _requeue_system_gap_fix(
                tasks,
                "System Gap Fix must re-scan before final verification: " + "; ".join(effective_system_gap_issues[:3]),
            )
            save_tasks(self.project_root, tasks)
            save_task_runtime_state(
                self.project_root,
                FINAL_VERIFY_TASK_ID,
                {
                    "final_verify_status": "repair_required",
                    "final_verify_system_gap_issues": effective_system_gap_issues,
                    "final_release_hard_gates": hard_gates,
                    "repair_candidates": [PREFINAL_AUDIT_TASK_ID],
                    "repair_task_id": PREFINAL_AUDIT_TASK_ID,
                },
            )
            return {
                "passed": False,
                "status": "repair_required",
                "summary": "final verification requires a fresh System Gap Fix scan",
                "repair_candidates": [PREFINAL_AUDIT_TASK_ID],
                "repair_task_id": PREFINAL_AUDIT_TASK_ID,
                "repair_report": None,
                "system_gap_issues": effective_system_gap_issues,
            }
        semantic_report_artifact = write_semantic_scan_report(self.project_root, semantic_findings)
        release_path = render_release_evidence(self.project_root, summary, requirement_coverage, missing_test_types)
        final_review_path = self.project_root / "docs" / "reviews" / "final-review.md"
        final_review_status = None
        if final_review_path.exists():
            for raw_line in final_review_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if line.lower().startswith("status:"):
                    final_review_status = line.split(":", 1)[1].strip()
                    break
        ready_for_final_review = all(result.passed for result in results) and requirement_coverage["all_covered"] and not missing_test_types and not non_verified_gates and not semantic_findings and not effective_system_gap_issues
        final_pass = ready_for_final_review and str(final_review_status or "").strip().casefold() == "pass"
        if environment_blocked:
            repair_candidates: list[str] = []
        else:
            repair_candidates = _dedupe_task_ids(
                [
                    task_id
                    for gate in non_verified_gates
                    for task_id in gate.get("repair_candidates", [])
                ]
                + infer_final_repair_candidates(self.project_root, results, missing_test_types)
                + _semantic_repair_candidates(tasks, semantic_findings)
            )
        current_repair_task_id = str(normalize_task_runtime_state(load_task_runtime_state(self.project_root, FINAL_VERIFY_TASK_ID)).get("repair_task_id") or "").strip()
        current_repair_task = next((task for task in tasks if task.id == current_repair_task_id), None) if current_repair_task_id else None
        current_repair_is_environment = current_repair_task is not None and current_repair_task.title.startswith(ENVIRONMENT_REPAIR_TASK_PREFIX)
        repair_path_exhausted = current_repair_task is not None and current_repair_task.status == "exception" and (
            current_repair_is_environment if environment_blocked else True
        )
        repair_required = (environment_blocked or bool(repair_candidates)) and not repair_path_exhausted
        repair_task_id = None
        final_repair_limit_reached = False
        if (environment_blocked and current_repair_is_environment and current_repair_task is not None and current_repair_task.status == "exception") or (repair_candidates and current_repair_task is not None and current_repair_task.status == "exception"):
            repair_task_id = current_repair_task.id
        elif repair_required and environment_blocked:
            environment_plan = build_environment_repair_plan(
                self.project_root,
                tasks,
                results=results,
            )
            tasks = environment_plan.tasks
            repair_task_id = environment_plan.repair_task_id
            repair_candidates = environment_plan.repair_candidates
            tasks = _invalidate_prefinal_audit_after_repair(tasks, repair_task_id)
        elif repair_required:
            tasks, repair_task_id, final_repair_limit_reached = _ensure_final_repair_task(
                self.project_root,
                tasks,
                repair_candidates=repair_candidates,
                results=results,
                missing_test_types=missing_test_types,
                non_verified_gates=non_verified_gates,
            )
            if final_repair_limit_reached:
                repair_required = False
            else:
                tasks = _invalidate_prefinal_audit_after_repair(tasks, repair_task_id)
        repair_report_artifact = write_final_repair_report(
            self.project_root,
            results=results,
            requirement_coverage=requirement_coverage,
            missing_test_types=missing_test_types,
            repair_candidates=repair_candidates,
        )
        save_task_runtime_state(
            self.project_root,
            FINAL_VERIFY_TASK_ID,
            {
                "repair_candidates": repair_candidates,
                "repair_task_id": repair_task_id,
                "repair_report_artifact": repair_report_artifact,
                "final_verify_missing_test_types": [list(item) for item in missing_test_types],
                "final_verify_requirement_coverage": requirement_coverage,
                "final_verify_gate_statuses": [
                    {
                        "id": str(gate.get("id") or "").strip(),
                        "status": str(gate.get("status") or "").strip(),
                        "report_artifact": str(gate.get("report_artifact") or "").strip() or None,
                    }
                    for gate in non_verified_gates
                ],
                "final_verify_semantic_findings": [finding.__dict__ for finding in semantic_findings],
                "final_verify_system_gap_issues": effective_system_gap_issues,
                "final_release_hard_gates": hard_gates,
                "semantic_report_artifact": semantic_report_artifact,
                "final_repair_limit_reached": final_repair_limit_reached,
                "final_verify_status": "pass" if final_pass else ("review_pending" if ready_for_final_review else ("environment_blocked" if environment_blocked and repair_required else ("repair_required" if repair_required else "blocked"))),
                **(
                    {
                        "escalation": {
                            "status": "required",
                            "kind": "final_repair_limit",
                            "reason": f"final verification reached maximum repair iterations ({MAX_FINAL_REPAIR_ITERATIONS})",
                            "escalated_at": utc_now_iso(),
                            "repair_task_id": repair_task_id,
                            "repair_candidates": repair_candidates,
                        }
                    }
                    if final_repair_limit_reached
                    else {}
                ),
            },
        )
        if any(task.id == FINAL_VERIFY_TASK_ID for task in tasks):
            if final_pass:
                tasks = mark_task(
                    tasks,
                    FINAL_VERIFY_TASK_ID,
                    "verified",
                    completed_at=utc_now_iso(),
                    review_status="pass",
                    review_artifact="docs/reviews/final-review.md",
                    reviewed_at=utc_now_iso(),
                    verification_source="final_verification",
                    verification_actor="framework",
                    verification_reason="full verification and pass final review evidence",
                    blocked_reason=None,
                )
            elif ready_for_final_review:
                write_final_review_request(
                    self.project_root,
                    results_summary=summary,
                    requirement_coverage=requirement_coverage,
                    missing_test_types=missing_test_types,
                )
                tasks = mark_task(
                    tasks,
                    FINAL_VERIFY_TASK_ID,
                    "review_pending",
                    completed_at=utc_now_iso(),
                    review_status=None,
                    review_artifact=None,
                    reviewed_at=None,
                    blocked_reason=None,
                )
            elif repair_required:
                tasks = mark_task(
                    tasks,
                    FINAL_VERIFY_TASK_ID,
                    "blocked",
                    completed_at=None,
                    review_status=None,
                    review_artifact=None,
                    reviewed_at=None,
                    blocked_reason=(
                        f"final verification created environment repair task {repair_task_id}; repair validation environment readiness first"
                        if environment_blocked and repair_task_id
                        else (
                            f"final verification created repair task {repair_task_id}; preserve verified tasks and repair through that bundle"
                            if repair_task_id
                            else "final verification found repairable issues; repair bundle required"
                        )
                    ),
                )
            else:
                tasks = mark_task(
                    tasks,
                    FINAL_VERIFY_TASK_ID,
                    "blocked",
                    completed_at=None,
                    review_status=None,
                    review_artifact=None,
                    reviewed_at=None,
                    blocked_reason=(
                        f"final verification reached maximum repair iterations ({MAX_FINAL_REPAIR_ITERATIONS}); remaining failures require manual escalation"
                        if final_repair_limit_reached
                        else (
                        f"repair task {repair_task_id} completed but final verification is still failing"
                        if repair_task_id and current_repair_task is not None and current_repair_task.status == "verified"
                        else (
                            f"repair task {repair_task_id} entered exception before final verification could pass"
                            if repair_task_id and current_repair_task is not None and current_repair_task.status == "exception"
                            else (
                                f"final verification has missing test coverage without a strongly attributed repair owner: {_format_missing_test_types(missing_test_types)}"
                                if missing_test_types
                                else "final verification requirements are not yet satisfied"
                            )
                        )
                        )
                    ),
                )
            save_tasks(self.project_root, tasks)
        return {
            "passed": final_pass,
            "status": "pass" if final_pass else ("review_pending" if ready_for_final_review else ("environment_blocked" if environment_blocked and repair_required else ("repair_required" if repair_required else "blocked"))),
            "summary": summary,
            "requirement_coverage": requirement_coverage,
            "missing_test_types": missing_test_types,
            "gates": gates_payload,
            "blocked_gates": non_verified_gates,
            "semantic_findings": [finding.__dict__ for finding in semantic_findings],
            "semantic_report": semantic_report_artifact,
            "repair_candidates": repair_candidates,
            "repair_task_id": repair_task_id,
            "repair_report": repair_report_artifact,
            "final_review": {"status": final_review_status} if final_review_status else None,
            "final_review_artifact": "docs/reviews/final-review.md" if final_review_path.exists() else None,
            "final_review_request": str((self.project_root / ".app-delivery-runtime" / "review-requests" / "final-review.md").relative_to(self.project_root)) if ready_for_final_review else None,
            "release_evidence": str(release_path),
        }

    def run(self, *, max_auto_tasks: int | None = None) -> dict[str, Any]:
        tasks = recover(self.project_root)
        verified_before = sum(
            1
            for task in tasks
            if task.status == "verified" and task.id not in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}
        )
        while not self._pause_requested():
            tasks = all_tasks(self.project_root)
            if self.all_done(tasks):
                return self.final_verify()
            if max_auto_tasks is not None:
                verified_now = sum(
                    1
                    for task in tasks
                    if task.status == "verified" and task.id not in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}
                )
                if verified_now - verified_before >= max_auto_tasks:
                    return {"status": "paused", "reason": f"reached max_auto_tasks={max_auto_tasks}"}
            task = self._next_active_task_to_resume(tasks)
            if task is None:
                task = pick_next_task(tasks)
            if task is None:
                exception_retry = self._next_exception_task_to_retry(tasks)
                if exception_retry is not None:
                    tasks = reset_task(tasks, exception_retry.id)
                    save_tasks(self.project_root, tasks)
                    task = next((row for row in tasks if row.id == exception_retry.id), None)
                    if task is None:
                        return {"status": "blocked", "reason": f"could not reset exception task {exception_retry.id}"}
                else:
                    review_pending_ids = self._review_pending_task_ids(tasks)
                    if review_pending_ids:
                        return {"status": "review_pending", "reason": "external review required", "review_pending_task_ids": review_pending_ids}
                    exception_ids = self._exception_task_ids(tasks)
                    if exception_ids:
                        return {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": exception_ids}
                    blocked_ids = self._blocked_task_ids(tasks)
                    if blocked_ids:
                        return {"status": "blocked", "reason": "no runnable tasks", "blocked_task_ids": blocked_ids}
                    return {"status": "blocked", "reason": "no runnable tasks"}
            if task.id == SCAFFOLD_TASK_ID:
                self.run_builtin_scaffold()
                continue
            if task.id == FINAL_VERIFY_TASK_ID:
                return self.final_verify()
            if task.task_kind == "repair" and task.id.startswith("T"):
                non_repair_incomplete = [
                    row.id
                    for row in tasks
                    if row.id not in {FINAL_VERIFY_TASK_ID, task.id}
                    and row.task_kind != "repair"
                    and row.status not in {"verified", "cancelled"}
                ]
                if non_repair_incomplete:
                    return {
                        "status": "blocked",
                        "task_id": task.id,
                        "reason": "repair task cannot run before all non-final feature tasks are settled",
                        "blocked_task_ids": non_repair_incomplete,
                    }
            success, state = self._execute_task(task)
            if not success:
                if state == "exception":
                    continue
                if state == "stalled_recovery":
                    continue
                if state == "review_pending":
                    return {"status": "review_pending", "task_id": task.id, "reason": "external review required"}
                return {"status": state, "task_id": task.id, "reason": "task execution blocked"}
        return {"status": "paused"}
