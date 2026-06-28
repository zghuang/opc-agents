from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import DeliveryError
from .gates import refresh_gates
from .loop_gitops import git_commit_explicit_paths, git_commit_task
from .requirements_context import format_acceptance_context, format_requirement_context
from .runtime_config import load_project_metadata
from .runtime_config import resolve_project_root
from .session import current_session, retire_session, save_current_session
from .state import ensure_runtime_dirs, load_task_runtime_state, normalize_task_runtime_state, project_paths, save_task_runtime_state, utc_now_iso
from .task import FINAL_VERIFY_TASK_ID, PREFINAL_AUDIT_REPORT_PATH, PREFINAL_AUDIT_TASK_ID, Task, all_tasks, mark_task, save_tasks


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "requirement_assessment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["id", "status", "notes"],
            },
        },
        "acceptance_assessment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["id", "status", "notes"],
            },
        },
    },
    "required": ["status", "summary", "findings"],
}

ALLOWED_REVIEW_STATUSES = {"pass", "changes_requested"}
ALLOWED_ASSESSMENT_STATUSES = {"pass", "changes_requested"}


def _requires_review_matrix(task: Task) -> bool:
    if task.task_kind not in {"feature", "validation", "repair"}:
        return False
    return bool(task.requirements or task.acceptance_scenarios)


