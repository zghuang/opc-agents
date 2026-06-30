from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .requirements_context import format_acceptance_context, format_requirement_context
from .runtime_config import load_project_metadata
from .state import load_gates, load_test_results, project_paths
from .builtin_task_prompts import render_frontend_api_audit_prompt, render_prefinal_audit_prompt
from .builtin_tasks import FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from .task import Task, lint_task_contract


FINAL_REPAIR_REPORT_PATH = "docs/reviews/final-repair-report.md"
FINAL_VERIFICATION_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"


def _prompt_dir(project_root: Path | str) -> Path:
    paths = project_paths(project_root)
    prompt_dir = paths.runtime_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    return prompt_dir


def write_task_prompt(project_root: Path | str, task: Task, prompt: str) -> Path:
    path = _prompt_dir(project_root) / f"{task.id}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def _normalize_path(path: str) -> str:
    normalized = str(path or "").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _path_overlaps(scope: str, candidate: str) -> bool:
    normalized_scope = _normalize_path(scope).rstrip("/")
    normalized_candidate = _normalize_path(candidate).rstrip("/")
    if not normalized_scope or not normalized_candidate:
        return False
    return (
        normalized_candidate == normalized_scope
        or normalized_candidate.startswith(normalized_scope + "/")
        or normalized_scope.startswith(normalized_candidate + "/")
    )


def _relevant_failed_results(task: Task, test_results: dict[str, object]) -> list[dict[str, object]]:
    rows = test_results.get("results") if isinstance(test_results.get("results"), list) else []
    relevant: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or bool(row.get("passed")):
            continue
        row_task_id = str(row.get("task_id") or "").strip()
        if row_task_id == task.id:
            relevant.append(row)
            continue
        row_test_files = row.get("test_files") if isinstance(row.get("test_files"), list) else []
        if any(
            _path_overlaps(scope, str(candidate))
            for scope in task.output_tests
            for candidate in row_test_files
        ):
            relevant.append(row)
    return relevant[-5:]


def _relevant_gate_rows(project_root: Path | str, task: Task) -> list[dict[str, object]]:
    payload = load_gates(project_root)
    rows = payload.get("gates") if isinstance(payload.get("gates"), list) else []
    relevant: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip()
        if status == "verified":
            continue
        scope_tasks = {str(value).strip() for value in row.get("scope_tasks", []) if str(value).strip()}
        scope_requirements = {str(value).strip() for value in row.get("scope_requirements", []) if str(value).strip()}
        repair_candidates = {str(value).strip() for value in row.get("repair_candidates", []) if str(value).strip()}
        if task.id in scope_tasks or task.id in repair_candidates:
            relevant.append(row)
            continue
        if task.task_kind == "repair" and scope_requirements.intersection(task.requirements):
            relevant.append(row)
    return relevant


def _has_browser_style_test(task: Task) -> bool:
    for spec in task.output_tests:
        lowered = str(spec).strip().lower()
        if lowered.startswith("frontend/e2e/"):
            return True
        if "playwright" in lowered or "/e2e/" in lowered or "npm run e2e" in lowered:
            return True
    return False


def _dependency_hint_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", str(name or "").casefold()) if len(token) >= 3]


def _task_hint_context(task: Task, requirement_context: list[str], acceptance_context: list[str]) -> str:
    parts = [
        task.title,
        " ".join(task.requirements),
        " ".join(task.acceptance_scenarios),
        " ".join(task.output_paths),
        " ".join(task.output_tests),
        " ".join(requirement_context),
        " ".join(acceptance_context),
    ]
    return "\n".join(parts).casefold()


def _task_touches_manifest_for_ecosystem(task: Task, ecosystem: str) -> bool:
    paths = set(task.output_paths)
    if ecosystem == "backend":
        return bool(paths.intersection({"backend/pyproject.toml", "backend/uv.lock"}))
    if ecosystem == "frontend":
        return bool(paths.intersection({"frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"}))
    if ecosystem in {"infra", "project"}:
        return any(path in paths for path in {"docker-compose.yml", "README.md"})
    return False


def _dependency_hint_relevant(task: Task, hint: dict[str, str], context: str) -> bool:
    ecosystem = str(hint.get("ecosystem") or "project").strip().lower() or "project"
    if _task_touches_manifest_for_ecosystem(task, ecosystem):
        return True
    name = str(hint.get("name") or "").strip()
    if not name:
        return False
    if name.casefold() in context:
        return True
    tokens = _dependency_hint_tokens(name)
    return bool(tokens) and any(token in context for token in tokens)


