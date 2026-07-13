from __future__ import annotations

import json
from pathlib import Path

import pytest

from delivery.system_gap_ledger import (
    AUDIT_BLOCKED,
    AUDIT_READY_FOR_FINAL,
    AUDIT_REPAIR_REQUIRED,
    SYSTEM_GAP_LEDGER_PATH,
    system_gap_ledger_issues,
    system_gap_ledger_status,
    validate_system_gap_ledger,
)


def _gap(*, status: str = "fixed", repairability: str = "automatic") -> dict[str, object]:
    return {
        "id": "GAP-001",
        "severity": "blocking",
        "status": status,
        "repairability": repairability,
        "kind": "missing_behavior",
        "summary": "The declared behavior is missing.",
        "requirement_ids": ["REQ-001"],
        "acceptance_ids": ["AS-001"],
        "source_refs": ["section 1"],
        "owner_task_ids": ["T002"],
        "evidence": ["backend/service.py"],
        "validation": ["backend/tests/test_service.py"],
    }


def test_validates_ready_ledger_with_fixed_gap() -> None:
    ledger = validate_system_gap_ledger(
        {
            "schema_version": "1",
            "audit_status": AUDIT_READY_FOR_FINAL,
            "gaps": [_gap()],
        }
    )

    assert ledger["audit_status"] == AUDIT_READY_FOR_FINAL


def test_requires_repair_status_for_automatic_blocking_gap() -> None:
    gap = _gap(status="unresolved")
    gap["evidence"] = []
    gap["validation"] = []

    ledger = validate_system_gap_ledger(
        {
            "schema_version": "1",
            "audit_status": AUDIT_REPAIR_REQUIRED,
            "gaps": [gap],
        }
    )

    assert ledger["audit_status"] == AUDIT_REPAIR_REQUIRED


def test_rejects_inconsistent_blocked_gap_status() -> None:
    gap = _gap(status="needs_clarification", repairability="needs_clarification")

    with pytest.raises(ValueError, match="audit_status must be blocked"):
        validate_system_gap_ledger(
            {
                "schema_version": "1",
                "audit_status": AUDIT_READY_FOR_FINAL,
                "gaps": [gap],
            }
        )


def test_reports_blocking_ledger_to_framework(tmp_path: Path) -> None:
    ledger_path = tmp_path / SYSTEM_GAP_LEDGER_PATH
    ledger_path.parent.mkdir(parents=True)
    gap = _gap(status="external_blocker", repairability="external")
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "audit_status": AUDIT_BLOCKED,
                "gaps": [gap],
            }
        ),
        encoding="utf-8",
    )

    issues = system_gap_ledger_issues(tmp_path)

    assert issues == ["system gap ledger has blocking gaps that require clarification or an external dependency"]
    assert system_gap_ledger_status(tmp_path) == AUDIT_BLOCKED
