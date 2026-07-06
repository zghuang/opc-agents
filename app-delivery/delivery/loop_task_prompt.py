from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .requirements_context import format_acceptance_context, format_requirement_context
from .state import load_gates, load_task_runtime_state, load_test_results, project_paths
from .test_env import frontend_e2e_env_prefix
from .builtin_task_prompts import render_frontend_api_audit_prompt, render_prefinal_audit_prompt
from .builtin_tasks import FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, SHARED_FOUNDATION_TASK_ID
from .loop_gitops import repair_invalid_verified_tasks
from .task import Task, all_tasks, lint_task_contract


FINAL_REPAIR_REPORT_PATH = "docs/reviews/final-repair-report.md"
FINAL_VERIFICATION_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"
MAX_RUNTIME_ATTENTION_MESSAGE_CHARS = 1000
MAX_RUNTIME_ATTENTION_DETAIL_LINES = 6
APP_DELIVERY_HEARTBEAT_RE = re.compile(
    r"^\[app-delivery\]\s+(?P<phase>[A-Za-z0-9_.-]+)\s+(?P<label>.*?):\s+still running(?:\s+\((?P<elapsed>\d+)s elapsed\))?\s*$"
)
OPENCODE_EVENT_LINE_RE = re.compile(r'^\{\s*"type"\s*:\s*"(?:step_start|step_finish|tool_use|text|app-delivery-wrapper-(?:start|end))"')
SHELL_METADATA_RE = re.compile(r"</?shell_metadata>")


def _prompt_dir(project_root: Path | str) -> Path:
    paths = project_paths(project_root)
    prompt_dir = paths.runtime_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    return prompt_dir


def _prompt_history_dir(project_root: Path | str, task: Task) -> Path:
    paths = project_paths(project_root)
    history_dir = paths.runtime_dir / "prompt-history" / task.id
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def _safe_prompt_kind(prompt_kind: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(prompt_kind or "task").strip().lower()).strip("-._")
    return normalized or "task"


def _prompt_history_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _prompt_history_path(project_root: Path | str, task: Task, prompt_kind: str) -> Path:
    history_dir = _prompt_history_dir(project_root, task)
    timestamp = _prompt_history_timestamp()
    safe_kind = _safe_prompt_kind(prompt_kind)
    candidate = history_dir / f"{timestamp}-{safe_kind}.md"
    suffix = 2
    while candidate.exists():
        candidate = history_dir / f"{timestamp}-{safe_kind}-{suffix}.md"
        suffix += 1
    return candidate


def _archive_task_prompt(project_root: Path | str, task: Task, prompt: str, *, prompt_kind: str) -> None:
    try:
        _prompt_history_path(project_root, task, prompt_kind).write_text(prompt, encoding="utf-8")
    except OSError:
        return


def write_task_prompt(project_root: Path | str, task: Task, prompt: str, *, prompt_kind: str = "task") -> Path:
    path = _prompt_dir(project_root) / f"{task.id}.md"
    path.write_text(prompt, encoding="utf-8")
    _archive_task_prompt(project_root, task, prompt, prompt_kind=prompt_kind)
    return path


def _normalize_path(path: str) -> str:
    normalized = str(path or "").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _append_exception_patch_conflict_section(lines: list[str], project_root: Path | str, task: Task) -> None:
    runtime_state = load_task_runtime_state(project_root, task.id)
    conflict = runtime_state.get("exception_patch_conflict")
    if not isinstance(conflict, dict) or str(conflict.get("status") or "").strip() != "conflict":
        return
    patch_path = str(conflict.get("patch_path") or "").strip() or f".app-delivery-runtime/exception-patches/{task.id}.patch"
    brief_path = str(conflict.get("conflict_brief_path") or "").strip() or f".app-delivery-runtime/exception-conflicts/{task.id}.md"
    affected_paths = [str(path).strip() for path in conflict.get("affected_paths", []) if str(path).strip()] if isinstance(conflict.get("affected_paths"), list) else []
    affected_summary = ", ".join(affected_paths) if affected_paths else "unknown"
    git_output = str(conflict.get("git_output") or "").strip()
    lines.extend(
        [
            "Exception patch conflict:",
            f"- Patch artifact: {patch_path}",
            f"- Conflict brief: {brief_path}",
            f"- Affected paths: {affected_summary}",
            "- First resolve this inside the current implementation runtime session; do not invoke an external review runner for this merge step.",
            "- Do not add Git conflict markers. Read the patch and current files, then migrate the patch intent into the current code directly.",
            "- Preserve current verified-task behavior unless the patch intent requires a narrow compatible change.",
            "- After resolving the patch conflict, continue this task's normal implementation and declared tests.",
        ]
    )
    if git_output:
        lines.extend(["- git apply --check output:"])
        for raw_line in git_output.splitlines()[:8]:
            line = raw_line.strip()
            if line:
                lines.append(f"  {line}")
    lines.append("")


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