def _relevant_dependency_hints(project_root: Path | str, task: Task, requirement_context: list[str] | None = None, acceptance_context: list[str] | None = None) -> list[dict[str, str]]:
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
        hint = {
            "ecosystem": ecosystem,
            "name": name,
            "reason": str(row.get("reason") or "").strip(),
            "source": str(row.get("source") or "").strip(),
            "evidence": str(row.get("evidence") or "").strip(),
        }
        context = _task_hint_context(task, requirement_context or [], acceptance_context or [])
        if _dependency_hint_relevant(task, hint, context):
            result.append(hint)
    return result


def _append_dependency_hint_section(lines: list[str], dependency_hints: list[dict[str, str]]) -> None:
    if not dependency_hints:
        return
    lines.append("Project technology constraints for this task:")
    for hint in dependency_hints:
        suffix = ""
        evidence = str(hint.get("evidence") or "").strip()
        source = str(hint.get("source") or "").strip()
        if evidence or source:
            suffix = f" [{'; '.join(part for part in [source, evidence] if part)}]"
        reason = str(hint.get("reason") or "").strip() or "Required by project planning artifacts."
        lines.append(f"- {hint['name']} ({hint['ecosystem']}): {reason}{suffix}")
    lines.append("- Treat the listed technologies as task requirements unless an existing ADR or clarification explicitly supersedes them; do not replace them with custom implementations silently.")
    lines.append("")


def _append_task_intent_section(lines: list[str], task: Task) -> None:
    intent = task.intent if isinstance(task.intent, dict) else {}
    objective = str(intent.get("objective") or "").strip()
    journey = str(intent.get("journey") or "").strip()
    done_when = [str(value).strip() for value in intent.get("done_when", []) if str(value).strip()]
    non_goals = [str(value).strip() for value in intent.get("non_goals", []) if str(value).strip()]
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


def _format_id_summary(values: list[str], *, limit: int = 20) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized:
        return "none"
    if len(normalized) <= limit:
        return ", ".join(normalized)
    return f"{', '.join(normalized[:limit])}, ... ({len(normalized)} total)"


def _is_final_repair_task(task: Task) -> bool:
    title = str(task.title or "").strip()
    return task.task_kind == "repair" and (
        title.startswith(FINAL_VERIFICATION_REPAIR_TASK_PREFIX)
        or title.startswith(FINAL_REVIEW_REPAIR_TASK_PREFIX)
    )


def _repair_evidence_paths(project_root: Path | str, task: Task) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    evidence: list[str] = []
    title = str(task.title or "").strip()
    if title.startswith(FINAL_VERIFICATION_REPAIR_TASK_PREFIX) and (project_dir / FINAL_REPAIR_REPORT_PATH).exists():
        evidence.append(FINAL_REPAIR_REPORT_PATH)
    for path in task.output_paths:
        normalized = _normalize_path(path)
        if normalized.startswith("docs/reviews/") and normalized.endswith(".md") and normalized not in evidence:
            evidence.append(normalized)
    return evidence[:5]


def _append_repair_focus_section(lines: list[str], project_root: Path | str, task: Task) -> None:
    if task.task_kind != "repair":
        return
    lines.append("Repair objective:")
    lines.append("- This is a focused repair task, not a fresh feature build. Do not re-derive the whole product from every requirement before fixing the concrete failure.")
    lines.append("- Start with the repair evidence, failed tests, blocked gates, declared tests, and declared paths in this prompt.")
    lines.append("- Read requirement details from docs/requirements.json only for requirement IDs directly tied to the failing evidence you are repairing.")
    evidence_paths = _repair_evidence_paths(project_root, task)
    if evidence_paths:
        lines.append("- Read these repair evidence artifacts first:")
        lines.extend(f"  - {path}" for path in evidence_paths)
    if task.blocked_reason:
        lines.append(f"- Repair summary: {task.blocked_reason}")
    if _is_final_repair_task(task):
        lines.append("- For final repair bundles, close the listed final verification or final review failures; do not attempt a broad second implementation pass.")
    lines.append("")


