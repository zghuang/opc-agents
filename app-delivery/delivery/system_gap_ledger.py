from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SYSTEM_GAP_LEDGER_PATH = "docs/reviews/system-gap-fix.json"
SYSTEM_GAP_LEDGER_SCHEMA_VERSION = "1"

AUDIT_READY_FOR_FINAL = "ready_for_final"
AUDIT_REPAIR_REQUIRED = "repair_required"
AUDIT_BLOCKED = "blocked"
ALLOWED_AUDIT_STATUSES = {
    AUDIT_READY_FOR_FINAL,
    AUDIT_REPAIR_REQUIRED,
    AUDIT_BLOCKED,
}

GAP_FIXED = "fixed"
GAP_UNRESOLVED = "unresolved"
GAP_NEEDS_CLARIFICATION = "needs_clarification"
GAP_EXTERNAL_BLOCKER = "external_blocker"
ALLOWED_GAP_STATUSES = {
    GAP_FIXED,
    GAP_UNRESOLVED,
    GAP_NEEDS_CLARIFICATION,
    GAP_EXTERNAL_BLOCKER,
}

REPAIRABILITY_AUTOMATIC = "automatic"
REPAIRABILITY_NEEDS_CLARIFICATION = "needs_clarification"
REPAIRABILITY_EXTERNAL = "external"
ALLOWED_REPAIRABILITY = {
    REPAIRABILITY_AUTOMATIC,
    REPAIRABILITY_NEEDS_CLARIFICATION,
    REPAIRABILITY_EXTERNAL,
}

ALLOWED_SEVERITIES = {"blocking", "non_blocking"}
GAP_ID_RE = re.compile(r"^GAP-[A-Za-z0-9][A-Za-z0-9_.-]*$")


def system_gap_ledger_path(project_root: Path | str) -> Path:
    return Path(project_root).expanduser().resolve() / SYSTEM_GAP_LEDGER_PATH