def _trim_attention_details(lines: list[str]) -> str:
    details: list[str] = []
    for line in lines:
        stripped = str(line or "").strip()
        if not stripped:
            continue
        if details and stripped == details[-1]:
            continue
        details.append(stripped)
        if len(details) >= MAX_RUNTIME_ATTENTION_DETAIL_LINES:
            break
    text = "\n".join(details).strip()
    if len(text) > MAX_RUNTIME_ATTENTION_MESSAGE_CHARS:
        text = text[:MAX_RUNTIME_ATTENTION_MESSAGE_CHARS].rstrip() + "..."
    return text


def _format_heartbeat_summary(heartbeat_rows: list[dict[str, object]]) -> str:
    first = heartbeat_rows[0]
    phase = str(first.get("phase") or "runtime").strip() or "runtime"
    label = str(first.get("label") or "task").strip() or "task"
    same_target = all(str(row.get("phase") or "").strip() == phase and str(row.get("label") or "").strip() == label for row in heartbeat_rows)
    target = f" for {phase} {label}" if same_target else ""
    elapsed_values = [int(row["elapsed"]) for row in heartbeat_rows if isinstance(row.get("elapsed"), int)]
    elapsed = ""
    if elapsed_values:
        elapsed = f"; elapsed range {elapsed_values[0]}s-{elapsed_values[-1]}s"
    return f"Wrapper heartbeat summary: {len(heartbeat_rows)} repeated still-running lines{target}{elapsed}."