def build_task_prompt(project_root: Path | str, task: Task) -> str:
    if task.id == FRONTEND_API_AUDIT_TASK_ID:
        prompt = render_frontend_api_audit_prompt(project_root, task)
        write_task_prompt(project_root, task, prompt)
        return prompt
    if task.id == PREFINAL_AUDIT_TASK_ID:
        prompt = render_prefinal_audit_prompt(project_root, task)
        write_task_prompt(project_root, task, prompt)
        return prompt
    if task.task_kind == "validation":
        return build_validation_task_prompt(project_root, task)
    project_dir = Path(project_root).expanduser().resolve()
    test_results = load_test_results(project_dir)
    failed_summary = []
    for row in _relevant_failed_results(task, test_results):
        failed_summary.append(f"- {row.get('task_id')}: {row.get('test_files')} -> {row.get('failed_count')} failed")
    contract_lint = lint_task_contract(task)
    gate_rows = _relevant_gate_rows(project_root, task)
    lines = [
        f"## Task {task.id}: {task.title}",
        "",
        f"Project path: {project_dir}",
        "",
    ]
    _append_task_intent_section(lines, task)
    _append_repair_focus_section(lines, project_root, task)
    if task.requirements:
        if task.task_kind == "repair":
            lines.append(f"Requirement IDs potentially affected: {_format_id_summary(task.requirements)}")
            requirement_context = []
        else:
            lines.append(f"Requirements: {', '.join(task.requirements)}")
            lines.append("")
            lines.append("Requirement details:")
            requirement_context = format_requirement_context(project_root, task.requirements) or []
            lines.extend(requirement_context or ["- none"])
        lines.append("")
    else:
        requirement_context = []
    if task.acceptance_scenarios:
        if task.task_kind == "repair":
            lines.append(f"Acceptance scenario IDs potentially affected: {_format_id_summary(task.acceptance_scenarios)}")
            acceptance_context = []
        else:
            lines.append(f"Acceptance scenarios: {', '.join(task.acceptance_scenarios)}")
            lines.append("")
            lines.append("Acceptance scenario details:")
            acceptance_context = format_acceptance_context(project_root, task.acceptance_scenarios) or []
            lines.extend(acceptance_context or ["- none"])
        lines.append("")
    else:
        acceptance_context = []
    dependency_hints = _relevant_dependency_hints(project_root, task, requirement_context, acceptance_context)
    _append_dependency_hint_section(lines, dependency_hints)
    if task.output_paths:
        lines.append(f"Paths: {', '.join(task.output_paths)}")
        lines.append("")
    if task.output_tests:
        lines.append(f"Tests: {', '.join(task.output_tests)}")
        lines.append("")
    if contract_lint["warnings"]:
        lines.append("Task contract warnings:")
        for warning in contract_lint["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    if failed_summary:
        lines.append("Relevant recent failing test summary:")
        lines.extend(failed_summary)
        lines.append("")
    if gate_rows:
        lines.append("Related gate requirements:")
        for gate in gate_rows:
            required = ", ".join(str(value).strip() for value in gate.get("required_test_types", []) if str(value).strip()) or "none"
            observed = ", ".join(str(value).strip() for value in gate.get("observed_test_types", []) if str(value).strip()) or "none"
            missing = ", ".join(str(value).strip() for value in gate.get("missing_test_types", []) if str(value).strip()) or "none"
            reason = str(gate.get("blocked_reason") or "").strip() or str(gate.get("status") or "").strip() or "gate requirements outstanding"
            report_artifact = str(gate.get("report_artifact") or "").strip() or "none"
            lines.append(
                f"- {str(gate.get('id') or '-').strip()}: status={str(gate.get('status') or '-').strip()} required={required} observed={observed} missing={missing} reason={reason} report={report_artifact}"
            )
        lines.append("")
    touches_dependency_manifest = any(
        path in {"backend/pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"}
        for path in task.output_paths
    )
    if str(task.review_status or "").strip().casefold() == "changes_requested":
        lines.append("Previous independent code review requested changes:")
        if task.blocked_reason:
            lines.append(f"- Review summary: {task.blocked_reason}")
        if task.review_artifact:
            lines.append(f"- Review artifact: {task.review_artifact}")
            lines.append("- Read that artifact and repair the cited issues before re-running validation.")
        else:
            lines.append("- Repair the cited review issues before re-running validation.")
        lines.append("")
    elif task.task_kind == "repair" and task.blocked_reason:
        lines.append("Repair context:")
        lines.append(f"- {task.blocked_reason}")
        lines.append("- Start from the failed validation evidence and the declared tests below; do not broaden into unrelated product work.")
        lines.append("- If this bundle contains many gaps, enumerate them and repair in small verified batches; do not stop after only the first easy fix while declared failures remain.")
        lines.append("- If context pressure or runtime interruption stops this session, leave progress on disk and let the framework resume from the remaining evidence.")
        lines.append("")
    has_backend_specs = any(spec.startswith("backend/") for spec in task.output_tests)
    has_frontend_specs = any(spec.startswith("frontend/") for spec in task.output_tests)
    has_frontend_paths = any(path.startswith("frontend/") for path in task.output_paths)
    has_browser_specs = _has_browser_style_test(task)
    lines.append("Follow the runtime baseline in AGENTS.md / CLAUDE.md. For this task:")
    lines.append("- Prefer the declared output paths and output tests, but if completing the requirement or fixing regressions in the current task needs adjacent support-file edits, make them in this task and validate them here instead of stopping early on scope alone.")
    lines.append("- Treat the declared output_tests as the minimum validation floor, not as proof by themselves that the requirement is done. Passing tests are necessary but not sufficient when the requirement or acceptance details describe stronger behavior.")
    lines.append("- If the declared requirement or acceptance behavior is still not demonstrated inside this task's scope, add or strengthen task-local tests before stopping, then run the updated tests in the same task.")
    if has_frontend_paths and task.acceptance_scenarios:
        lines.append("- This task owns frontend-facing acceptance behavior. Include browser/e2e coverage for the user-visible flow in this task unless a declared downstream validation task explicitly owns that exact browser journey.")
        if not has_browser_specs:
            lines.append("- No browser/e2e test is currently declared for this frontend acceptance slice. Add one now unless the downstream validation owner is explicit in the task graph.")
    lines.append("- Complete only the current task. Do not start the next task, pre-implement future work, or widen scope after this task's declared tests pass.")
    if has_backend_specs:
        lines.append("- Backend validation: prefer package-relative commands from `backend/`, for example `cd backend && uv run pytest ...`, using the repository environment rather than bare system python.")
    if has_frontend_specs:
        lines.append("- Frontend validation: prefer the scripts declared in `frontend/package.json` (`npm run test`, `typecheck`, `lint`, `build`, `e2e`) instead of custom one-off commands.")
    if touches_dependency_manifest:
        lines.append("- This task touches dependency manifests. Consult the `Tech Design` section in AGENTS.md / CLAUDE.md plus `docs/architecture.md` before changing project-wide stack choices.")
        lines.append("- When a requirement mandates a technology family rather than a literal package name, resolve the exact package entry deliberately instead of dumping all possible dependencies into the manifest.")
    lines.append("- If previously passing tests outside this task's declared scope regress after your changes, determine whether the regression is caused by your implementation, stale tests, or both, then apply the minimal correct fix.")
    lines.append("- When the current task is complete, blocked, or ready for review, stop and wait for the framework to route the next step.")
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt)
    return prompt


