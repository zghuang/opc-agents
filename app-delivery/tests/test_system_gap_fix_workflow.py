from __future__ import annotations

import json
from pathlib import Path

from delivery.builtin_tasks import (
    PREFINAL_AUDIT_OUTPUT_PATHS,
    PREFINAL_AUDIT_OUTPUT_TESTS,
    PREFINAL_AUDIT_TASK_ID,
)
from delivery.loop import DeliveryLoop
from delivery.loop_review import _prefinal_audit_artifact_issues, import_task_review
from delivery.loop_task_prompt import build_fix_prompt
from delivery.requirements_context import format_requirement_context
from delivery.state import save_test_results, save_work_items
from delivery.task import Task


def _write_requirements(project_root: Path) -> None:
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-001",
                        "title": "Source-backed behavior",
                        "summary": "Implement the declared behavior.",
                        "source_requirement_ids": ["3.2 item 4", "5.1"],
                    }
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")


def _write_ready_gap_artifacts(project_root: Path) -> None:
    docs_dir = project_root / "docs" / "reviews"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "system-audit.md").write_text(
        "# System Gap Fix\n\n"
        "## Scan Scope\n\n"
        "## Gap Summary\n\n"
        "## Fixed Gaps\n\n"
        "## Remaining Gaps / Blockers\n\n"
        "## Requirement Gap Matrix\n\n"
        "## Validation Summary\n\n"
        "## Changed Files\n\n"
        "## Final Recommendation\n",
        encoding="utf-8",
    )
    (docs_dir / "system-gap-fix.json").write_text(
        json.dumps({"schema_version": "1", "audit_status": "ready_for_final", "gaps": []}),
        encoding="utf-8",
    )


def test_requirement_context_includes_precise_source_anchors(tmp_path: Path) -> None:
    _write_requirements(tmp_path)

    context = format_requirement_context(tmp_path, ["REQ-001"])

    assert context == [
        "- REQ-001: Source-backed behavior — Implement the declared behavior.\n"
        "  Source anchors: 3.2 item 4, 5.1. Read these parts of `docs/requirements-source.md` before coding."
    ]


def test_app_delivery_skill_preserves_independent_project_source_binding() -> None:
    skill_path = Path(__file__).resolve().parents[1] / "skills" / "app-delivery" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")

    assert "Target project: <resolved project path>" in text
    assert "Requirements source: <resolved file path or none>" in text
    assert 'target_project="${OPC_HOME:-$HOME/opc}/projects/$project_arg"' in text
    assert 'control --goal auto --project "$target_project" --requirements "$requirements_source"' in text


def test_system_gap_fix_retry_retains_scan_fix_rescan_mission(tmp_path: Path) -> None:
    _write_requirements(tmp_path)
    reviews_dir = tmp_path / "docs" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "system-gap-fix.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "audit_status": "repair_required",
                "gaps": [
                    {
                        "id": "GAP-001",
                        "severity": "blocking",
                        "status": "unresolved",
                        "repairability": "automatic",
                        "kind": "missing_behavior",
                        "summary": "A source-determined behavior is missing.",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": [],
                        "source_refs": ["3.2 item 4"],
                        "owner_task_ids": ["T002"],
                        "evidence": [],
                        "validation": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    task = Task(
        id=PREFINAL_AUDIT_TASK_ID,
        title="System Gap Fix",
        status="active",
        requirements=[],
        acceptance_scenarios=[],
        dependencies=[],
        output_tests=list(PREFINAL_AUDIT_OUTPUT_TESTS),
        output_paths=list(PREFINAL_AUDIT_OUTPUT_PATHS),
        task_kind="audit",
    )

    prompt = build_fix_prompt(tmp_path, task, "Gap artifact validation failed.")

    assert "scan-fix-validate-rescan loop" in prompt
    assert "Do not reduce this retry to formatting or artifact checks" in prompt
    assert "GAP-001: A source-determined behavior is missing." in prompt
    assert "The tests for task" not in prompt


def test_prefinal_audit_requires_ready_gap_ledger(tmp_path: Path) -> None:
    _write_requirements(tmp_path)
    _write_ready_gap_artifacts(tmp_path)

    assert _prefinal_audit_artifact_issues(tmp_path) == []

    (tmp_path / "docs" / "reviews" / "system-gap-fix.json").unlink()

    assert _prefinal_audit_artifact_issues(tmp_path) == [
        "required system gap ledger is missing: docs/reviews/system-gap-fix.json"
    ]


def test_ready_audit_fixture_remains_reviewable_with_ledger(tmp_path: Path) -> None:
    _write_requirements(tmp_path)
    _write_ready_gap_artifacts(tmp_path)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": PREFINAL_AUDIT_TASK_ID,
                    "title": "System Gap Fix",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS),
                }
            ],
        },
    )

    assert _prefinal_audit_artifact_issues(tmp_path) == []


