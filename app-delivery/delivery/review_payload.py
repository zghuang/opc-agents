from __future__ import annotations

import json
import re
from typing import Any

from .task import Task


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object"}},
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
        "intent_assessment": {"type": "object"},
        "task_contract_assessment": {"type": "object"},
        "technology_assessment": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["status", "summary", "findings"],
}

ALLOWED_REVIEW_STATUSES = {"pass", "changes_requested"}
ALLOWED_ASSESSMENT_STATUSES = {"pass", "changes_requested"}
ALLOWED_FINDING_SEVERITIES = {"blocking", "non_blocking"}
FINDING_SEVERITY_ALIASES = {
    "advisory": "non_blocking",
    "info": "non_blocking",
    "informational": "non_blocking",
    "minor": "non_blocking",
    "note": "non_blocking",
    "non-blocking": "non_blocking",
    "nonblocking": "non_blocking",
    "suggestion": "non_blocking",
    "warning": "non_blocking",
}
TASK_CONTRACT_REPAIR_ACTION = "task_contract_repair"
TASK_CONTRACT_DECOMPOSE_FALLBACK_ACTION = "repair_task_decompose"
ALLOWED_TASK_CONTRACT_ACTIONS = {
    "implementation_repair",
    TASK_CONTRACT_REPAIR_ACTION,
    TASK_CONTRACT_DECOMPOSE_FALLBACK_ACTION,
}
ALLOWED_TASK_CONTRACT_ISSUE_TYPES = {"implementation", "task_contract"}


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
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{field_name}[{index}] must be an object")
            continue
        row_errors: list[str] = []
        item_id = str(row.get("id") or "").strip()
        status = str(row.get("status") or "").strip().casefold()
        notes = str(row.get("notes") or "").strip()
        if not item_id or item_id not in allowed:
            errors.append(f"{field_name} contains unknown id: {item_id or '<empty>'}")
            continue
        if item_id in seen:
            errors.append(f"{field_name} contains duplicate id: {item_id}")
            continue
        if status not in ALLOWED_ASSESSMENT_STATUSES:
            row_errors.append(f"{field_name} status for {item_id} must be one of: {', '.join(sorted(ALLOWED_ASSESSMENT_STATUSES))}")
        if not notes:
            row_errors.append(f"{field_name} notes must be non-empty for {item_id}")
        if row_errors:
            errors.extend(row_errors)
            seen.add(item_id)
            continue
        normalized.append({"id": item_id, "status": status, "notes": notes})
        seen.add(item_id)
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def _expand_pass_all_except_matrix(
    parsed: dict[str, Any],
    *,
    field_name: str,
    allowed_ids: list[str],
) -> Any:
    mode = str(parsed.get(f"{field_name}_mode") or "").strip().casefold()
    if mode != "pass_all_except":
        return parsed.get(field_name)
    defaults = parsed.get(f"{field_name}_defaults") if isinstance(parsed.get(f"{field_name}_defaults"), dict) else {}
    default_notes = str(defaults.get("notes") or "Passed by reviewer default under pass_all_except mode.").strip()
    exception_rows = _normalize_review_matrix(parsed.get(field_name, []), field_name=field_name, allowed_ids=allowed_ids)
    by_id = {row["id"]: row for row in exception_rows}
    return [by_id[item_id] if item_id in by_id else {"id": item_id, "status": "pass", "notes": default_notes} for item_id in allowed_ids]