def load_system_gap_ledger(project_root: Path | str) -> dict[str, Any]:
    path = system_gap_ledger_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _string_list(value: Any, field_name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{field_name} must be an array")
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalized_gap(row: Any, index: int, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        errors.append(f"gaps[{index}] must be an object")
        return None
    gap_id = str(row.get("id") or "").strip()
    severity = str(row.get("severity") or "").strip().casefold()
    status = str(row.get("status") or "").strip().casefold()
    repairability = str(row.get("repairability") or REPAIRABILITY_AUTOMATIC).strip().casefold()
    kind = str(row.get("kind") or "").strip()
    summary = str(row.get("summary") or "").strip()
    if not GAP_ID_RE.match(gap_id):
        errors.append(f"gaps[{index}].id must match {GAP_ID_RE.pattern}")
    if severity not in ALLOWED_SEVERITIES:
        errors.append(f"gaps[{index}].severity must be one of: {', '.join(sorted(ALLOWED_SEVERITIES))}")
    if status not in ALLOWED_GAP_STATUSES:
        errors.append(f"gaps[{index}].status must be one of: {', '.join(sorted(ALLOWED_GAP_STATUSES))}")
    if repairability not in ALLOWED_REPAIRABILITY:
        errors.append(f"gaps[{index}].repairability must be one of: {', '.join(sorted(ALLOWED_REPAIRABILITY))}")
    if not kind:
        errors.append(f"gaps[{index}].kind must be non-empty")
    if not summary:
        errors.append(f"gaps[{index}].summary must be non-empty")
    evidence = _string_list(row.get("evidence"), f"gaps[{index}].evidence", errors)
    validation = _string_list(row.get("validation"), f"gaps[{index}].validation", errors)
    if status == GAP_FIXED and not evidence:
        errors.append(f"gaps[{index}].evidence must be non-empty when status=fixed")
    if status == GAP_FIXED and not validation:
        errors.append(f"gaps[{index}].validation must be non-empty when status=fixed")
    if status == GAP_NEEDS_CLARIFICATION and repairability != REPAIRABILITY_NEEDS_CLARIFICATION:
        errors.append(f"gaps[{index}].repairability must be needs_clarification when status=needs_clarification")
    if status == GAP_EXTERNAL_BLOCKER and repairability != REPAIRABILITY_EXTERNAL:
        errors.append(f"gaps[{index}].repairability must be external when status=external_blocker")
    if status == GAP_UNRESOLVED and repairability != REPAIRABILITY_AUTOMATIC:
        errors.append(f"gaps[{index}].repairability must be automatic when status=unresolved")
    return {
        "id": gap_id,
        "severity": severity,
        "status": status,
        "repairability": repairability,
        "kind": kind,
        "summary": summary,
        "requirement_ids": _string_list(row.get("requirement_ids"), f"gaps[{index}].requirement_ids", errors),
        "acceptance_ids": _string_list(row.get("acceptance_ids"), f"gaps[{index}].acceptance_ids", errors),
        "source_refs": _string_list(row.get("source_refs"), f"gaps[{index}].source_refs", errors),
        "owner_task_ids": _string_list(row.get("owner_task_ids"), f"gaps[{index}].owner_task_ids", errors),
        "evidence": evidence,
        "validation": validation,
    }


def validate_system_gap_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("system gap ledger must be a JSON object")
    errors: list[str] = []
    schema_version = str(payload.get("schema_version") or "").strip()
    audit_status = str(payload.get("audit_status") or "").strip().casefold()
    if schema_version != SYSTEM_GAP_LEDGER_SCHEMA_VERSION:
        errors.append(f"schema_version must be {SYSTEM_GAP_LEDGER_SCHEMA_VERSION}")
    if audit_status not in ALLOWED_AUDIT_STATUSES:
        errors.append(f"audit_status must be one of: {', '.join(sorted(ALLOWED_AUDIT_STATUSES))}")
    raw_gaps = payload.get("gaps")
    if not isinstance(raw_gaps, list):
        errors.append("gaps must be an array")
        raw_gaps = []
    gaps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(raw_gaps):
        gap = _normalized_gap(row, index, errors)
        if gap is None:
            continue
        if gap["id"] in seen_ids:
            errors.append(f"gaps contains duplicate id: {gap['id']}")
        seen_ids.add(gap["id"])
        gaps.append(gap)

    automatic_blockers = [gap for gap in gaps if gap["severity"] == "blocking" and gap["status"] == GAP_UNRESOLVED]
    external_blockers = [
        gap
        for gap in gaps
        if gap["severity"] == "blocking" and gap["status"] in {GAP_NEEDS_CLARIFICATION, GAP_EXTERNAL_BLOCKER}
    ]
    if automatic_blockers and audit_status != AUDIT_REPAIR_REQUIRED:
        errors.append("audit_status must be repair_required while unresolved automatic blocking gaps remain")
    if external_blockers and audit_status != AUDIT_BLOCKED:
        errors.append("audit_status must be blocked while clarification or external blocking gaps remain")
    if not automatic_blockers and not external_blockers and audit_status != AUDIT_READY_FOR_FINAL:
        errors.append("audit_status must be ready_for_final when no blocking gaps remain")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "schema_version": schema_version,
        "audit_status": audit_status,
        "gaps": gaps,
    }


def system_gap_ledger_issues(project_root: Path | str) -> list[str]:
    path = system_gap_ledger_path(project_root)
    if not path.is_file():
        return [f"required system gap ledger is missing: {SYSTEM_GAP_LEDGER_PATH}"]
    payload = load_system_gap_ledger(project_root)
    if not payload:
        return [f"required system gap ledger cannot be read: {SYSTEM_GAP_LEDGER_PATH}"]
    try:
        ledger = validate_system_gap_ledger(payload)
    except ValueError as exc:
        return [f"required system gap ledger is invalid: {exc}"]
    if ledger["audit_status"] == AUDIT_REPAIR_REQUIRED:
        return ["system gap ledger has unresolved automatically repairable blocking gaps"]
    if ledger["audit_status"] == AUDIT_BLOCKED:
        return ["system gap ledger has blocking gaps that require clarification or an external dependency"]
    return []


def system_gap_ledger_status(project_root: Path | str) -> str | None:
    payload = load_system_gap_ledger(project_root)
    try:
        ledger = validate_system_gap_ledger(payload)
    except ValueError:
        return None
    return str(ledger["audit_status"])


def unresolved_system_gap_summary(project_root: Path | str) -> list[str]:
    payload = load_system_gap_ledger(project_root)
    try:
        ledger = validate_system_gap_ledger(payload)
    except ValueError:
        return []
    return [
        f"{gap['id']}: {gap['summary']}"
        for gap in ledger["gaps"]
        if gap["severity"] == "blocking" and gap["status"] != GAP_FIXED
    ]