def _is_noisy_runtime_attention_line(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return True
    if OPENCODE_EVENT_LINE_RE.match(stripped):
        return True
    if SHELL_METADATA_RE.search(stripped):
        return True
    if stripped.startswith(('"type":"', '"part":{', '"state":{', '"metadata":{', '"output":"', '"input":{')):
        return True
    return False


def normalize_runtime_attention_message(message: object) -> str:
    raw = str(message or "").strip()
    if not raw:
        return "Runtime stalled without meaningful progress."
    heartbeat_rows: list[dict[str, object]] = []
    detail_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = APP_DELIVERY_HEARTBEAT_RE.match(stripped)
        if match:
            elapsed_text = match.group("elapsed")
            heartbeat_rows.append(
                {
                    "phase": match.group("phase"),
                    "label": match.group("label"),
                    "elapsed": int(elapsed_text) if elapsed_text else None,
                }
            )
            continue
        if _is_noisy_runtime_attention_line(stripped):
            continue
        detail_lines.append(stripped)
    if not heartbeat_rows:
        clean_lines = [line for line in raw.splitlines() if not _is_noisy_runtime_attention_line(line)]
        return _trim_attention_details(clean_lines) or "Runtime stalled without meaningful progress."
    parts = [_format_heartbeat_summary(heartbeat_rows)]
    detail_text = _trim_attention_details(detail_lines)
    if detail_text:
        parts.append(detail_text)
    return "\n".join(parts)


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


def _has_python_package_output_dir(task: Task) -> bool:
    return any(
        str(path).strip().endswith("/")
        and str(path).strip().startswith(("backend/", "mock-server/"))
        for path in task.output_paths
    )


def _append_python_package_placement_rule(lines: list[str], task: Task) -> None:
    if not _has_python_package_output_dir(task):
        return
    lines.append(
        "- For Python package directories, put implementation in module files; keep `__init__.py` to exports or light initialization."
    )


def _append_validation_command_guidance(lines: list[str], project_root: Path | str, task: Task, *, has_backend_specs: bool, has_frontend_specs: bool) -> None:
    if has_backend_specs:
        lines.append("- Backend validation: prefer package-relative commands from `backend/`, for example `cd backend && uv run pytest ...`, using the repository environment rather than bare system python.")
    if has_frontend_specs:
        lines.append("- Frontend validation: prefer the scripts declared in `frontend/package.json` (`npm run test`, `typecheck`, `lint`, `build`, `e2e`) instead of custom one-off commands.")
    if has_frontend_specs and _has_browser_style_test(task):
        e2e_prefix = frontend_e2e_env_prefix(project_root).strip()
        if e2e_prefix:
            lines.append(f"- Browser/e2e validation: if you manually run Playwright or `npm run e2e`, prefix it with `{e2e_prefix}` so the local backend is started for Vite API proxy requests.")


def _append_production_fidelity_rule(lines: list[str]) -> None:
    lines.append("- Production fidelity: do not satisfy requirements, API contracts, or tests by adding production-path hardcoded/default responses, static fixtures, fake data, or stub routes. Use real service/database/tool/external-system behavior; if that is too broad for this task, document a blocking gap instead of faking completion. Keep fake behavior only in explicit mock/dev fixtures.")


def _append_technology_constraints_section(lines: list[str], constraints: list[dict[str, Any]] | None) -> None:
    if not constraints:
        return
    lines.append("Required technology constraints for this task:")
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
    lines.append("- Treat these as task-scoped implementation requirements unless a superseding ADR or clarification is explicit.")
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
    lines.append("- Fix the cited failure; do not restart the feature build.")
    lines.append("- Start with repair evidence, failed tests, blocked gates, declared tests, and declared paths.")
    lines.append("- Read requirement details only for IDs tied to the failure.")
    evidence_paths = _repair_evidence_paths(project_root, task)
    if evidence_paths:
        lines.append("- Read these repair evidence artifacts first:")
        lines.extend(f"  - {path}" for path in evidence_paths)
    if task.blocked_reason:
        lines.append(f"- Repair summary: {task.blocked_reason}")
    if _is_final_repair_task(task):
        lines.append("- For final repair bundles, close listed final failures only.")
    lines.append("")


def _append_invalid_verified_context(lines: list[str], project_root: Path | str) -> None:
    tasks = all_tasks(project_root)
    task_by_id = {task.id: task for task in tasks}
    _, issues = repair_invalid_verified_tasks(project_root, tasks)
    if not issues:
        return
    lines.append("Verified upstream evidence issues:")
    for task_id, issue in list(issues.items())[:5]:
        upstream_task = task_by_id.get(task_id)
        context_paths = [f".app-delivery-runtime/prompts/{task_id}.md"]
        if upstream_task is not None and upstream_task.review_artifact:
            context_paths.append(upstream_task.review_artifact)
        test_report = f"docs/reviews/test-report-{task_id}.md"
        if (Path(project_root).expanduser().resolve() / test_report).exists():
            context_paths.append(test_report)
        lines.append(f"- {task_id}: {issue}")
        lines.append(f"  Context: {', '.join(context_paths)}")
    lines.append("- Do not reopen, reset, or re-run verified feature tasks to repair these issues.")
    lines.append("- If the current task already touches the affected behavior, repair it here as a necessary adjacent support change and validate it under the current task's evidence.")
    lines.append("- If the issue is outside the current task's scope, leave the verified task unchanged and report that a dedicated repair bundle is needed.")
    lines.append("")


def _is_real_backend_validation_gate(task: Task) -> bool:
    if str(task.task_kind or "").strip() != "validation":
        return False
    tests = {str(value).strip() for value in task.output_tests}
    paths = {str(value).strip() for value in task.output_paths}
    return (
        "frontend/e2e/real-backend.spec.ts" in tests
        and "scripts/e2e-backend.sh" in paths
        and "scripts/seed-backend.sh" in paths
    )


def _is_production_gate_task(task: Task) -> bool:
    return str(task.task_kind or "").strip() == "validation" and str(task.title or "").startswith("Production Gate:")


def _append_production_gate_source_guidance(lines: list[str], task: Task) -> None:
    lines.append("Production gate source check:")
    lines.append("- Read the relevant sections of `docs/requirements-source.md` and `docs/architecture.md` before changing code; generated summaries may omit contract details.")
    lines.append("- Inspect the current implementation and tests for this gate's scope before deciding whether the issue is code, test, data, or environment setup.")
    lines.append("- If `docs/reviews/production-gate-*.md` is declared in this task's output paths, create or update it with the source sections consulted, production-path scan findings, executable test evidence, and any blocking gaps.")
    lines.append("- Keep repairs limited to evidence needed by this production gate; do not restart broad feature implementation.")
    lines.append("")


def _append_security_access_gate_guidance(lines: list[str]) -> None:
    lines.append("Security/access-control gate focus:")
    lines.append("- Scan the authentication, authorization, access-control, data-protection, and sensitive-operation paths the current project claims to implement before adding tests.")
    lines.append("- Validate both allow and deny paths for the project's declared model, such as RBAC, ABAC, policy rules, tenant/resource scopes, ownership checks, or unauthenticated public endpoints.")
    lines.append("- Treat production paths that bypass declared security rules, rely on dev-only shortcuts, or are protected only by test fixtures as blocking findings unless the source docs explicitly allow them.")
    lines.append("")


def _append_agent_reality_gate_guidance(lines: list[str]) -> None:
    lines.append("Declared AI/automation integration gate focus:")
    lines.append("- First confirm from source docs and architecture which AI, automation, model, tool, or external-service integration boundaries this project actually declares.")
    lines.append("- Scan only those declared boundaries, including adapters, orchestration, tool calls, schemas, persistence, retry/timeout handling, and observability when they exist.")
    lines.append("- Flag static canned outputs or bypassed integration paths when the project claims a real integration boundary; local simulators are acceptable only when they exercise that boundary's contract rather than replace its result.")
    lines.append("- Validate request/response contracts, evidence or trace references when declared, failure handling, and state persistence across the owned boundary.")
    lines.append("")


def _append_execution_loop_gate_guidance(lines: list[str]) -> None:
    lines.append("Declared process/execution gate focus:")
    lines.append("- First confirm from source docs and architecture which approval, workflow, execution, dispatch, rollback, or manual-control behavior this project actually declares.")
    lines.append("- Scan only those declared process boundaries, including state transitions, side effects, auditability, authorization, idempotency, and external-system handoff when they exist.")
    lines.append("- Validate the declared unhappy paths as well as the happy path, such as rejection, retry, timeout, cancellation, rollback, or manual override where applicable.")
    lines.append("- Treat in-memory happy-path-only evidence, missing side-effect verification, or reports that do not trace to executable boundary tests as blocking findings.")
    lines.append("")


def _append_production_gate_specific_guidance(lines: list[str], task: Task) -> None:
    title = str(task.title or "").casefold()
    paths_and_tests = " ".join([*(task.output_paths or []), *(task.output_tests or [])]).casefold()
    if "security" in title or "rbac" in title or "access control" in title or "/security" in paths_and_tests:
        _append_security_access_gate_guidance(lines)
    if "agent" in title or "mcp" in paths_and_tests or "/agents/" in paths_and_tests:
        _append_agent_reality_gate_guidance(lines)
    if "approval" in title or "execution loop" in title or "execution_dispatch" in paths_and_tests or "dispatch_to_external_system" in paths_and_tests:
        _append_execution_loop_gate_guidance(lines)


def _append_real_backend_gate_guidance(lines: list[str]) -> None:
    lines.append("Real-backend E2E focus:")
    lines.append("- Start from `frontend/e2e/real-backend.spec.ts`, `scripts/e2e-backend.sh`, and `scripts/seed-backend.sh`; do not begin with broad repository exploration.")
    lines.append("- Reproduce the latest failing real-backend spec first, then inspect backend startup, migration, and seed behavior before changing unrelated feature code.")
    lines.append("- Run Playwright from the `frontend/` directory against the declared spec path `e2e/real-backend.spec.ts`; do not run project-root paths like `frontend/e2e/real-backend.spec.ts` through a separate Playwright context.")
    lines.append("- Do not create alternate or throwaway specs as completion evidence; repair the declared real-backend spec and report instead.")
    lines.append("- Scan `frontend/src/` and `frontend/e2e/` for mocked, hardcoded, or fallback API data on flows where real backend routes already exist.")
    lines.append("- Compare frontend API usage with backend routes and schemas; core real-backend validation should use live backend behavior rather than page.route, fixtures, or static demo responses.")
    lines.append("- Check backend health/login endpoints and the exact failing API assertion path before widening scope.")
    lines.append("- If backend startup is broken, repair environment, migration, or seed steps first; do not spend the turn on unrelated placeholder tests.")
    lines.append("")


def _append_review_repair_context(
    lines: list[str],
    project_root: Path | str,
    task: Task,
    *,
    requirement_context: list[str],
    acceptance_context: list[str],
    gate_rows: list[dict[str, object]],
    failed_summary: list[str],
) -> None:
    lines.append("Review repair mode:")
    lines.append("- Repair the review findings; do not re-derive the task.")
    if task.blocked_reason:
        lines.append(f"- Review summary: {task.blocked_reason}")
    if task.review_artifact:
        lines.append(f"- Review artifact: {task.review_artifact}")
        lines.append("- Read the review artifact first and use it as the primary repair brief for this turn.")
    if task.requirements:
        lines.append(f"- Declared requirement IDs: {', '.join(task.requirements)}")
    if task.acceptance_scenarios:
        lines.append(f"- Declared acceptance IDs: {', '.join(task.acceptance_scenarios)}")
    if task.output_paths:
        lines.append(f"- Declared output paths: {', '.join(task.output_paths)}")
    if task.output_tests:
        lines.append(f"- Declared output tests: {', '.join(task.output_tests)}")
    _append_python_package_placement_rule(lines, task)
    if requirement_context:
        lines.append("")
        lines.append("Relevant requirement context:")
        lines.extend(requirement_context)
    if acceptance_context:
        lines.append("")
        lines.append("Relevant acceptance context:")
        lines.extend(acceptance_context)
    if failed_summary:
        lines.append("")
        lines.append("Current failing-test context:")
        lines.extend(failed_summary)
    if gate_rows:
        lines.append("")
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
    lines.append("Repair priorities:")
    lines.append("- Start from the affected behavior named by review.")
    lines.append("- Keep the task boundary; edit adjacent support files only when needed for the fix.")
    lines.append("- Re-run task-local validation, then stop for review.")
    lines.append("")


def build_review_repair_prompt(project_root: Path | str, task: Task) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    test_results = load_test_results(project_dir)
    failed_summary = []
    for row in _relevant_failed_results(task, test_results):
        failed_summary.append(f"- {row.get('task_id')}: {row.get('test_files')} -> {row.get('failed_count')} failed")
    gate_rows = _relevant_gate_rows(project_root, task)
    lines = [
        f"## Task {task.id}: {task.title}",
        "",
        f"Project path: {project_dir}",
        "",
    ]
    _append_exception_patch_conflict_section(lines, project_root, task)
    _append_task_intent_section(lines, task)
    requirement_context = format_requirement_context(project_root, task.requirements) or []
    acceptance_context = format_acceptance_context(project_root, task.acceptance_scenarios) or []
    _append_technology_constraints_section(lines, task.technology_constraints)
    _append_review_repair_context(
        lines,
        project_root,
        task,
        requirement_context=requirement_context,
        acceptance_context=acceptance_context,
        gate_rows=gate_rows,
        failed_summary=failed_summary,
    )
    _append_invalid_verified_context(lines, project_root)
    has_backend_specs = any(spec.startswith("backend/") for spec in task.output_tests)
    has_frontend_specs = any(spec.startswith("frontend/") for spec in task.output_tests)
    has_frontend_paths = any(path.startswith("frontend/") for path in task.output_paths)
    has_browser_specs = _has_browser_style_test(task)
    touches_dependency_manifest = any(
        path in {"backend/pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"}
        for path in task.output_paths
    )
    lines.append("Execution guidance:")
    lines.append("- Continue from current code; the review artifact is the repair brief.")
    lines.append("- Green tests are not enough; prove the review findings are fixed.")
    lines.append("- Add or strengthen task-local tests when proof is missing.")
    _append_production_fidelity_rule(lines)
    if has_frontend_paths and task.acceptance_scenarios:
        lines.append("- If review affects frontend acceptance behavior, keep browser/e2e coverage here unless a declared downstream validation task owns the exact journey.")
        if not has_browser_specs:
            lines.append("- No browser/e2e test is currently declared for this frontend acceptance slice. Add one now unless the downstream validation owner is explicit in the task graph.")
    lines.append("- Complete only the current task. Do not start the next task, pre-implement future work, or widen scope after the review issues are repaired.")
    _append_validation_command_guidance(lines, project_root, task, has_backend_specs=has_backend_specs, has_frontend_specs=has_frontend_specs)
    if touches_dependency_manifest:
        lines.append("- This task touches dependency manifests. Consult the `Tech Design` section in AGENTS.md / CLAUDE.md plus `docs/architecture.md` before changing project-wide stack choices.")
        lines.append("- Resolve exact packages deliberately; do not dump unused stack dependencies into manifests.")
    lines.append("- If your changes break nearby previously passing tests, apply the minimal correct fix.")
    lines.append("- When the current task is complete, blocked, or ready for review, stop and wait for the framework to route the next step.")
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt, prompt_kind="review-repair")
    return prompt