def build_validation_task_prompt(project_root: Path | str, task: Task) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    test_results = load_test_results(project_dir)
    failed_summary = []
    for row in _relevant_failed_results(task, test_results):
        failed_summary.append(f"- {row.get('task_id')}: {row.get('test_files')} -> {row.get('failed_count')} failed")
    contract_lint = lint_task_contract(task)
    gate_rows = _relevant_gate_rows(project_root, task)
    lines = [
        f"## Task {task.id}: {task.title}",
        "",
        f"Project path: {project_dir}",
        "",
    ]
    _append_task_intent_section(lines, task)
    if task.requirements:
        lines.append(f"Requirements: {', '.join(task.requirements)}")
        lines.append("")
        lines.append("Requirement details:")
        requirement_context = format_requirement_context(project_root, task.requirements) or []
        lines.extend(requirement_context or ["- none"])
        lines.append("")
    else:
        requirement_context = []
    if task.acceptance_scenarios:
        lines.append(f"Acceptance scenarios: {', '.join(task.acceptance_scenarios)}")
        lines.append("")
        lines.append("Acceptance scenario details:")
        acceptance_context = format_acceptance_context(project_root, task.acceptance_scenarios) or []
        lines.extend(acceptance_context or ["- none"])
        lines.append("")
    else:
        acceptance_context = []
    dependency_hints = _relevant_dependency_hints(project_root, task, requirement_context, acceptance_context)
    _append_dependency_hint_section(lines, dependency_hints)
    if task.output_paths:
        lines.append(f"Paths: {', '.join(task.output_paths)}")
        lines.append("")
    if task.output_tests:
        lines.append(f"Tests: {', '.join(task.output_tests)}")
        lines.append("")
    if contract_lint["warnings"]:
        lines.append("Task contract warnings:")
        for warning in contract_lint["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    if failed_summary:
        lines.append("Relevant recent failing test summary:")
        lines.extend(failed_summary)
        lines.append("")
    if gate_rows:
        lines.append("Related gate requirements:")
        for gate in gate_rows:
            required = ", ".join(str(value).strip() for value in gate.get("required_test_types", []) if str(value).strip()) or "none"
            observed = ", ".join(str(value).strip() for value in gate.get("observed_test_types", []) if str(value).strip()) or "none"
            missing = ", ".join(str(value).strip() for value in gate.get("missing_test_types", []) if str(value).strip()) or "none"
            reason = str(gate.get("blocked_reason") or "").strip() or str(gate.get("status") or "").strip() or "gate requirements outstanding"
            report_artifact = str(gate.get("report_artifact") or "").strip() or "none"
            lines.append(
                f"- {str(gate.get('id') or '-').strip()}: status={str(gate.get('status') or '-').strip()} required={required} observed={observed} missing={missing} reason={reason} report={report_artifact}"
            )
        lines.append("")
    if str(task.review_status or "").strip().casefold() == "changes_requested":
        lines.append("Previous independent code review requested changes:")
        if task.blocked_reason:
            lines.append(f"- Review summary: {task.blocked_reason}")
        if task.review_artifact:
            lines.append(f"- Review artifact: {task.review_artifact}")
            lines.append("- Read that artifact and repair the cited issues before re-running validation.")
        else:
            lines.append("- Repair the cited review issues before re-running validation.")
        lines.append("")

    has_backend_specs = any(spec.startswith("backend/") for spec in task.output_tests)
    has_frontend_specs = any(spec.startswith("frontend/") for spec in task.output_tests)
    has_frontend_paths = any(path.startswith("frontend/") for path in task.output_paths)
    has_browser_specs = _has_browser_style_test(task)

    lines.append("Validation task mission:")
    lines.append("- This is a validation task. Your primary responsibility is to complete or strengthen the declared validation assets for the requirements and acceptance scenarios above.")
    lines.append("- After this runtime turn, the app-delivery control loop will deterministically execute the declared Tests. Treat those Tests as contractual validation entrypoints, not optional examples.")
    lines.append("- Make sure every declared test path or command exists, is runnable in this repository, and produces meaningful evidence for the owned scenarios.")
    lines.append("- Prefer improving or adding acceptance tests, end-to-end tests, fixtures, scenario data, and test helpers before changing product code.")
    lines.append("- If a declared scenario cannot be validated truthfully without a small supporting implementation fix, apply the minimal code change required to enable the validation and keep it tightly scoped.")
    lines.append("- Do not turn this task into broad new product implementation that belongs to upstream feature tasks.")
    lines.append("")
    lines.append("Execution guidance:")
    lines.append("- Use the declared Paths and Tests as the main scope and delivery contract for this task.")
    lines.append("- Treat passing tests as necessary but not sufficient. Do not stop if the declared acceptance scenarios are still not directly represented by executable validation.")
    lines.append("- If existing tests are weak, incomplete, missing fixtures, or not executable, fix them in this task.")
    lines.append("- If a scenario remains unprovable without broad new product work, stop and surface the blocker instead of faking coverage.")
    if task.acceptance_scenarios:
        lines.append("- Each declared acceptance scenario should map to at least one concrete validation path, assertion set, or explicitly documented coverage route in this task.")
    if has_frontend_paths or any(str(value).strip().startswith("frontend/") for value in task.output_tests):
        lines.append("- This task owns frontend-facing validation coverage. Keep browser/e2e checks explicit for user-visible flows rather than relying only on unit tests.")
        if not has_browser_specs:
            lines.append("- No browser/e2e test is currently declared for the frontend-facing scenarios in this validation task. Add one unless the task contract is explicitly wrong and must be corrected upstream.")
    lines.append("- Complete only the current task. Do not start the next task, pre-implement future work, or widen scope after this task's declared tests pass.")
    if has_backend_specs:
        lines.append("- Backend validation: prefer package-relative commands from `backend/`, for example `cd backend && uv run pytest ...`, using the repository environment rather than bare system python.")
    if has_frontend_specs:
        lines.append("- Frontend validation: prefer the scripts declared in `frontend/package.json` (`npm run test`, `typecheck`, `lint`, `build`, `e2e`) instead of custom one-off commands.")
    lines.append("- When the current task is complete, blocked, or ready for review, stop and wait for the framework to route the next step.")
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt)
    return prompt


