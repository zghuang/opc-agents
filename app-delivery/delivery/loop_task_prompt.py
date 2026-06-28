from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .requirements_context import format_acceptance_context, format_requirement_context
from .runtime_config import load_project_metadata
from .state import load_gates, load_test_results, project_paths
from .task import PREFINAL_AUDIT_REPORT_PATH, PREFINAL_AUDIT_TASK_ID, Task, lint_task_contract


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


def build_task_prompt(project_root: Path | str, task: Task) -> str:
    if task.id == PREFINAL_AUDIT_TASK_ID:
        return build_prefinal_audit_prompt(project_root, task)
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
    if task.requirements:
        lines.append(f"Requirements: {', '.join(task.requirements)}")
        lines.append("")
        lines.append("Requirement details:")
        lines.extend(format_requirement_context(project_root, task.requirements) or ["- none"]) 
        lines.append("")
    if task.acceptance_scenarios:
        lines.append(f"Acceptance scenarios: {', '.join(task.acceptance_scenarios)}")
        lines.append("")
        lines.append("Acceptance scenario details:")
        lines.extend(format_acceptance_context(project_root, task.acceptance_scenarios) or ["- none"])
        lines.append("")
    dependency_hints = _relevant_dependency_hints(project_root, task)
    if dependency_hints:
        lines.append("Project metadata declares technology constraints relevant to this task. If the requirement explicitly mandates a package, component library, or framework family, use it instead of silently leaving declared dependencies unused.")
        lines.append("")
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


def build_prefinal_audit_prompt(project_root: Path | str, task: Task) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    lines = [
        f"## Task {task.id}: {task.title}",
        "",
        f"Project path: {project_dir}",
        "",
        "You are running a final pre-release system audit for the current repository state.",
        "This is a new runtime session: do not rely on hidden conversation history. Base conclusions on the files and evidence present in this project directory.",
        "",
        "Mission:",
        "- Read the original requirements, the normalized requirements, any requirement clarification or conflict-resolution decisions, and the current system source code.",
        "- Compare implemented behavior against those requirement sources. Use other project docs, ledgers, reviews, and test evidence only as supporting evidence, not as replacements for the requirement sources.",
        "- Fix release-relevant gaps, incorrect implementations, broken flows, or missing validation evidence that can be responsibly fixed inside this task.",
        f"- Write a detailed audit report to `{PREFINAL_AUDIT_REPORT_PATH}`.",
        "",
        "Authoritative inputs to inspect:",
        "- docs/requirements-source.md",
        "- docs/requirements.json",
        "- docs/clarification-needed.md, docs/clarification-answers.md, or equivalent clarification files, if present",
        "- docs/adr/, docs/adrs/, or equivalent decision/conflict-resolution records, if they clarify or supersede requirements",
        "",
        "Supporting evidence to consult as needed:",
        "- docs/work-items.json, docs/test-plan.json, docs/gates.json, and docs/reviews/",
        "- architecture, UI, module, or design documents only when they help interpret requirements or explain implementation intent",
        "",
        "Code surfaces to inspect:",
        "- Inspect the full application source tree for this project, including production code, tests, fixtures/mocks, integration adapters, UI code if present, configuration, manifests, and app-owned scripts.",
        "- Adapt to the repository's actual stack and layout; do not assume Python, React, backend/frontend folders, or mock-server folders exist.",
        "- Ignore dependency/vendor/build/runtime noise unless it is directly relevant to a requirement or failing behavior.",
        "",
        "Audit rules:",
        "- Do not trust task status, green tests, route existence, or intermediate review summaries as proof of full requirement satisfaction; use them as evidence only.",
        "- Do not mark a requirement as satisfied merely because scaffolding, stubs, shared wiring, or placeholders exist. Require evidence of the actual owned behavior.",
        "- Distinguish complete behavior from partial support, deferred behavior, missing behavior, and incorrect behavior.",
        "- If a user-visible flow is claimed complete, inspect the real user-facing behavior and appropriate end-to-end evidence for this stack.",
        "- If an integration, workflow, background job, data pipeline, or agent capability is claimed complete, inspect the real code path, contracts, fallback behavior, evidence model, and tests.",
        "- If you find a gap that can be fixed without inventing new product scope, fix it and add or strengthen relevant validation.",
        "- If a gap requires major product reinterpretation or external clarification, document it as a blocker instead of hiding it behind a partial fix.",
        "",
        "Scope authority:",
        "- This task has project-wide scope for release-correctness fixes inside the target application project.",
        "- Do not rewrite unrelated code for style, architecture preference, or polish unless it is necessary to close a concrete release gap.",
        "",
        "Validation rules:",
        "- Do not run the full release suite here. The framework will run exhaustive final verification after this audit task is reviewed.",
        "- For fixes made in this task, run focused repository-native validations that are strong enough to prove the fix did not break the touched area.",
        "- Prefer targeted unit, component, contract, or integration tests near the changed code. If a failing test is itself stale or incorrect, repair the test and explain why in the report.",
        "- Run browser/end-to-end checks only when the audit fix changes a user-facing flow or when no lighter validation can credibly prove the behavior.",
        "- Record every validation command and result in the audit report. If a broader validation is deferred to final verification, say so explicitly.",
        "",
        f"Required report: `{PREFINAL_AUDIT_REPORT_PATH}`",
        "The report must contain these markdown sections exactly:",
        "- # System Audit",
        "- ## Audit Scope",
        "- ## Executive Verdict",
        "- ## Fixed Issues",
        "- ## Remaining Gaps / Blockers",
        "- ## Requirement Gap Matrix",
        "- ## Validation Summary",
        "- ## Changed Files",
        "- ## Final Recommendation",
        "",
        "Completion rules:",
        f"- `{PREFINAL_AUDIT_REPORT_PATH}` must exist and be fully populated before stopping.",
        "- If you changed code, tests, config, or mocks, include those files and validation evidence in the report.",
        "- Stop only when all responsibly fixable release gaps are fixed and reported, or when remaining blockers are explicitly documented.",
        "- When this task is complete, blocked, or ready for review, stop and let the framework route the next step.",
    ]
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