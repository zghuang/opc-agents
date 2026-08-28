from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .builtin_tasks import (
    FINAL_VERIFY_TASK_ID,
    FRONTEND_API_AUDIT_REPORT_PATH,
    FRONTEND_API_AUDIT_REQUIRED_SECTIONS,
    FRONTEND_API_AUDIT_TASK_ID,
    PREFINAL_AUDIT_REPORT_PATH,
    PREFINAL_AUDIT_REQUIRED_SECTIONS,
    PREFINAL_AUDIT_TASK_ID,
)
from .errors import DeliveryError
from .gates import refresh_gates
from .loop_gitops import blocking_verified_task_issues, git_commit_explicit_paths, git_commit_task
from .production_semantics import mock_only_browser_e2e_issues, scan_production_semantics
from .release_assessment import (
    RELEASE_SCORE_THRESHOLD,
    release_assessment_path,
    validate_release_assessment,
)
from .review_artifacts import (
    _archive_review_round,
    _deferred_review_payload,
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
from .review_prompts import (
    build_code_review_request,
    build_final_review_request,
)
from .runtime_config import resolve_project_root
from .session import current_session, retire_session, save_current_session
from .state import (
    load_task_runtime_state,
    load_test_results,
    normalize_task_runtime_state,
    project_paths,
    save_task_runtime_state,
    utc_now_iso,
)
from .system_gap_ledger import system_gap_ledger_issues, system_gap_ledger_status, unresolved_system_gap_summary
from .task import Task, all_tasks, mark_task, next_generated_task_id, save_tasks
from .task_contract_repair import (
    TaskContractRepairError,
    apply_task_contract_repair,
    defer_acceptance_scenarios,
)

MAX_CODE_REVIEW_REPAIR_ATTEMPTS = 4
MAX_FINAL_REPAIR_ITERATIONS = 3
MACHINE_PRECONDITION_PROGRESS_REPEAT_THRESHOLD = 2
SEMANTIC_RISK_REGISTER_PATH = "docs/reviews/semantic-risk-register.json"
SECURITY_RISK_REGISTER_PATH = SEMANTIC_RISK_REGISTER_PATH
DEFERABLE_MACHINE_PRECONDITION_CATEGORIES = {"real-backend-e2e", "production-stub", "production-fake-data"}
FINAL_VERIFICATION_REPAIR_TASK_PREFIX = "Final Verification Repair Bundle"
FINAL_REVIEW_REPAIR_TASK_PREFIX = "Final Review Repair Bundle"
ACCESS_CONTROL_SURFACE_MARKERS = {
    "2fa",
    "abac",
    "accesscontrol",
    "acl",
    "attributebasedaccesscontrol",
    "auth",
    "authn",
    "authz",
    "authenticate",
    "authenticated",
    "authentication",
    "authorize",
    "authorized",
    "authorization",
    "bearer",
    "casbin",
    "cedar",
    "claim",
    "claimbasedaccesscontrol",
    "cognito",
    "credential",
    "entitlement",
    "guard",
    "iam",
    "identity",
    "idp",
    "jwt",
    "jwks",
    "keycloak",
    "ldap",
    "login",
    "mfa",
    "middleware",
    "oauth",
    "oauth2",
    "oidc",
    "openid",
    "opa",
    "permission",
    "policy",
    "policybasedaccesscontrol",
    "principal",
    "privilege",
    "privileged",
    "rbac",
    "rls",
    "role",
    "rolebasedaccesscontrol",
    "rowlevelsecurity",
    "saml",
    "scim",
    "scope",
    "security",
    "session",
    "sso",
    "tenant",
    "tenantisolation",
    "tenancy",
    "token",
}


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
    lowered = report_text.casefold()
    release_blocking_markers = (
        "overall: defer",
        "t-final should not proceed",
        "fails pre-final validation",
        "release-blocking",
    )
    if any(marker in lowered for marker in release_blocking_markers):
        return [
            "system audit report declares release-blocking gaps or says T-FINAL should not proceed; review pass is not allowed until blockers are fixed or the report is updated with source-backed evidence"
        ]
    return system_gap_ledger_issues(project_root)


def _final_release_assessment_issues(project_root: Path | str) -> list[str]:
    system_gap_issues = system_gap_ledger_issues(project_root)
    if system_gap_issues:
        return system_gap_issues
    path = release_assessment_path(project_root)
    if not path.is_file():
        return [f"required release assessment is missing: {path.relative_to(resolve_project_root(project_root))}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["required release assessment cannot be read"]
    try:
        assessment = validate_release_assessment(payload)
    except ValueError as exc:
        return [f"required release assessment is invalid: {exc}"]
    if assessment["score"] < RELEASE_SCORE_THRESHOLD:
        return [f"release assessment score {assessment['score']} is below the required threshold {RELEASE_SCORE_THRESHOLD}"]
    failed_gates = [gate["id"] for gate in assessment["hard_gates"] if gate["status"] != "pass"]
    if failed_gates:
        return ["release assessment has failed hard gates: " + ", ".join(failed_gates)]
    if not assessment["release_eligible"]:
        return ["release assessment is not eligible for release"]
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, FINAL_VERIFY_TASK_ID))
    framework_gates = runtime_state.get("final_release_hard_gates")
    if not isinstance(framework_gates, list):
        return ["framework hard-gate attestation is missing; rerun final verification before importing a pass review"]
    framework_status = {
        str(row.get("id") or "").strip(): str(row.get("status") or "").strip().casefold()
        for row in framework_gates
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    assessment_status = {gate["id"]: gate["status"] for gate in assessment["hard_gates"]}
    missing_framework_gates = [gate_id for gate_id in assessment_status if gate_id not in framework_status]
    if missing_framework_gates:
        return ["framework hard-gate attestation is missing gates: " + ", ".join(missing_framework_gates)]
    failed_framework_gates = [gate_id for gate_id, status in framework_status.items() if gate_id in assessment_status and status != "pass"]
    if failed_framework_gates:
        return ["framework hard-gate attestation has failed gates: " + ", ".join(failed_framework_gates)]
    mismatched_gates = [
        gate_id
        for gate_id, status in assessment_status.items()
        if framework_status.get(gate_id) != status
    ]
    if mismatched_gates:
        return ["release assessment hard gates disagree with framework attestation: " + ", ".join(mismatched_gates)]
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
    lowered = report_text.casefold()
    mentions_production_stubs = any(
        marker in lowered
        for marker in (
            "implemented as stubs",
            "backend stub routes added",
            "stub endpoint",
            "returns stub",
            "return stub",
        )
    )
    minimizes_blockers = bool(re.search(r"###\s*critical[^\n]*\n\s*\*\*none\b", lowered)) or "critical (functional blockers)\n**none" in lowered
    if mentions_production_stubs and minimizes_blockers:
        return [
            "frontend/API audit report identifies production-path stubs but presents them as non-blocking; production stubs must be fixed, moved to explicit mock/dev fixtures, or documented as blocking gaps"
        ]
    return []


def _path_overlaps(scope: str, candidate: str) -> bool:
    normalized_scope = str(scope or "").strip().lstrip("./").rstrip("/")
    normalized_candidate = str(candidate or "").strip().lstrip("./").rstrip("/")
    if not normalized_scope or not normalized_candidate:
        return False
    return (
        normalized_candidate == normalized_scope
        or normalized_candidate.startswith(normalized_scope + "/")
        or normalized_scope.startswith(normalized_candidate + "/")
    )


def _production_semantic_precondition_errors(project_root: Path | str, task: Task) -> list[str]:
    project_dir = resolve_project_root(project_root)
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
    scope_report = runtime_state.get("pending_scope_report") if isinstance(runtime_state.get("pending_scope_report"), dict) else {}
    changed_paths = [
        str(path).strip()
        for key in ("changed_paths", "staged_paths", "out_of_scope")
        for path in scope_report.get(key, [])
        if str(path).strip() and (project_dir / str(path).strip().lstrip("./")).exists()
    ]
    if not changed_paths:
        return []
    errors: list[str] = []
    for finding in scan_production_semantics(project_root):
        if not any(_path_overlaps(path, finding.path) for path in changed_paths):
            continue
        errors.append(
            f"production semantic finding in task-changed path: {finding.category} {finding.path}: {finding.message}"
        )
        if len(errors) >= 20:
            break
    return errors


def _is_production_gate_task(task: Task) -> bool:
    return str(task.task_kind or "").strip() == "validation" and str(task.title or "").startswith("Production Gate:")


def _production_gate_precondition_errors(project_root: Path | str, task: Task) -> list[str]:
    if not _is_production_gate_task(task):
        return []
    errors: list[str] = []
    for finding in scan_production_semantics(project_root):
        errors.append(
            f"production gate semantic finding: {finding.category} {finding.path}: {finding.message}"
        )
        if len(errors) >= 10:
            break
    return errors


def _unsupported_frontend_root_precondition_errors(project_root: Path | str, task: Task) -> list[str]:
    project_dir = resolve_project_root(project_root)
    if not (project_dir / "frontend" / "package.json").exists():
        return []
    runtime_state = normalize_task_runtime_state(load_task_runtime_state(project_root, task.id))
    scope_report = runtime_state.get("pending_scope_report") if isinstance(runtime_state.get("pending_scope_report"), dict) else {}
    changed_paths = [
        str(path).strip().lstrip("./")
        for key in ("changed_paths", "staged_paths", "out_of_scope")
        for path in scope_report.get(key, [])
        if str(path).strip()
    ]
    invalid = sorted(
        {
            path
            for path in changed_paths
            if path.startswith(("src/", "e2e/")) or path in {"package.json", "vitest.config.ts", "vite.config.ts"}
        }
    )
    if not invalid:
        return []
    return [
        "task changed unsupported top-level frontend paths while this project uses `frontend/` as the frontend root: "
        + ", ".join(invalid[:8])
        + "; move fixes under `frontend/` or repair the task/test contract instead of creating root-level frontend shims"
    ]


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
    errors.extend(_production_semantic_precondition_errors(project_root, task))
    errors.extend(_production_gate_precondition_errors(project_root, task))
    errors.extend(_unsupported_frontend_root_precondition_errors(project_root, task))
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


def _precondition_failure_review_payload(task: Task, parsed: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    summary = (
        "Machine-verifiable review pass preconditions failed; route this task back to implementation repair. "
        + f"{len(errors)} blocking precondition error(s): "
        + "; ".join(errors[:3])
    )
    findings = [
        *[finding for finding in parsed.get("findings", []) if isinstance(finding, dict)],
        *[
            {
                "severity": "blocking",
                "requirement_ids": list(task.requirements),
                "acceptance_ids": list(task.acceptance_scenarios),
                "message": error,
            }
            for error in errors
        ],
    ]
    return {
        **parsed,
        "status": "changes_requested",
        "review_type": "machine-precondition",
        "external_review_status": str(parsed.get("status") or "").strip() or "unknown",
        "machine_precondition_status": "failed",
        "machine_precondition_errors": list(errors),
        "summary": summary,
        "findings": findings,
        "task_contract_assessment": {
            "status": "changes_requested",
            "issue_type": "implementation",
            "recommended_action": "implementation_repair",
            "operations": [],
            "affected_requirement_ids": list(task.requirements),
            "affected_acceptance_ids": list(task.acceptance_scenarios),
            "notes": summary,
        },
    }


def _machine_precondition_errors(review_payload: dict[str, Any]) -> list[str]:
    return [str(error).strip() for error in review_payload.get("machine_precondition_errors", []) if str(error).strip()]


def _machine_precondition_error_family(error: str) -> dict[str, str]:
    text = str(error or "").strip()
    lowered = text.casefold()
    if "security-access-control" in lowered:
        return {"category": "security-access-control", "family": "missing-visible-route-auth"}
    if "real-backend-e2e" in lowered:
        return {"category": "real-backend-e2e", "family": "mock-only-proof"}
    if "production-stub" in lowered:
        return {"category": "production-stub", "family": "static-route-response"}
    if "production-fake-data" in lowered:
        return {"category": "production-fake-data", "family": "synthetic-data"}
    if "unsupported top-level frontend paths" in lowered:
        return {"category": "unsupported-frontend-root", "family": "wrong-root"}
    return {"category": "unknown", "family": re.sub(r"\s+", " ", lowered)[:240]}


def _machine_precondition_error_families(review_payload: dict[str, Any]) -> list[dict[str, str]]:
    return sorted((_machine_precondition_error_family(error) for error in _machine_precondition_errors(review_payload)), key=lambda row: (row["category"], row["family"]))


def _machine_precondition_fingerprint(review_payload: dict[str, Any]) -> str:
    if str(review_payload.get("review_type") or "").strip() != "machine-precondition":
        return ""
    if str(review_payload.get("external_review_status") or "").strip().casefold() != "pass":
        return ""
    families = _machine_precondition_error_families(review_payload)
    if not families:
        return ""
    payload = json.dumps(families, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _machine_precondition_is_security_access_control(review_payload: dict[str, Any]) -> bool:
    families = _machine_precondition_error_families(review_payload)
    return bool(families) and all(row.get("category") == "security-access-control" for row in families)


def _machine_precondition_deferable_families(task: Task, review_payload: dict[str, Any], project_tasks: list[Task] | None = None) -> list[dict[str, str]]:
    families = _machine_precondition_error_families(review_payload)
    if not families:
        return []
    for row in families:
        category = row.get("category")
        if category == "security-access-control":
            if not (_task_owns_auth_or_rbac_surface(task) or _later_task_owns_auth_or_rbac_surface(task, project_tasks or [])):
                return []
            continue
        if category not in DEFERABLE_MACHINE_PRECONDITION_CATEGORIES:
            return []
    return families


def _normalize_auth_surface_token(token: str) -> str:
    normalized = str(token or "").strip().casefold()
    if len(normalized) > 4 and normalized.endswith("ies"):
        return normalized[:-3] + "y"
    if len(normalized) > 3 and normalized.endswith("es"):
        return normalized[:-2]
    if len(normalized) > 3 and normalized.endswith("s"):
        return normalized[:-1]
    return normalized


def _auth_surface_tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold())
    words = [_normalize_auth_surface_token(token) for token in normalized.split() if token]
    tokens = set(words)
    for ngram_size in range(2, 5):
        for index in range(0, max(len(words) - ngram_size + 1, 0)):
            tokens.add("".join(words[index : index + ngram_size]))
    compact = re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())
    if compact:
        tokens.add(compact)
    return tokens


def _task_owns_auth_or_rbac_surface(task: Task) -> bool:
    intent_text = json.dumps(task.intent or {}, ensure_ascii=False).casefold()
    technology_text = json.dumps(task.technology_constraints or [], ensure_ascii=False).casefold()
    haystack = "\n".join(
        [
            str(task.title or ""),
            intent_text,
            technology_text,
            *task.output_paths,
            *task.output_tests,
        ]
    )
    tokens = _auth_surface_tokens(haystack)
    return any(token in ACCESS_CONTROL_SURFACE_MARKERS or token.startswith("auth") for token in tokens)


def _later_task_owns_auth_or_rbac_surface(task: Task, project_tasks: list[Task]) -> bool:
    current_index = next((index for index, candidate in enumerate(project_tasks) if candidate.id == task.id), -1)
    if current_index < 0:
        return False
    terminal_statuses = {"verified", "cancelled"}
    for candidate in project_tasks[current_index + 1 :]:
        if str(candidate.task_kind or "feature").strip().casefold() != "feature":
            continue
        if str(candidate.status or "").strip().casefold() in terminal_statuses:
            continue
        if _task_owns_auth_or_rbac_surface(candidate):
            return True
    return False


def _machine_precondition_can_defer_for_progress(task: Task, review_payload: dict[str, Any], repeat_count: int, project_tasks: list[Task] | None = None) -> bool:
    return (
        repeat_count >= MACHINE_PRECONDITION_PROGRESS_REPEAT_THRESHOLD
        and bool(_machine_precondition_deferable_families(task, review_payload, project_tasks))
    )


def _machine_precondition_progress_pass_payload(task: Task, review_payload: dict[str, Any], *, fingerprint: str, repeat_count: int, project_tasks: list[Task] | None = None) -> dict[str, Any]:
    errors = _machine_precondition_errors(review_payload)
    families = _machine_precondition_deferable_families(task, review_payload, project_tasks)
    categories = sorted({row["category"] for row in families})
    risk = {
        "kind": "semantic_review_deferred_for_progress",
        "fingerprint": fingerprint,
        "repeat_count": repeat_count,
        "categories": categories,
        "families": families,
        "reason": "repeated deferable machine-precondition after external review pass; allow dependent tasks to continue and require final human release review",
        "errors": errors,
    }
    return {
        **review_payload,
        "status": "pass",
        "review_type": "machine-precondition",
        "machine_precondition_status": "deferred_for_progress",
        "summary": (
            "Repeated machine-precondition deferred for progress after focused repair attempts. "
            "Task tests and external review passed; final human release review remains required."
        ),
        "semantic_risk": risk,
        "task_contract_assessment": {
            "status": "pass",
            "issue_type": "semantic_review_deferred_for_progress",
            "recommended_action": "final_human_release_review",
            "operations": [],
            "affected_requirement_ids": list(task.requirements),
            "affected_acceptance_ids": list(task.acceptance_scenarios),
            "notes": risk["reason"],
        },
    }


def _write_semantic_risk_register(project_root: Path, *, task: Task, review_artifact: str, risk: dict[str, Any], reviewed_at: str) -> str:
    path = project_root / SEMANTIC_RISK_REGISTER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"schema_version": "1", "risks": []}
    if not isinstance(payload, dict):
        payload = {"schema_version": "1", "risks": []}
    risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
    row = {
        "task_id": task.id,
        "task_title": task.title,
        "review_artifact": review_artifact,
        "registered_at": reviewed_at,
        **risk,
    }
    risks = [existing for existing in risks if not (isinstance(existing, dict) and existing.get("task_id") == task.id and existing.get("fingerprint") == risk.get("fingerprint"))]
    risks.append(row)
    payload["schema_version"] = "1"
    payload["risks"] = risks
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return SEMANTIC_RISK_REGISTER_PATH


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
        precondition_errors = _review_pass_precondition_errors(project_dir, task)
        if precondition_errors:
            parsed = _precondition_failure_review_payload(task, parsed, precondition_errors)
            review_status = "changes_requested"
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
    machine_fingerprint = _machine_precondition_fingerprint(parsed)
    same_machine_fingerprint = bool(machine_fingerprint and str(runtime_state.get("machine_precondition_fingerprint") or "") == machine_fingerprint)
    machine_repeat_count = int(runtime_state.get("machine_precondition_repeat_count") or 0) + 1 if same_machine_fingerprint else 0
    deferred_semantic_risk_path: str | None = None
    project_tasks = all_tasks(project_dir)
    if task.id == PREFINAL_AUDIT_TASK_ID and system_gap_ledger_status(project_dir) == "blocked":
        blockers = unresolved_system_gap_summary(project_dir)
        blocked_reason = "System Gap Fix requires clarification or an external dependency"
        if blockers:
            blocked_reason += ": " + "; ".join(blockers[:3])
        tasks = mark_task(
            project_tasks,
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
                "system_gap_fix_status": "blocked",
                "system_gap_fix_blockers": blockers,
                "system_gap_fix_blocked_at": reviewed_at,
                "pending_scope_report": None,
            },
        )
        refresh_gates(project_dir)
        return 2
    if same_machine_fingerprint and _machine_precondition_can_defer_for_progress(task, parsed, machine_repeat_count, project_tasks):
        parsed = _machine_precondition_progress_pass_payload(task, parsed, fingerprint=machine_fingerprint, repeat_count=machine_repeat_count, project_tasks=project_tasks)
        review_status = "pass"
        review_artifact = _write_review_artifact(project_dir, task, parsed)
        risk = parsed.get("semantic_risk") if isinstance(parsed.get("semantic_risk"), dict) else {}
        deferred_semantic_risk_path = _write_semantic_risk_register(project_dir, task=task, review_artifact=review_artifact, risk=risk, reviewed_at=reviewed_at)
    if review_status.casefold() == "pass":
        pass_extra_paths = [*accepted_scope_paths, review_artifact]
        if deferred_semantic_risk_path:
            pass_extra_paths.append(deferred_semantic_risk_path)
        if staged_scope_paths:
            commit_sha = git_commit_explicit_paths(
                project_dir,
                [*staged_scope_paths, *pass_extra_paths],
                f"feat({task.id}): {task.title}",
            )
        else:
            commit_sha = git_commit_task(
                project_dir,
                task,
                f"feat({task.id}): {task.title}",
                extra_paths=pass_extra_paths,
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
            verification_source="code_review",
            verification_actor="code_review",
            verification_reason="independent code review passed",
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
                **(
                    {
                        "machine_precondition_fingerprint": machine_fingerprint,
                        "machine_precondition_repeat_count": machine_repeat_count,
                        "machine_precondition_errors": _machine_precondition_errors(parsed),
                        "semantic_review_deferred_for_progress": True,
                        "semantic_risk_register": deferred_semantic_risk_path,
                        "semantic_risk": parsed.get("semantic_risk"),
                    }
                    if deferred_semantic_risk_path
                    else {}
                ),
            },
        )
        active_session = current_session(project_dir)
        if active_session is not None and active_session.id == task.status_session_id:
            active_session.current_task_id = None
            active_session.title = active_session.runtime
            save_current_session(project_dir, active_session)
        refresh_gates(project_dir)
        return 0

    if same_machine_fingerprint:
        deferable_families = _machine_precondition_deferable_families(task, parsed, project_tasks)
        if deferable_families:
            review_changes_requested_count = int(runtime_state.get("review_changes_requested_count") or 0) + 1
            review_repair_state = {
                "review_changes_requested_count": review_changes_requested_count,
                "review_repair_limit": MAX_CODE_REVIEW_REPAIR_ATTEMPTS,
                "last_changes_requested_review_artifact": review_artifact,
                "last_changes_requested_reviewed_at": reviewed_at,
                "machine_precondition_fingerprint": machine_fingerprint,
                "machine_precondition_repeat_count": machine_repeat_count,
                "machine_precondition_errors": list(parsed.get("machine_precondition_errors", [])),
                "machine_precondition_families": deferable_families,
                "focused_repair": {
                    "kind": "repeated_machine_precondition_family",
                    "reason": "same deferable machine-precondition family repeated; run one focused repair before deferring to final release review",
                    "repeat_count": machine_repeat_count,
                    "next_repeat_action": "defer_for_progress" if machine_repeat_count + 1 >= MACHINE_PRECONDITION_PROGRESS_REPEAT_THRESHOLD else "focused_repair",
                },
            }
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
                blocked_reason=str(parsed.get("summary") or "repeated security-access-control machine-precondition requires focused repair"),
                attempts=max(task.attempts, 1),
            )
            save_tasks(project_dir, tasks)
            active_session = current_session(project_dir)
            if active_session is not None and active_session.id == task.status_session_id:
                retire_session(project_dir, active_session)
            save_task_runtime_state(project_dir, task_id, {**review_repair_state, "pending_scope_report": None})
            refresh_gates(project_dir)
            return 2
        blocked_reason = (
            "repeated machine-precondition finding after external review pass; "
            f"host escalation required instead of another automatic repair loop; see {review_artifact}"
        )
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
                "machine_precondition_fingerprint": machine_fingerprint,
                "machine_precondition_repeat_count": machine_repeat_count,
                "machine_precondition_errors": list(parsed.get("machine_precondition_errors", [])),
                "machine_precondition_blocked_at": reviewed_at,
                "last_changes_requested_review_artifact": review_artifact,
                "last_changes_requested_reviewed_at": reviewed_at,
                "review_requested_scope_paths": accepted_scope_paths,
                "pending_scope_report": None,
                "escalation": {
                    "status": "required",
                    "kind": "repeated_machine_precondition",
                    "reason": "same machine-precondition finding repeated after external review pass",
                    "escalated_at": reviewed_at,
                    "review_artifact": review_artifact,
                    "fingerprint": machine_fingerprint,
                },
            },
        )
        refresh_gates(project_dir)
        return 2

    review_changes_requested_count = int(runtime_state.get("review_changes_requested_count") or 0) + 1
    review_repair_state = {
        "review_changes_requested_count": review_changes_requested_count,
        "review_repair_limit": MAX_CODE_REVIEW_REPAIR_ATTEMPTS,
        "last_changes_requested_review_artifact": review_artifact,
        "last_changes_requested_reviewed_at": reviewed_at,
    }
    if machine_fingerprint:
        review_repair_state.update(
            {
                "machine_precondition_fingerprint": machine_fingerprint,
                "machine_precondition_repeat_count": machine_repeat_count,
                "machine_precondition_errors": list(parsed.get("machine_precondition_errors", [])),
            }
        )
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
                "escalation": {
                    "status": "required",
                    "kind": "review_repair_limit",
                    "reason": f"code review requested changes {review_changes_requested_count} time(s)",
                    "escalated_at": reviewed_at,
                    "review_artifact": review_artifact,
                    "exception_report": exception_report,
                },
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
                verification_source="code_review_deferred_acceptance",
                verification_actor="code_review",
                verification_reason="independent review passed with documented acceptance deferral",
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
    review_status = str(parsed.get("status") or "changes_requested").strip()
    if review_status.casefold() == "pass":
        project_tasks = all_tasks(project_dir)
        invalid_verified = blocking_verified_task_issues(project_dir, project_tasks)
        invalid_verified.pop(FINAL_VERIFY_TASK_ID, None)
        if invalid_verified:
            tasks_by_id = {candidate.id: candidate for candidate in project_tasks}
            evidence_findings = [
                {
                    "severity": "blocking",
                    "requirement_ids": list(tasks_by_id[task_id].requirements),
                    "acceptance_ids": list(tasks_by_id[task_id].acceptance_scenarios),
                    "message": f"Verified task {task_id} has invalid release evidence: {issue}",
                }
                for task_id, issue in invalid_verified.items()
                if task_id in tasks_by_id
            ]
            parsed = {
                **parsed,
                "status": "changes_requested",
                "summary": "Final review pass rejected because verified-task release evidence is invalid: "
                + "; ".join(f"{task_id}: {issue}" for task_id, issue in invalid_verified.items()),
                "findings": [
                    *[finding for finding in parsed.get("findings", []) if isinstance(finding, dict)],
                    *evidence_findings,
                ],
            }
            review_status = "changes_requested"
    # Release assessment is intentionally display-only. The framework's existing
    # verification, gate, and System Gap Fix checks remain the release controls.
    # Keep this former score-gating branch for a future policy decision rather
    # than deleting it, but do not let scorecard values start repair work now.
    # if review_status.casefold() == "pass":
    #     assessment_errors = _final_release_assessment_issues(project_dir)
    #     if assessment_errors:
    #         parsed = {
    #             **parsed,
    #             "status": "changes_requested",
    #             "summary": "Final review pass rejected because release assessment requirements are not satisfied. " + "; ".join(assessment_errors),
    #             "findings": [
    #                 *[finding for finding in parsed.get("findings", []) if isinstance(finding, dict)],
    #                 *[
    #                     {
    #                         "severity": "blocking",
    #                         "requirement_ids": [],
    #                         "acceptance_ids": [],
    #                         "message": error,
    #                     }
    #                     for error in assessment_errors
    #                 ],
    #             }
    #         }
    #         review_status = "changes_requested"
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
    if review_status.casefold() == "pass":
        tasks = mark_task(
            all_tasks(project_dir),
            FINAL_VERIFY_TASK_ID,
            "verified",
            review_status="pass",
            review_artifact=review_artifact,
            reviewed_at=reviewed_at,
            verified_at=reviewed_at,
            verification_source="final_review",
            verification_actor="final_review",
            verification_reason="independent final review passed",
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
    "_parse_review_payload",
    "_write_final_review_artifact",
    "_write_review_artifact",
    "build_code_review_request",
    "build_final_review_request",
    "code_review_request_path",
    "final_review_input_path",
    "final_review_request_path",
    "import_final_review",
    "import_task_review",
    "review_input_path",
    "write_code_review_request",
    "write_final_review_request",
]