def build_fix_prompt(project_root: Path | str, task: Task, test_summary: str) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    lines = [
        f"The tests for task {task.id} are still failing.",
        f"Task: {task.title}",
        "",
        f"Project path: {project_dir}",
        "",
    ]
    if task.output_paths:
        lines.append(f"Declared output paths: {', '.join(task.output_paths)}")
    if task.output_tests:
        lines.append(f"Declared output tests: {', '.join(task.output_tests)}")
    if task.output_paths or task.output_tests:
        lines.append("")
    lines.extend(
        [
            "Failure summary:",
            test_summary,
            "",
            "Repair guidance:",
            "- Stay inside this project root and task contract. Do not search sibling projects or the framework repo unless you have concrete evidence of a framework defect.",
            "- Start from the failing declared tests and the declared output paths above. Prefer fixing project-local implementation and test wiring before broad exploration.",
            "- Backend validation: run package-relative commands from `backend/`, for example `cd backend && uv run pytest ...`.",
            "- Frontend validation: run the scripts declared in `frontend/package.json`, including Playwright from `frontend/` when the failing spec is under `frontend/e2e/`.",
            "- If the failure is in browser/e2e coverage, inspect the project-local route/auth wiring, Playwright config, and backend startup assumptions before searching generic framework files.",
            "- Repair the implementation or the tests if they are brittle, then stop as soon as the current task is complete, blocked, or ready for review.",
        ]
    )
    return "\n".join(lines)


