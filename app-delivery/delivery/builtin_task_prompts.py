from __future__ import annotations

from pathlib import Path
from typing import Any

from .builtin_tasks import FRONTEND_API_AUDIT_REPORT_PATH, PREFINAL_AUDIT_REPORT_PATH


def render_frontend_api_audit_prompt(project_root: Path | str, task: Any) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    lines = [
        f"## Task {task.id}: Pre-final frontend/API integration repair pass",
        "",
        f"Project path: {project_dir}",
        "",
        "You are running a pre-final frontend/API integration scan-and-fix pass for the current repository state.",
        "Do not treat this as a report-only audit. Find concrete release-relevant integration gaps, fix the responsibly fixable ones in this task, validate those fixes, and document both fixes and remaining blockers.",
        "This is a new runtime session: base conclusions on the files, tests, logs, and runnable behavior present in this project directory.",
        "",
        "Mission:",
        "- Determine whether the frontend user journeys use real backend APIs instead of placeholders, static fixtures, hardcoded demo data, or Playwright route mocks as the only proof, then fix actionable integration gaps directly.",
        "- Map frontend API clients, query/mutation hooks, route loaders, and user actions to backend HTTP/MCP/mock-server endpoints and their request/response contracts.",
        "- Inspect browser/e2e tests and classify each as real-backend E2E, browser-UI-with-mocked-API, smoke-only, or placeholder.",
        "- Prepare or repair realistic test data, seed/reset helpers, and environment commands needed for real backend browser E2E where the project stack supports it.",
        "- Add or strengthen at least one real-backend browser/API integration validation for critical frontend acceptance flows when this can be done without inventing unrelated product scope.",
        f"- Write a detailed audit report to `{FRONTEND_API_AUDIT_REPORT_PATH}`.",
        "",
        "Authoritative inputs to inspect:",
        "- docs/requirements-source.md",
        "- docs/requirements.json",
        "- docs/test-plan.json",
        "- docs/work-items.json",
        "- frontend source, e2e tests, API clients, route loaders, stores, and fixtures if present",
        "- backend API routes, schemas, service adapters, mock-server tools, seed data, and environment scripts if present",
        "",
        "Audit rules:",
        "- Treat `page.route(...)`, `route.fulfill(...)`, static JSON fixtures, hardcoded fake API responses, and component-only tests as useful UI evidence but not as real backend E2E evidence.",
        "- Treat generated placeholder specs, skipped placeholder suites, and tests whose only behavior is `throw new Error('placeholder test not implemented yet')` as blockers, not planned coverage.",
        "- A user-visible flow is not real E2E unless the browser drives the UI and the application reaches the project-owned backend/API layer without Playwright fulfilling the core application endpoints.",
        "- If the frontend has API clients but pages bypass them, use hardcoded data, or only render mock payloads, fix the integration or document the blocker.",
        "- If the backend cannot support real E2E because test data, seed/reset helpers, dev server commands, CORS/proxy config, or health checks are missing, add the smallest project-native support needed or document the blocker.",
        "- Do not count dependency installation, route existence, passing unit tests, or mocked browser tests as proof that the real frontend/backend flow works.",
        "- If a project genuinely has no frontend surface, write the report as not applicable and explain the evidence.",
        "",
        "Validation rules:",
        "- Run focused checks for any fixes made in this task.",
        "- Prefer a real-backend Playwright or equivalent browser test for at least one critical user journey when feasible.",
        "- Preserve mocked browser tests when they are useful for UI states, but label them clearly in the report and do not present them as full E2E coverage.",
        "- In the report, include a concise count/list of placeholder, smoke-only, mocked-browser, and real-backend browser/API specs so later audits can see coverage quality without reclassifying every file.",
        "- Record every validation command and result in the report. If a real-backend test cannot be created safely, record the exact blocker and required follow-up.",
        "",
        f"Required report: `{FRONTEND_API_AUDIT_REPORT_PATH}`",
        "The report must contain these markdown sections exactly:",
        "- # Frontend API Integration Audit",
        "- ## Audit Scope",
        "- ## API Surface Mapping",
        "- ## Mocked Browser Test Assessment",
        "- ## Real Backend E2E Readiness",
        "- ## Test Data / Environment Readiness",
        "- ## Fixed Issues",
        "- ## Remaining Gaps / Blockers",
        "- ## Validation Summary",
        "- ## Final Recommendation",
        "",
        "Completion rules:",
        f"- `{FRONTEND_API_AUDIT_REPORT_PATH}` must exist and be fully populated before stopping.",
        "- If high-severity real integration gaps remain, do not present the frontend as release-complete; document them as blockers.",
        "- When this task is complete, blocked, or ready for review, stop and let the framework route the next step.",
    ]
    return "\n".join(lines)