def _normalize_review_matrix(
    rows: Any,
    *,
    field_name: str,
    allowed_ids: list[str],
) -> list[dict[str, str]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(f"{field_name} must be an array when provided")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    allowed = set(allowed_ids)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{field_name}[{index}] must be an object")
        item_id = str(row.get("id") or "").strip()
        status = str(row.get("status") or "").strip().casefold()
        notes = str(row.get("notes") or "").strip()
        if not item_id or item_id not in allowed:
            raise ValueError(f"{field_name} contains unknown id: {item_id or '<empty>'}")
        if item_id in seen:
            raise ValueError(f"{field_name} contains duplicate id: {item_id}")
        if status not in ALLOWED_ASSESSMENT_STATUSES:
            raise ValueError(
                f"{field_name} status for {item_id} must be one of: {', '.join(sorted(ALLOWED_ASSESSMENT_STATUSES))}"
            )
        if not notes:
            raise ValueError(f"{field_name} notes must be non-empty for {item_id}")
        normalized.append({"id": item_id, "status": status, "notes": notes})
        seen.add(item_id)
    return normalized


def _validate_pass_review_matrix(task: Task, parsed: dict[str, Any]) -> dict[str, Any]:
    requirement_assessment = _normalize_review_matrix(
        parsed.get("requirement_assessment"),
        field_name="requirement_assessment",
        allowed_ids=task.requirements,
    )
    acceptance_assessment = _normalize_review_matrix(
        parsed.get("acceptance_assessment"),
        field_name="acceptance_assessment",
        allowed_ids=task.acceptance_scenarios,
    )
    parsed["requirement_assessment"] = requirement_assessment
    parsed["acceptance_assessment"] = acceptance_assessment

    if str(parsed.get("status") or "").strip().casefold() != "pass":
        return parsed
    if not _requires_review_matrix(task):
        return parsed

    requirement_ids = {row["id"] for row in requirement_assessment}
    acceptance_ids = {row["id"] for row in acceptance_assessment}
    missing_requirements = [requirement_id for requirement_id in task.requirements if requirement_id not in requirement_ids]
    missing_acceptance = [scenario_id for scenario_id in task.acceptance_scenarios if scenario_id not in acceptance_ids]
    non_passing_requirements = [row["id"] for row in requirement_assessment if row["status"] != "pass"]
    non_passing_acceptance = [row["id"] for row in acceptance_assessment if row["status"] != "pass"]

    problems: list[str] = []
    if missing_requirements:
        problems.append(f"missing requirement assessments: {', '.join(missing_requirements)}")
    if missing_acceptance:
        problems.append(f"missing acceptance assessments: {', '.join(missing_acceptance)}")
    if non_passing_requirements:
        problems.append(f"non-passing requirement assessments: {', '.join(non_passing_requirements)}")
    if non_passing_acceptance:
        problems.append(f"non-passing acceptance assessments: {', '.join(non_passing_acceptance)}")
    if problems:
        raise ValueError(
            "status=pass requires explicit passing review assessments for every declared requirement and acceptance scenario; "
            + "; ".join(problems)
        )
    return parsed


def _relevant_dependency_hints(project_root: Path | str, task: Task) -> list[dict[str, str]]:
    metadata = load_project_metadata(project_root)
    raw_hints = metadata.get("dependency_hints") if isinstance(metadata.get("dependency_hints"), list) else []
    ecosystems = {"project"}
    if any(path.startswith("backend/") for path in task.output_paths):
        ecosystems.add("backend")
    if any(path.startswith("frontend/") for path in task.output_paths):
        ecosystems.add("frontend")
    result: list[dict[str, str]] = []
    for row in raw_hints:
        if not isinstance(row, dict):
            continue
        ecosystem = str(row.get("ecosystem") or "project").strip().lower() or "project"
        if ecosystem not in ecosystems:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        result.append(
            {
                "ecosystem": ecosystem,
                "name": name,
                "reason": str(row.get("reason") or "").strip(),
            }
        )
    return result


def _parse_review_payload(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("reviewer returned empty output")
    candidates = [raw]
    fenced_matches = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fenced_matches if match.strip())
    object_start = raw.find("{")
    object_end = raw.rfind("}")
    if object_start != -1 and object_end != -1 and object_end > object_start:
        candidates.append(raw[object_start : object_end + 1].strip())
    saw_invalid_status = False
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        findings_raw = payload.get("findings")
        findings = [str(item).strip() for item in findings_raw] if isinstance(findings_raw, list) else []
        if not status or not summary:
            continue
        normalized_status = status.casefold()
        if normalized_status not in ALLOWED_REVIEW_STATUSES:
            saw_invalid_status = True
            continue
        payload["status"] = status
        payload["summary"] = summary
        payload["findings"] = [item for item in findings if item]
        return payload
    if saw_invalid_status:
        raise ValueError("review status must be one of: pass, changes_requested")
    raise ValueError("reviewer did not return parseable JSON object")


REVIEW_INPUT_DIRNAME = "review-inputs"
REVIEW_REQUEST_DIRNAME = "review-requests"


def _final_review_path(project_root: Path | str) -> Path:
    return project_paths(project_root).reviews_dir / "final-review.md"


def review_input_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    review_dir = paths.runtime_dir / REVIEW_INPUT_DIRNAME
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir / f"code-review-{task_id}.json"


def final_review_input_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    review_dir = paths.runtime_dir / REVIEW_INPUT_DIRNAME
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir / "final-review.json"


def code_review_request_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    request_dir = paths.runtime_dir / REVIEW_REQUEST_DIRNAME
    request_dir.mkdir(parents=True, exist_ok=True)
    return request_dir / f"code-review-{task_id}.md"


def final_review_request_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    request_dir = paths.runtime_dir / REVIEW_REQUEST_DIRNAME
    request_dir.mkdir(parents=True, exist_ok=True)
    return request_dir / "final-review.md"


def _write_request_if_changed(path: Path, content: str) -> None:
    normalized = str(content or "")
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == normalized:
            return
    path.write_text(normalized, encoding="utf-8")


def build_code_review_request(
    project_root: Path | str,
    task: Task,
    *,
    scope_report: dict[str, Any] | None = None,
) -> str:
    if task.id == PREFINAL_AUDIT_TASK_ID:
        return build_prefinal_audit_review_request(project_root, task, scope_report=scope_report)
    touches_dependency_manifest = any(
        path in {"backend/pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"}
        for path in task.output_paths
    )
    lines = [
        f"Perform an independent code review for task {task.id}.",
        f"Project path: {Path(project_root).expanduser().resolve()}",
        f"Task title: {task.title}",
        "",
        "Requirement details:",
    ]
    lines.extend(format_requirement_context(project_root, task.requirements) or ["- none"])
    lines.append("")
    lines.append("Acceptance scenario details:")
    lines.extend(format_acceptance_context(project_root, task.acceptance_scenarios) or ["- none"])
    lines.append("")
    dependency_hints = _relevant_dependency_hints(project_root, task)
    if dependency_hints:
        lines.append("Project metadata declares technology constraints relevant to this task. If the declared requirement explicitly mandates a package, component library, or framework family, check that the implementation actually uses it or explicitly justifies any non-use.")
        lines.append("")
    lines.append("Review scope guardrails:")
    lines.append("- Judge the task against its declared requirements, acceptance scenarios, output_paths, output_tests, and any necessary shared support edits that are directly required for this task.")
    lines.append("- Do not fail the task solely because the broader architecture or future tasks mention additional technologies, adapters, mocks, or end-to-end flows that are not yet owned by this task's declared scope.")
    lines.append("- If a missing test type is expected to be supplied by a downstream validation task rather than the current feature task, do not fail the current task for that gap alone.")
    lines.append("- Do not reject the task only because it touched files outside the original output_paths when those edits are necessary support work or regression fixes for the current task. Judge those edits on correctness and necessity.")
    lines.append("- Passing task tests is necessary but not sufficient. Review whether the declared requirements and acceptance scenarios are actually complete in behavior, not only whether the declared tests are green.")
    if task.requirements:
        lines.append("- For every declared requirement, include exactly one `requirement_assessment` row with `id`, `status`, and `notes`.")
    if task.acceptance_scenarios:
        lines.append("- For every declared acceptance scenario, include exactly one `acceptance_assessment` row with `id`, `status`, and `notes`.")
    if any(path.startswith("frontend/") for path in task.output_paths) and task.acceptance_scenarios:
        lines.append("- When the task owns a user-visible frontend acceptance scenario, check for browser/e2e evidence in this task unless an explicitly declared downstream validation task owns that exact browser journey.")
        lines.append("- If the user-visible frontend flow is effectively untested in the browser and there is no explicit downstream owner for that browser coverage, return `status=changes_requested`.")
    lines.append("- If any declared requirement or acceptance scenario is incomplete, contradicted, only partially implemented, or only deferred without explicit scope allowance, return `status=changes_requested`.")
    lines.append("")
    if touches_dependency_manifest:
        lines.append("Tech-design check:")
        lines.append("- This task touches dependency manifests. Cross-check the implementation against the high-level `Tech Design` section in AGENTS.md / CLAUDE.md and `docs/architecture.md` rather than requiring a pasted list of all requirement-derived technology hints.")
        lines.append("- Fail the review only when the manifest changes silently contradict project-wide mandated stack choices for this task surface, or when an intentional translation/deferral is unjustified.")
        lines.append("")
    if isinstance(scope_report, dict):
        out_of_scope = [str(path).strip() for path in scope_report.get("out_of_scope", []) if str(path).strip()]
        changed_paths = [str(path).strip() for path in scope_report.get("changed_paths", []) if str(path).strip()]
        lines.append("Scope observations:")
        if out_of_scope:
            lines.append("- The framework detected task changes outside the planned contract scope.")
            for path in out_of_scope:
                lines.append(f"- out_of_scope: {path}")
            lines.append("- Treat these paths as advisory, not an automatic failure.")
            lines.append("- Evaluate whether these paths are necessary shared infrastructure or contract-aligned support for the current task, versus premature implementation of future tasks.")
            lines.append("- If these paths are justified for the task, you may still return status=pass.")
            lines.append("- Necessary shared fixes, wiring, or support code may be acceptable when they are required to complete the current task correctly.")
            lines.append("- If they are not justified, return status=changes_requested and cite the offending paths in findings.")
        else:
            lines.append("- No out-of-scope paths were detected by the framework.")
        if changed_paths:
            lines.append("- Changed paths seen by the framework:")
            for path in changed_paths:
                lines.append(f"  - {path}")
        lines.append("")
    lines.append("Inspect the current working tree and staged diff for this task only.")
    lines.append("Determine whether the task is ready to commit or requires more implementation changes.")
    lines.append("Use status=pass only when the task is ready to accept as-is. Otherwise use status=changes_requested.")
    lines.append("Reply in raw JSON with fields: status, summary, findings (array of strings), requirement_assessment (array), acceptance_assessment (array).")
    return "\n".join(lines)


def build_prefinal_audit_review_request(
    project_root: Path | str,
    task: Task,
    *,
    scope_report: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"Perform an independent review for task {task.id}.",
        f"Project path: {Path(project_root).expanduser().resolve()}",
        f"Task title: {task.title}",
        "",
        "This task is the final pre-release system audit. Review whether the audit work and report are credible for the current repository state.",
        "",
        "Required checks:",
        f"- `{PREFINAL_AUDIT_REPORT_PATH}` exists and contains all required sections.",
        "- The report shows that original requirements, clarification/decision records, architecture, UI design, work-items, gates, reviews, tests, and current source code were inspected.",
        "- The report does not treat scaffolding, route stubs, wiring, or placeholders as full requirement satisfaction without actual behavior evidence.",
        "- Any release-relevant fixes made by this task are coherent, scoped to the project, and validated with appropriate backend/frontend/integration/e2e checks.",
        "- Remaining blockers, if any, are explicit enough to stop T-FINAL from being trusted as a release signal.",
        "",
        "Return status=changes_requested if the report is missing, shallow, contradicted by the code, omits obvious user-facing requirement gaps, lacks validation evidence for changes, or claims readiness while release-blocking issues remain.",
        "Return status=pass only when the audit report and changes are acceptable for T-FINAL to run next.",
    ]
    if isinstance(scope_report, dict):
        changed_paths = [str(path).strip() for path in scope_report.get("changed_paths", []) if str(path).strip()]
        out_of_scope = [str(path).strip() for path in scope_report.get("out_of_scope", []) if str(path).strip()]
        lines.extend(["", "Scope observations:"])
        if changed_paths:
            lines.append("- Changed paths seen by the framework:")
            for path in changed_paths:
                lines.append(f"  - {path}")
        else:
            lines.append("- No changed paths were detected by the framework.")
        if out_of_scope:
            lines.append("- Out-of-scope paths were detected. Because this audit has broad project scope, reject only if these edits are unrelated to release correctness:")
            for path in out_of_scope:
                lines.append(f"  - {path}")
    lines.extend(
        [
            "",
            "Inspect the current repository state and staged diff for this audit task.",
            "Reply in raw JSON with fields: status, summary, findings (array of strings), requirement_assessment (array), acceptance_assessment (array). Use empty arrays for the assessment fields if there are no per-ID rows.",
        ]
    )
    return "\n".join(lines)


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


def build_final_review_request(
    project_root: Path | str,
    *,
    results_summary: str,
    requirement_coverage: dict[str, Any],
    missing_test_types: list[tuple[str, str]],
) -> str:
    lines = [
        "Perform an independent final release review.",
        f"Project path: {Path(project_root).expanduser().resolve()}",
        "",
        "Full suite summary:",
        results_summary or "No test results were recorded.",
        "",
        f"Requirement coverage: total={requirement_coverage.get('total', 0)} covered={requirement_coverage.get('covered', 0)} uncovered={', '.join(requirement_coverage.get('uncovered', [])) or 'none'}",
        "",
        "Missing test-type coverage:",
    ]
    if missing_test_types:
        for requirement_id, test_type in missing_test_types:
            lines.append(f"- {requirement_id}: missing {test_type}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Inspect the repository state and release evidence.",
            "Use status=pass only when the release is ready to accept as-is. Otherwise use status=changes_requested.",
            "Reply in raw JSON with fields: status, summary, findings (array of strings).",
        ]
    )
    return "\n".join(lines)


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