def test_system_gap_fix_blocks_only_for_external_or_clarification_gap(tmp_path: Path) -> None:
    _write_requirements(tmp_path)
    _write_ready_gap_artifacts(tmp_path)
    ledger_path = tmp_path / "docs" / "reviews" / "system-gap-fix.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "audit_status": "blocked",
                "gaps": [
                    {
                        "id": "GAP-EXT-001",
                        "severity": "blocking",
                        "status": "external_blocker",
                        "repairability": "external",
                        "kind": "external_dependency",
                        "summary": "A required external service is unavailable.",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": [],
                        "source_refs": ["3.2 item 4"],
                        "owner_task_ids": [],
                        "evidence": [],
                        "validation": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": PREFINAL_AUDIT_TASK_ID,
                    "title": "System Gap Fix",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS),
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "results": [
                {
                    "task_id": PREFINAL_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:00:00Z",
                    "test_files": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "test_types": ["unit"],
                    "requirement_ids": [],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {},
        },
    )
    input_path = tmp_path / "review.json"
    payload = {"status": "changes_requested", "summary": "External dependency required.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    assert import_task_review(tmp_path, PREFINAL_AUDIT_TASK_ID, payload, input_path) == 2

    items = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"]
    assert items[0]["status"] == "blocked"
    assert "A required external service is unavailable" in items[0]["blocked_reason"]


def test_final_verify_requeues_legacy_system_gap_fix_without_ledger(tmp_path: Path, monkeypatch) -> None:
    _write_requirements(tmp_path)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T000",
                    "title": "Scaffold",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": [],
                },
                {
                    "id": PREFINAL_AUDIT_TASK_ID,
                    "title": "Legacy system audit",
                    "status": "verified",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS),
                    "git_commit": "abc123",
                    "review_status": "pass",
                },
                {
                    "id": "T-FINAL",
                    "title": "Final verification",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [PREFINAL_AUDIT_TASK_ID],
                    "output_tests": [],
                    "output_paths": ["docs/release-evidence.md"],
                },
            ],
        },
    )
    monkeypatch.setattr("delivery.loop.run_full_suite", lambda project_root, mode="all": [])
    monkeypatch.setattr("delivery.loop.refresh_gates", lambda project_root: {"gates": []})
    monkeypatch.setattr("delivery.loop.check_requirements_coverage", lambda project_root, requirements_payload: {"total": 1, "covered": 1, "uncovered": [], "all_covered": True})
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])
    monkeypatch.setattr("delivery.loop.scan_production_semantics", lambda project_root: [])

    result = DeliveryLoop(tmp_path, runtime="opencode").final_verify()

    assert result["status"] == "repair_required"
    assert result["repair_task_id"] == PREFINAL_AUDIT_TASK_ID
    items = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"]
    audit = next(item for item in items if item["id"] == PREFINAL_AUDIT_TASK_ID)
    assert audit["status"] == "pending"
    assert audit["git_commit"] is None
    assert "required system gap ledger is missing" in audit["blocked_reason"]