def render_prefinal_audit_prompt(project_root: Path | str, task: Any) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    lines = [
        f"## Task {task.id}: Pre-final full-system repair pass",
        "",
        f"Project path: {project_dir}",
        "",
        "You are running a final pre-release scan-and-fix pass for the current repository state.",
        "Do not treat this as a report-only audit. Find concrete release-relevant gaps, fix the responsibly fixable ones in this task, validate those fixes, and document both fixes and remaining blockers.",
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
    return "\n".join(lines)


def render_prefinal_audit_review_request(project_root: Path | str, task: Any, *, scope_report: dict[str, Any] | None = None) -> str:
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
    _append_scope_observations(lines, scope_report, broad_scope_reason="this audit has broad project scope", reject_guidance="these edits are unrelated to release correctness")
    lines.extend(
        [
            "",
            "Inspect the current repository state and staged diff for this audit task.",
            "Reply in raw JSON with fields: status, summary, findings (array of finding objects), requirement_assessment (array), acceptance_assessment (array). Use empty arrays for findings and assessment fields if there are no rows.",
        ]
    )
    return "\n".join(lines)


def render_frontend_api_audit_review_request(project_root: Path | str, task: Any, *, scope_report: dict[str, Any] | None = None) -> str:
    lines = [
        f"Perform an independent review for task {task.id}.",
        f"Project path: {Path(project_root).expanduser().resolve()}",
        f"Task title: {task.title}",
        "",
        "This task is a pre-final frontend/API integration audit. Review whether frontend user journeys are backed by real project-owned APIs, not only mocks or static fixtures.",
        "",
        "Required checks:",
        f"- `{FRONTEND_API_AUDIT_REPORT_PATH}` exists and contains all required sections.",
        "- The report maps frontend API clients/hooks/loaders/actions to backend routes, schemas, mock-server tools, or documented non-HTTP integrations.",
        "- The report explicitly lists placeholder specs separately and does not count them as planned, mocked, or real E2E coverage.",
        "- The report classifies browser tests that use Playwright route mocks separately from real-backend E2E tests.",
        "- The report identifies whether critical user-facing acceptance flows have at least one real-backend browser/API integration path when the stack supports it.",
        "- The report covers test data, seed/reset support, health checks, proxy/CORS/dev-server setup, and other environment requirements needed for real E2E.",
        "- Fixes made by this task are scoped to closing real integration gaps or preparing credible test data/environment support.",
        "",
        "Return status=changes_requested if placeholder specs are minimized as acceptable coverage, mocked browser tests are presented as full E2E proof, frontend pages still bypass real API clients, core backend routes are unreachable from the UI, the report omits obvious API integration gaps, or high-severity blockers are minimized as non-blocking.",
        "Return status=pass only when the audit report and any fixes are credible enough for the broader system audit to rely on.",
    ]
    _append_scope_observations(lines, scope_report, broad_scope_reason="this audit has broad frontend/backend integration scope", reject_guidance="these edits are unrelated to credible real integration evidence")
    lines.extend(
        [
            "",
            "Inspect the current repository state and staged diff for this audit task.",
            "Reply in raw JSON with fields: status, summary, findings (array of finding objects), requirement_assessment (array), acceptance_assessment (array). Use empty arrays for findings and assessment fields if there are no rows.",
        ]
    )
    return "\n".join(lines)


def _append_scope_observations(lines: list[str], scope_report: dict[str, Any] | None, *, broad_scope_reason: str, reject_guidance: str) -> None:
    if not isinstance(scope_report, dict):
        return
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
        lines.append(f"- Out-of-scope paths were detected. Because {broad_scope_reason}, reject only if {reject_guidance}:")
        for path in out_of_scope:
            lines.append(f"  - {path}")