def _write_review_artifact(project_root: Path | str, task: Task, review_payload: dict[str, Any]) -> str:
    paths = project_paths(project_root)
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    review_path = paths.reviews_dir / f"code-review-{task.id}.md"
    status = str(review_payload.get("status") or "changes_requested").strip()
    summary = str(review_payload.get("summary") or "").strip()
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    requirement_assessment = review_payload.get("requirement_assessment") if isinstance(review_payload.get("requirement_assessment"), list) else []
    acceptance_assessment = review_payload.get("acceptance_assessment") if isinstance(review_payload.get("acceptance_assessment"), list) else []
    lines = [
        f"status: {status}",
        "review_type: code",
        f"work_item: {task.id}",
        "",
        "# Code Review",
        "",
        "## Summary",
        "",
        summary or "No summary provided.",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            lines.append(f"- {finding}")
    else:
        lines.append("- No blocking findings.")
    if requirement_assessment:
        lines.extend(["", "## Requirement Assessment", ""])
        for row in requirement_assessment:
            if not isinstance(row, dict):
                continue
            lines.append(f"- {row.get('id', '-')}: {row.get('status', '-')} — {row.get('notes', '')}")
    if acceptance_assessment:
        lines.extend(["", "## Acceptance Assessment", ""])
        for row in acceptance_assessment:
            if not isinstance(row, dict):
                continue
            lines.append(f"- {row.get('id', '-')}: {row.get('status', '-')} — {row.get('notes', '')}")
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(review_path.relative_to(paths.project_root))


def _write_final_review_artifact(project_root: Path | str, review_payload: dict[str, Any]) -> str:
    review_path = _final_review_path(project_root)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    status = str(review_payload.get("status") or "changes_requested").strip()
    summary = str(review_payload.get("summary") or "").strip()
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    lines = [
        f"status: {status}",
        "review_type: final",
        "work_item: T-FINAL",
        "",
        "# Final Review",
        "",
        "## Summary",
        "",
        summary or "No summary provided.",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            lines.append(f"- {finding}")
    else:
        lines.append("- No blocking findings.")
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(review_path.relative_to(project_paths(project_root).project_root))


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
    persisted_input = review_input_path(project_dir, task_id)
    persisted_input.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    review_artifact = _write_review_artifact(project_dir, task, parsed)
    review_status = str(parsed.get("status") or "changes_requested").strip()
    reviewed_at = utc_now_iso()
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
            },
        )
        active_session = current_session(project_dir)
        if active_session is not None and active_session.id == task.status_session_id:
            active_session.current_task_id = None
            active_session.title = active_session.runtime
            save_current_session(project_dir, active_session)
        refresh_gates(project_dir)
        return 0

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
    persisted_input = final_review_input_path(project_dir)
    persisted_input.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    review_artifact = _write_final_review_artifact(project_dir, parsed)
    review_status = str(parsed.get("status") or "changes_requested").strip()
    reviewed_at = utc_now_iso()
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

    tasks = mark_task(
        all_tasks(project_dir),
        FINAL_VERIFY_TASK_ID,
        "blocked",
        review_status=review_status,
        review_artifact=review_artifact,
        reviewed_at=reviewed_at,
        blocked_reason=str(parsed.get("summary") or "final review changes requested").strip() or "final review changes requested",
    )
    save_tasks(project_dir, tasks)
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