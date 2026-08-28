from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .builtin_task_prompts import (
    render_frontend_api_audit_review_request,
    render_prefinal_audit_review_request,
)
from .builtin_tasks import FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from .release_assessment import (
    RELEASE_ASSESSMENT_PATH,
    RELEASE_DIMENSIONS,
    RELEASE_SCORE_THRESHOLD,
    REQUIRED_HARD_GATES,
)
from .requirements_context import format_acceptance_context, format_requirement_context
from .task import Task, normalize_intent_list

REVIEW_PROMPT_NOISE_PATHS = {
    "docs/gates.json",
    "docs/test-results.json",
    "docs/work-items.json",
    "docs/work-items.md",
    "docs/project-summary.json",
    "docs/project-summary.md",
}

REVIEW_PROMPT_NOISE_PREFIXES = (
    ".app-delivery-runtime/",
    "app-delivery-runtime/",
    "docs/reviews/code-review-",
    "docs/reviews/test-report-",
    "docs/reviews/gate-report-",
)

REVIEW_PROMPT_CHANGED_PATH_LIMIT = 12
REVIEW_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "skills" / "code-review" / "references" / "review-contract.md"
SEMANTIC_RISK_REGISTER_PATH = "docs/reviews/semantic-risk-register.json"


def _task_intent_fields(task: Task) -> tuple[str, str, list[str], list[str]]:
    intent = task.intent if isinstance(task.intent, dict) else {}
    return (
        str(intent.get("objective") or "").strip(),
        str(intent.get("journey") or "").strip(),
        normalize_intent_list(intent.get("done_when")),
        normalize_intent_list(intent.get("non_goals")),
    )


def _append_task_intent_section(lines: list[str], task: Task) -> None:
    objective, journey, done_when, non_goals = _task_intent_fields(task)
    if not any([objective, journey, done_when, non_goals]):
        return
    lines.append("Task intent:")
    if objective:
        lines.append(f"- Objective: {objective}")
    if journey:
        lines.append(f"- Journey: {journey}")
    if done_when:
        lines.append("- Done when:")
        lines.extend(f"  - {value}" for value in done_when)
    if non_goals:
        lines.append("- Non-goals:")
        lines.extend(f"  - {value}" for value in non_goals)
    lines.append("")


def _append_review_contract(lines: list[str], task: Task) -> None:
    declared_acceptance = ", ".join(task.acceptance_scenarios) if task.acceptance_scenarios else "none; this task declares no acceptance scenarios, so do not use task_contract_repair"
    text = REVIEW_CONTRACT_PATH.read_text(encoding="utf-8").strip()
    text = text.replace("{{declared_acceptance_scenarios}}", declared_acceptance)
    lines.append(text)
    lines.append("")


def _append_scope_observations(lines: list[str], scope_report: dict[str, Any] | None, *, validation: bool = False) -> None:
    if not isinstance(scope_report, dict):
        return
    out_of_scope = [str(path).strip() for path in scope_report.get("out_of_scope", []) if str(path).strip()]
    changed_paths, omitted_changed_path_count = _review_prompt_changed_paths(scope_report)
    lines.append("Scope observations:")
    if out_of_scope:
        if validation:
            lines.append("- The framework detected changes outside planned validation scope.")
        else:
            lines.append("- The framework detected changes outside planned scope.")
        for path in out_of_scope:
            lines.append(f"- out_of_scope: {path}")
        if validation:
            lines.append("- Pass only if they are necessary validation support; otherwise request changes and cite them.")
        else:
            lines.append("- Pass only if they are necessary support for this task; otherwise request changes and cite them.")
    else:
        lines.append("- No out-of-scope paths were detected by the framework.")
    if changed_paths:
        lines.append("- Implementation-relevant changed paths seen by the framework:")
        for path in changed_paths:
            lines.append(f"  - {path}")
    if omitted_changed_path_count:
        lines.append(f"- Omitted {omitted_changed_path_count} framework-generated or low-signal changed paths from this summary.")
    lines.append("")


def _append_technology_constraints_review_section(lines: list[str], constraints: list[dict[str, Any]] | None) -> None:
    if not constraints:
        return
    lines.append("Technology constraints to verify:")
    for constraint in constraints:
        name = str(constraint.get("name") or "").strip()
        if not name:
            continue
        ecosystem = str(constraint.get("ecosystem") or "project").strip() or "project"
        requirement = str(constraint.get("requirement") or "must_use").strip() or "must_use"
        reason = str(constraint.get("reason") or "").strip() or "Required by planning artifacts."
        source = str(constraint.get("source") or "").strip()
        suffix = f" [source: {source}]" if source else ""
        lines.append(f"- {name} ({ecosystem}, {requirement}): {reason}{suffix}")
        expected_evidence = [str(value).strip() for value in constraint.get("expected_evidence", []) if str(value).strip()] if isinstance(constraint.get("expected_evidence"), list) else []
        if expected_evidence:
            lines.append("  Expected evidence:")
            lines.extend(f"  - {value}" for value in expected_evidence)
    lines.append("- For each listed constraint, include exactly one `technology_assessment` row with name, status, evidence, and notes.")
    lines.append("- A `must_use` constraint without implementation evidence or explicit superseding ADR/clarification requires status=changes_requested.")
    lines.append("")