def build_task_prompt(project_root: Path | str, task: Task) -> str:
    if task.id == FRONTEND_API_AUDIT_TASK_ID:
        prompt = render_frontend_api_audit_prompt(project_root, task)
        write_task_prompt(project_root, task, prompt, prompt_kind="task")
        return prompt
    if task.id == PREFINAL_AUDIT_TASK_ID:
        prompt = render_prefinal_audit_prompt(project_root, task)
        write_task_prompt(project_root, task, prompt, prompt_kind="task")
        return prompt
    if task.task_kind == "validation":
        return build_validation_task_prompt(project_root, task)
    if str(task.review_status or "").strip().casefold() == "changes_requested":
        return build_review_repair_prompt(project_root, task)
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
    _append_exception_patch_conflict_section(lines, project_root, task)
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
    _append_technology_constraints_section(lines, task.technology_constraints)
    if task.output_paths:
        lines.append(f"Paths: {', '.join(task.output_paths)}")
        lines.append("")
        _append_python_package_placement_rule(lines, task)
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
    _append_invalid_verified_context(lines, project_root)
    touches_dependency_manifest = any(
        path in {"backend/pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"}
        for path in task.output_paths
    )
    if task.task_kind == "repair" and task.blocked_reason:
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
    lines.append("- Use declared output paths/tests as the main contract; make necessary adjacent support edits and validate them here.")
    lines.append("- Green tests are not enough; prove the declared requirement and acceptance behavior.")
    lines.append("- Add or strengthen task-local tests when behavior is not demonstrated.")
    _append_production_fidelity_rule(lines)
    if has_frontend_paths and task.acceptance_scenarios:
        lines.append("- Frontend acceptance behavior needs browser/e2e coverage here unless a declared downstream validation task owns the exact journey.")
        if not has_browser_specs:
            lines.append("- No browser/e2e test is currently declared for this frontend acceptance slice. Add one now unless the downstream validation owner is explicit in the task graph.")
    lines.append("- Complete only the current task. Do not start the next task, pre-implement future work, or widen scope after this task's declared tests pass.")
    _append_validation_command_guidance(lines, project_root, task, has_backend_specs=has_backend_specs, has_frontend_specs=has_frontend_specs)
    if touches_dependency_manifest:
        lines.append("- This task touches dependency manifests. Consult the `Tech Design` section in AGENTS.md / CLAUDE.md plus `docs/architecture.md` before changing project-wide stack choices.")
        lines.append("- Resolve exact packages deliberately; do not dump unused stack dependencies into manifests.")
        if task.id == SHARED_FOUNDATION_TASK_ID:
            lines.append("- For shared foundation, install only dependencies used by this task; defer feature-specific packages to their owning task.")
    lines.append("- If your changes break nearby previously passing tests, apply the minimal correct fix.")
    lines.append("- When the current task is complete, blocked, or ready for review, stop and wait for the framework to route the next step.")
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt, prompt_kind="task")
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
        if _is_production_gate_task(task):
            lines.append(f"Requirement IDs in gate scope: {_format_id_summary(task.requirements)}")
            lines.append("- Requirement details are intentionally not expanded for production gates; use `docs/requirements-source.md` and gate reports for precise source context.")
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
        if _is_production_gate_task(task):
            lines.append(f"Acceptance scenario IDs in gate scope: {_format_id_summary(task.acceptance_scenarios)}")
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
    _append_technology_constraints_section(lines, task.technology_constraints)
    if task.output_paths:
        lines.append(f"Paths: {', '.join(task.output_paths)}")
        lines.append("")
        _append_python_package_placement_rule(lines, task)
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
    _append_invalid_verified_context(lines, project_root)
    if str(task.review_status or "").strip().casefold() == "changes_requested":
        lines.append("Review repair context:")
        if task.blocked_reason:
            lines.append(f"- Review summary: {task.blocked_reason}")
        if task.review_artifact:
            lines.append(f"- Review artifact: {task.review_artifact}")
            lines.append("- Read it first, then repair and validate.")
        else:
            lines.append("- Repair cited review issues, then validate.")
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
    if _is_production_gate_task(task):
        _append_production_gate_source_guidance(lines, task)
        _append_production_gate_specific_guidance(lines, task)
    if _is_real_backend_validation_gate(task):
        _append_real_backend_gate_guidance(lines)
    lines.append("Execution guidance:")
    lines.append("- Use the declared Paths and Tests as the main scope and delivery contract for this task.")
    lines.append("- Treat passing tests as necessary but not sufficient. Do not stop if the declared acceptance scenarios are still not directly represented by executable validation.")
    lines.append("- If existing tests are weak, incomplete, missing fixtures, or not executable, fix them in this task.")
    _append_production_fidelity_rule(lines)
    lines.append("- If a scenario remains unprovable without broad new product work, stop and surface the blocker instead of faking coverage.")
    if task.acceptance_scenarios:
        lines.append("- Each declared acceptance scenario should map to at least one concrete validation path, assertion set, or explicitly documented coverage route in this task.")
    if has_frontend_paths or any(str(value).strip().startswith("frontend/") for value in task.output_tests):
        lines.append("- This task owns frontend-facing validation coverage. Keep browser/e2e checks explicit for user-visible flows rather than relying only on unit tests.")
        if not has_browser_specs:
            lines.append("- No browser/e2e test is currently declared for the frontend-facing scenarios in this validation task. Add one unless the task contract is explicitly wrong and must be corrected upstream.")
    lines.append("- Complete only the current task. Do not start the next task, pre-implement future work, or widen scope after this task's declared tests pass.")
    _append_validation_command_guidance(lines, project_root, task, has_backend_specs=has_backend_specs, has_frontend_specs=has_frontend_specs)
    lines.append("- When the current task is complete, blocked, or ready for review, stop and wait for the framework to route the next step.")
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt, prompt_kind="validation")
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
        _append_python_package_placement_rule(lines, task)
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
        ]
    )
    if _is_real_backend_validation_gate(task):
        _append_real_backend_gate_guidance(lines)
    _append_production_fidelity_rule(lines)
    _append_validation_command_guidance(
        lines,
        project_root,
        task,
        has_backend_specs=any(spec.startswith("backend/") for spec in task.output_tests),
        has_frontend_specs=any(spec.startswith("frontend/") for spec in task.output_tests),
    )
    lines.extend(
        [
            "- If the failure is in browser/e2e coverage, inspect the project-local route/auth wiring, Playwright config, and backend startup assumptions before searching generic framework files.",
            "- Repair the implementation or the tests if they are brittle, then stop as soon as the current task is complete, blocked, or ready for review.",
        ]
    )
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt, prompt_kind="test-fix")
    return prompt


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
        lines.append(f"- Attention signal: {str(runtime_attention.get('kind') or 'stalled').strip()} — {normalize_runtime_attention_message(runtime_attention.get('message'))}")
    lines.append("")
    if failed_summary:
        lines.append("Current failed-test context:")
        lines.extend(failed_summary)
        lines.append("")
    _append_python_package_placement_rule(lines, task)
    if _has_python_package_output_dir(task):
        lines.append("")
    if _is_real_backend_validation_gate(task):
        _append_real_backend_gate_guidance(lines)
    lines.extend(
        [
            "Recovery instructions:",
            "- Continue from the existing implementation already on disk. Inspect current task-scoped changes before creating new files or restarting broad exploration.",
            "- Do not re-scan the whole repository from scratch. Start from the declared output paths, declared output tests, and the stall evidence above.",
            "- If the current worktree already contains task-relevant changes, reconcile those changes with the failing tests before adding more code.",
        ]
    )
    _append_validation_command_guidance(
        lines,
        project_root,
        task,
        has_backend_specs=any(spec.startswith("backend/") for spec in task.output_tests),
        has_frontend_specs=any(spec.startswith("frontend/") for spec in task.output_tests),
    )
    lines.append("- When the task is complete, blocked, or ready for review, stop immediately and let the framework route the next step.")
    prompt = "\n".join(lines)
    write_task_prompt(project_root, task, prompt, prompt_kind="stalled-recovery")
    return prompt


def build_scope_fix_prompt(task: Task, message: str) -> str:
    lines = [
        f"Task {task.id} produced changes outside its declared scope.",
        f"Task: {task.title}",
        "",
        f"Scope error:\n{message}",
        "",
    ]
    _append_python_package_placement_rule(lines, task)
    if _has_python_package_output_dir(task):
        lines.append("")
    lines.append("Move the implementation back into the declared output paths and declared test paths, or reduce unintended file edits, then stop.")
    return "\n".join(lines)