def _normalize_findings(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("findings must be an array")
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            errors.append(f"findings[{index}] must be an object with fields severity, message, requirement_ids, and acceptance_ids")
            continue
        raw_severity = str(row.get("severity") or "").strip().casefold()
        severity = FINDING_SEVERITY_ALIASES.get(raw_severity, raw_severity)
        message = str(row.get("message") or row.get("description") or row.get("recommendation") or "").strip()
        if severity not in ALLOWED_FINDING_SEVERITIES:
            errors.append(f"findings[{index}].severity must be one of: {', '.join(sorted(ALLOWED_FINDING_SEVERITIES))}")
        if not message:
            errors.append(f"findings[{index}].message must be non-empty")
        if severity not in ALLOWED_FINDING_SEVERITIES or not message:
            continue
        normalized.append(
            {
                "severity": severity,
                "requirement_ids": [str(item).strip() for item in row.get("requirement_ids", []) if str(item).strip()],
                "acceptance_ids": [str(item).strip() for item in row.get("acceptance_ids", []) if str(item).strip()],
                "message": message,
            }
        )
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def _normalize_intent_assessment(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("intent_assessment must be an object when provided")
    status = str(value.get("status") or "").strip().casefold()
    if status not in ALLOWED_ASSESSMENT_STATUSES:
        raise ValueError("intent_assessment.status must be pass or changes_requested")
    missing_done_when = [str(item).strip() for item in value.get("missing_done_when", []) if str(item).strip()]
    violated_non_goals = [str(item).strip() for item in value.get("violated_non_goals", []) if str(item).strip()]
    if status == "pass" and (missing_done_when or violated_non_goals):
        raise ValueError("intent_assessment.status=pass cannot include missing_done_when or violated_non_goals")
    return {
        "status": status,
        "missing_done_when": missing_done_when,
        "violated_non_goals": violated_non_goals,
        "notes": str(value.get("notes") or "").strip(),
    }


def _normalize_task_contract_assessment(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("task_contract_assessment must be an object when provided")
    status = str(value.get("status") or "pass").strip().casefold()
    issue_type = str(value.get("issue_type") or "implementation").strip().casefold()
    recommended_action = str(value.get("recommended_action") or "implementation_repair").strip().casefold()
    notes = str(value.get("notes") or "").strip()
    if status not in ALLOWED_ASSESSMENT_STATUSES:
        raise ValueError("task_contract_assessment.status must be pass or changes_requested")
    if issue_type not in ALLOWED_TASK_CONTRACT_ISSUE_TYPES:
        raise ValueError("task_contract_assessment.issue_type must be implementation or task_contract")
    if recommended_action not in ALLOWED_TASK_CONTRACT_ACTIONS:
        raise ValueError("task_contract_assessment.recommended_action must be implementation_repair, task_contract_repair, or repair_task_decompose")
    if recommended_action in {TASK_CONTRACT_REPAIR_ACTION, TASK_CONTRACT_DECOMPOSE_FALLBACK_ACTION} and (status != "changes_requested" or issue_type != "task_contract"):
        raise ValueError("task_contract_assessment contract-repair actions require status=changes_requested and issue_type=task_contract")
    if recommended_action in {TASK_CONTRACT_REPAIR_ACTION, TASK_CONTRACT_DECOMPOSE_FALLBACK_ACTION} and not notes:
        raise ValueError("task_contract_assessment.notes must explain why task contract repair is required")
    operations = value.get("operations") if isinstance(value.get("operations"), list) else []
    normalized_operations = [dict(item) for item in operations if isinstance(item, dict)]
    if recommended_action == TASK_CONTRACT_REPAIR_ACTION and not normalized_operations:
        raise ValueError("task_contract_assessment.recommended_action=task_contract_repair requires operations[]")
    return {
        "status": status,
        "issue_type": issue_type,
        "recommended_action": recommended_action,
        "operations": normalized_operations,
        "affected_requirement_ids": [str(item).strip() for item in value.get("affected_requirement_ids", []) if str(item).strip()],
        "affected_acceptance_ids": [str(item).strip() for item in value.get("affected_acceptance_ids", []) if str(item).strip()],
        "notes": notes,
    }


def _normalize_technology_assessment(value: Any, task: Task) -> list[dict[str, Any]]:
    constraints = task.technology_constraints or []
    if not constraints:
        return []
    if not isinstance(value, list):
        raise ValueError("technology_assessment must be provided as an array when task technology_constraints are present")
    allowed_names = {str(row.get("name") or "").strip() for row in constraints if str(row.get("name") or "").strip()}
    required_names = {str(row.get("name") or "").strip() for row in constraints if str(row.get("name") or "").strip() and str(row.get("requirement") or "must_use").strip().lower() == "must_use"}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            errors.append(f"technology_assessment[{index}] must be an object")
            continue
        name = str(row.get("name") or "").strip()
        status = str(row.get("status") or "").strip().casefold()
        raw_evidence = row.get("evidence", [])
        if isinstance(raw_evidence, list):
            evidence = [str(item).strip() for item in raw_evidence if str(item).strip()]
        elif str(raw_evidence or "").strip():
            evidence = [str(raw_evidence).strip()]
        else:
            evidence = []
        notes = str(row.get("notes") or "").strip()
        if name not in allowed_names:
            errors.append(f"technology_assessment contains unknown technology constraint: {name or '<empty>'}")
            continue
        if name in seen:
            errors.append(f"technology_assessment contains duplicate technology constraint: {name}")
            continue
        if status not in ALLOWED_ASSESSMENT_STATUSES:
            errors.append(f"technology_assessment status for {name} must be pass or changes_requested")
            continue
        if not notes:
            errors.append(f"technology_assessment notes must be non-empty for {name}")
            continue
        normalized.append({"name": name, "status": status, "evidence": evidence, "notes": notes})
        seen.add(name)
    missing = sorted(name for name in allowed_names if name not in seen)
    if missing:
        errors.append(f"missing technology assessments: {', '.join(missing)}")
    passing_without_evidence = sorted(row["name"] for row in normalized if row["name"] in required_names and row["status"] == "pass" and not row["evidence"])
    if passing_without_evidence:
        errors.append(f"must_use technology assessments require evidence: {', '.join(passing_without_evidence)}")
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def review_requests_task_contract_repair(review_payload: dict[str, Any]) -> bool:
    assessment = review_payload.get("task_contract_assessment") if isinstance(review_payload.get("task_contract_assessment"), dict) else None
    if not assessment:
        return False
    return str(assessment.get("recommended_action") or "").strip().casefold() == TASK_CONTRACT_REPAIR_ACTION


def review_requests_task_decompose_repair(review_payload: dict[str, Any]) -> bool:
    assessment = review_payload.get("task_contract_assessment") if isinstance(review_payload.get("task_contract_assessment"), dict) else None
    if not assessment:
        return False
    return str(assessment.get("recommended_action") or "").strip().casefold() == TASK_CONTRACT_DECOMPOSE_FALLBACK_ACTION


def validate_pass_review_matrix(task: Task, parsed: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    try:
        findings = _normalize_findings(parsed.get("findings"))
    except ValueError as exc:
        findings = []
        errors.append(str(exc))
    requirement_assessment_source = _expand_pass_all_except_matrix(parsed, field_name="requirement_assessment", allowed_ids=task.requirements)
    acceptance_assessment_source = _expand_pass_all_except_matrix(parsed, field_name="acceptance_assessment", allowed_ids=task.acceptance_scenarios)
    if not isinstance(requirement_assessment_source, list):
        errors.append("requirement_assessment must be provided as an array")
    if not isinstance(acceptance_assessment_source, list):
        errors.append("acceptance_assessment must be provided as an array")
    if task.intent and not isinstance(parsed.get("intent_assessment"), dict):
        errors.append("intent_assessment must be provided when task intent is present")
    try:
        requirement_assessment = _normalize_review_matrix(
            requirement_assessment_source,
            field_name="requirement_assessment",
            allowed_ids=task.requirements,
        )
    except ValueError as exc:
        requirement_assessment = []
        errors.append(str(exc))
    try:
        acceptance_assessment = _normalize_review_matrix(
            acceptance_assessment_source,
            field_name="acceptance_assessment",
            allowed_ids=task.acceptance_scenarios,
        )
    except ValueError as exc:
        acceptance_assessment = []
        errors.append(str(exc))
    try:
        intent_assessment = _normalize_intent_assessment(parsed.get("intent_assessment"))
    except ValueError as exc:
        intent_assessment = None
        errors.append(str(exc))
    try:
        task_contract_assessment = _normalize_task_contract_assessment(parsed.get("task_contract_assessment"))
    except ValueError as exc:
        task_contract_assessment = None
        errors.append(str(exc))
    try:
        technology_assessment = _normalize_technology_assessment(parsed.get("technology_assessment"), task)
    except ValueError as exc:
        technology_assessment = []
        errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(error for error in errors if error))
    parsed["findings"] = findings
    parsed["requirement_assessment"] = requirement_assessment
    parsed["acceptance_assessment"] = acceptance_assessment
    if intent_assessment is not None:
        parsed["intent_assessment"] = intent_assessment
    if task_contract_assessment is not None:
        parsed["task_contract_assessment"] = task_contract_assessment
    if task.technology_constraints:
        parsed["technology_assessment"] = technology_assessment

    if str(parsed.get("status") or "").strip().casefold() != "pass":
        return parsed

    blocking_findings = [
        str(row.get("message") or "").strip()
        for row in findings
        if isinstance(row, dict) and str(row.get("severity") or "").strip().casefold() == "blocking"
    ]
    if intent_assessment is not None and str(intent_assessment.get("status") or "").strip().casefold() != "pass":
        blocking_findings.append("intent_assessment is changes_requested")
    if task_contract_assessment is not None and str(task_contract_assessment.get("status") or "").strip().casefold() != "pass":
        blocking_findings.append("task_contract_assessment is changes_requested")
    non_passing_technology = [row["name"] for row in technology_assessment if row["status"] != "pass"]
    if non_passing_technology:
        blocking_findings.append("technology_assessment is changes_requested: " + ", ".join(non_passing_technology))

    requirement_ids = {row["id"] for row in requirement_assessment}
    acceptance_ids = {row["id"] for row in acceptance_assessment}
    missing_requirements = [requirement_id for requirement_id in task.requirements if requirement_id not in requirement_ids]
    missing_acceptance = [scenario_id for scenario_id in task.acceptance_scenarios if scenario_id not in acceptance_ids]
    non_passing_requirements = [row["id"] for row in requirement_assessment if row["status"] != "pass"]
    non_passing_acceptance = [row["id"] for row in acceptance_assessment if row["status"] != "pass"]

    problems: list[str] = []
    if blocking_findings:
        problems.append("blocking findings present: " + "; ".join(blocking_findings[:5]))
    if _requires_review_matrix(task):
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
            "status=pass requires no blocking findings, passing intent assessment when provided, and explicit passing review assessments for every declared requirement and acceptance scenario; "
            + "; ".join(problems)
        )
    return parsed


def parse_review_payload(text: str) -> dict[str, Any]:
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
        if not status or not summary:
            continue
        normalized_status = status.casefold()
        if normalized_status not in ALLOWED_REVIEW_STATUSES:
            saw_invalid_status = True
            continue
        payload["status"] = status
        payload["summary"] = summary
        return payload
    if saw_invalid_status:
        raise ValueError("review status must be one of: pass, changes_requested")
    raise ValueError(
        "reviewer did not return parseable JSON object with required fields: "
        "status (pass|changes_requested), summary (string), findings (array of objects with severity=blocking|non_blocking and message), "
        "requirement_assessment (array), acceptance_assessment (array)"
    )


# Backward-compatible aliases for older imports and tests.
_review_requests_task_contract_repair = review_requests_task_contract_repair
_review_requests_task_decompose_repair = review_requests_task_decompose_repair
_validate_pass_review_matrix = validate_pass_review_matrix
_parse_review_payload = parse_review_payload


__all__ = [
    "REVIEW_SCHEMA",
    "TASK_CONTRACT_REPAIR_ACTION",
    "TASK_CONTRACT_DECOMPOSE_FALLBACK_ACTION",
    "parse_review_payload",
    "review_requests_task_contract_repair",
    "review_requests_task_decompose_repair",
    "validate_pass_review_matrix",
    "_parse_review_payload",
    "_review_requests_task_contract_repair",
    "_review_requests_task_decompose_repair",
    "_validate_pass_review_matrix",
]