def _is_review_prompt_noise_path(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return True
    if normalized in REVIEW_PROMPT_NOISE_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in REVIEW_PROMPT_NOISE_PREFIXES)


def _review_prompt_changed_paths(scope_report: dict[str, Any]) -> tuple[list[str], int]:
    raw_changed_paths = [str(path).strip() for path in scope_report.get("changed_paths", []) if str(path).strip()]
    out_of_scope = {str(path).strip() for path in scope_report.get("out_of_scope", []) if str(path).strip()}
    filtered: list[str] = []
    seen: set[str] = set()
    omitted = 0
    for path in raw_changed_paths:
        if path in seen:
            continue
        seen.add(path)
        if path in out_of_scope:
            continue
        if _is_review_prompt_noise_path(path):
            omitted += 1
            continue
        filtered.append(path)
    return filtered[:REVIEW_PROMPT_CHANGED_PATH_LIMIT], omitted + max(0, len(filtered) - REVIEW_PROMPT_CHANGED_PATH_LIMIT)


def build_code_review_request(
    project_root: Path | str,
    task: Task,
    *,
    scope_report: dict[str, Any] | None = None,
) -> str:
    if task.id == FRONTEND_API_AUDIT_TASK_ID:
        return render_frontend_api_audit_review_request(project_root, task, scope_report=scope_report)
    if task.id == PREFINAL_AUDIT_TASK_ID:
        return render_prefinal_audit_review_request(project_root, task, scope_report=scope_report)
    if task.task_kind == "validation":
        return build_validation_code_review_request(project_root, task, scope_report=scope_report)
    touches_dependency_manifest = any(
        path in {"backend/pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"}
        for path in task.output_paths
    )
    lines = [
        f"Perform an independent code review for task {task.id}.",
        f"Project path: {Path(project_root).expanduser().resolve()}",
        f"Task title: {task.title}",
        "",
    ]
    _append_task_intent_section(lines, task)
    lines.append("Requirement details:")
    requirement_context = format_requirement_context(project_root, task.requirements) or []
    lines.extend(requirement_context or ["- none"])
    lines.append("")
    lines.append("Acceptance scenario details:")
    acceptance_context = format_acceptance_context(project_root, task.acceptance_scenarios) or []
    lines.extend(acceptance_context or ["- none"])
    lines.append("")
    _append_technology_constraints_review_section(lines, task.technology_constraints)
    _append_review_contract(lines, task)
    lines.append("Feature review checks:")
    lines.append("- Judge declared requirements, acceptance scenarios, output_paths, output_tests, and necessary support edits.")
    lines.append("- Missing behavior, placeholders/stubs as complete, unmet IDs, and unowned deferrals are blocking findings.")
    lines.append("- Production-path routes, services, workflows, or UI data flows must not satisfy requirements through hardcoded/default responses, static fake data, or stub shims unless the requirement explicitly declares mock/dev-only behavior.")
    lines.append("- For security, access-control, or data-isolation requirements, verify the project's declared enforcement model on the owning route or service path.")
    lines.append("- For declared AI, automation, workflow, or external-integration tasks, verify real case/context/tool/service behavior, not only static demo payloads.")
    if any(path.startswith("frontend/") for path in task.output_paths) and task.acceptance_scenarios:
        lines.append("- Frontend acceptance flows need browser/e2e evidence here unless a declared downstream validation task owns the exact journey.")
    lines.append("")
    if touches_dependency_manifest:
        lines.append("Tech-design check:")
        lines.append("- Manifest changes must align with `Tech Design` in AGENTS.md / CLAUDE.md and `docs/architecture.md`.")
        lines.append("- Fail silent contradictions or unjustified deferrals of mandated stack choices.")
        lines.append("")
    _append_scope_observations(lines, scope_report)
    lines.append("Inspect current repository state and the task-scoped snapshot only.")
    lines.append("Use `pass` only when the task is acceptable as-is; otherwise use `changes_requested`.")
    return "\n".join(lines)