def build_stalled_recovery_prompt(
    project_root: Path | str,
    task: Task,
    *,
    runtime_state: dict[str, Any],
    runtime_attention: dict[str, Any] | None = None,
) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    test_results = load_test_results(project_dir)
    failed_summary = []
    for row in _relevant_failed_results(task, test_results):
        failed_summary.append(f"- {row.get('task_id')}: {row.get('test_files')} -> {row.get('failed_count')} failed")
    lines = [
        f"Previous run for task {task.id} appears stalled.",
        f"Task: {task.title}",
        "",
        f"Project path: {project_dir}",
        f"Declared output paths: {', '.join(task.output_paths) if task.output_paths else 'none'}",
        f"Declared output tests: {', '.join(task.output_tests) if task.output_tests else 'none'}",
        "",
        "Stall evidence:",
        f"- Previous session id: {str(runtime_state.get('session_id') or '').strip() or 'unknown'}",
        f"- Previous runtime started_at: {str(runtime_state.get('started_at') or '').strip() or 'unknown'}",
        f"- Last tool event: {str((runtime_attention or {}).get('last_tool_name') or runtime_state.get('last_tool_name') or 'unknown').strip() or 'unknown'}",
        f"- Last tool timestamp: {str(runtime_state.get('last_tool_at') or 'unknown').strip() or 'unknown'}",
        f"- Last file mutation timestamp: {str(runtime_state.get('last_mutation_at') or 'none recorded').strip() or 'none recorded'}",
    ]
    if runtime_attention:
        lines.append(f"- Attention signal: {str(runtime_attention.get('kind') or 'stalled').strip()} — {str(runtime_attention.get('message') or '').strip() or 'Runtime stalled without meaningful progress.'}")
    lines.append("")
    if failed_summary:
        lines.append("Current failed-test context:")
        lines.extend(failed_summary)
        lines.append("")
    lines.extend(
        [
            "Recovery instructions:",
            "- Continue from the existing implementation already on disk. Inspect current task-scoped changes before creating new files or restarting broad exploration.",
            "- Do not re-scan the whole repository from scratch. Start from the declared output paths, declared output tests, and the stall evidence above.",
            "- If the current worktree already contains task-relevant changes, reconcile those changes with the failing tests before adding more code.",
            "- Backend validation: run package-relative commands from `backend/`, for example `cd backend && uv run pytest ...`.",
            "- Frontend validation: run the scripts declared in `frontend/package.json`, including Playwright from `frontend/` when a failing spec is under `frontend/e2e/`.",
            "- When the task is complete, blocked, or ready for review, stop immediately and let the framework route the next step.",
        ]
    )
    return "\n".join(lines)


def build_scope_fix_prompt(task: Task, message: str) -> str:
    return (
        f"Task {task.id} produced changes outside its declared scope.\n"
        f"Task: {task.title}\n\n"
        f"Scope error:\n{message}\n\n"
        "Move the implementation back into the declared output paths and declared test paths, or reduce unintended file edits, then stop."
    )