def build_validation_code_review_request(
    project_root: Path | str,
    task: Task,
    *,
    scope_report: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"Perform an independent code review for validation task {task.id}.",
        f"Project path: {Path(project_root).expanduser().resolve()}",
        f"Task title: {task.title}",
        "",
        "This is a validation-task review. Judge whether the task delivered credible executable validation assets for its owned requirements and acceptance scenarios.",
        "",
    ]
    _append_task_intent_section(lines, task)
    lines.append("Requirement details:")
    requirement_context = format_requirement_context(project_root, task.requirements) or []
    lines.extend(requirement_context or ["- none"])
    lines.append("")
    lines.append("Acceptance scenario details:")
    acceptance_context = format_acceptance_context(project_root, task.acceptance_scenarios) or []
    lines.extend(acceptance_context or ["- none"])
    lines.append("")
    _append_technology_constraints_review_section(lines, task.technology_constraints)
    _append_review_contract(lines, task)
    lines.append("Validation review checks:")
    lines.append("- Declared output_tests are contractual validation entrypoints.")
    lines.append("- Tests or commands must exist, run, and prove the owned requirements and scenarios.")
    lines.append("- Each declared scenario must map to assertions, fixtures, scenario data, or browser/e2e coverage.")
    lines.append("- Mocked browser tests using `page.route()` count as mocked-browser evidence, not real backend E2E evidence.")
    lines.append("- Production gates must block demo constants, hardcoded API responses, missing declared security/access-control enforcement, and log-only side-effect or external-boundary behavior.")
    lines.append("- Return `status=changes_requested` for placeholder tests, shallow smoke checks, missing scenario coverage, weak assertions, or tests that cannot actually execute.")
    lines.append("- Support code is acceptable only when needed for truthful validation.")
    lines.append("- Fail broad product implementation that belongs to upstream feature tasks.")
    lines.append("")
    _append_scope_observations(lines, scope_report, validation=True)
    lines.append("Inspect current repository state and the validation-task snapshot only.")
    lines.append("Use `pass` only when validation evidence is credible, executable, and scoped.")
    return "\n".join(lines)


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
    risk_path = Path(project_root).expanduser().resolve() / SEMANTIC_RISK_REGISTER_PATH
    try:
        risk_payload = json.loads(risk_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        risk_payload = {}
    risks = risk_payload.get("risks") if isinstance(risk_payload, dict) and isinstance(risk_payload.get("risks"), list) else []
    lines.extend(["", "Deferred semantic risks:"])
    if risks:
        for risk in risks:
            if not isinstance(risk, dict):
                continue
            categories = risk.get("categories") if isinstance(risk.get("categories"), list) else []
            category_text = ", ".join(str(value) for value in categories) or str(risk.get("category") or "semantic-risk")
            lines.append(
                f"- {risk.get('task_id', '-')}: {category_text}; repeat_count={risk.get('repeat_count', '-')}; review={risk.get('review_artifact', '-')}"
            )
        lines.append("These risks were deferred only to keep implementation moving; final release review must decide whether they block acceptance.")
    else:
        lines.append("- none")
    final_state_path = Path(project_root).expanduser().resolve() / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json"
    try:
        final_state = json.loads(final_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        final_state = {}
    framework_gates = final_state.get("final_release_hard_gates") if isinstance(final_state, dict) else None
    lines.extend(["", "Framework hard-gate attestation:"])
    if isinstance(framework_gates, list):
        for gate in framework_gates:
            if isinstance(gate, dict):
                lines.append(f"- {gate.get('id', '-')}: {gate.get('status', '-')}")
    else:
        lines.append("- missing; do not submit pass until final verification writes this attestation")
    lines.extend(
        [
            "",
            "Informational release assessment:",
            f"- Write `{RELEASE_ASSESSMENT_PATH}` as JSON to publish a human-readable scorecard.",
            f"- Set threshold to {RELEASE_SCORE_THRESHOLD}. Score must equal the sum of the dimension scores.",
            "- Include these dimensions with their fixed maximum weights:",
            *[f"  - {dimension_id}: {weight}" for dimension_id, weight in RELEASE_DIMENSIONS],
            "- Every dimension must include non-empty notes and evidence.",
            "- Include these hard gates with status pass or fail and non-empty evidence:",
            *[f"  - {gate_id}" for gate_id in REQUIRED_HARD_GATES],
            "- The score is informational only. A score below 80, a failed scorecard hard-gate row, or a missing framework attestation must not create repair work or change final-review status.",
            "- The framework's independent verification, validation gates, production semantic scan, and System Gap Fix checks remain the actual release controls.",
            "",
            "Inspect the repository state and release evidence.",
            "Use status=pass only when the release is ready to accept as-is. Otherwise use status=changes_requested.",
            "Reply in raw JSON with fields: status, summary, findings (array of finding objects). Use an empty findings array when there are no findings.",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "build_code_review_request",
    "build_final_review_request",
    "build_validation_code_review_request",
]
