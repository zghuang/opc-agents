from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from importlib import import_module
from pathlib import Path

import pytest

from delivery.builtin_tasks import FRONTEND_API_AUDIT_OUTPUT_PATHS, FRONTEND_API_AUDIT_OUTPUT_TESTS, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_OUTPUT_PATHS, PREFINAL_AUDIT_OUTPUT_TESTS, PREFINAL_AUDIT_REPORT_PATH, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID
from delivery.errors import DeliveryError
from delivery.loop_gitops import ensure_git_repo, git, git_head_sha
from delivery.loop_review import code_review_request_path
from delivery.loop import status as loop_status
from delivery.control_plane_host import build_planning_host_step
from delivery.runtime_config import resolve_project_root
from delivery.skill_prompts import render_skill_prompt
from delivery.stage_harness import stage_import_command, stage_input_path
from delivery.state import load_gates, load_session_state, load_task_runtime_state, save_architecture_meta, save_session_state, save_task_runtime_state, save_test_plan, save_test_results, save_work_items
from delivery.task import Task


cli = import_module("delivery.__main__")


def test_review_payload_supports_pass_all_except_requirement_assessment() -> None:
    from delivery.review_payload import validate_pass_review_matrix

    task = Task.from_dict(
        {
            "id": "T010",
            "title": "Large repair",
            "status": "review_pending",
            "task_kind": "repair",
            "requirements": ["REQ-001", "REQ-002", "REQ-003"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": [],
            "output_paths": [],
        }
    )

    parsed = validate_pass_review_matrix(
        task,
        {
            "status": "changes_requested",
            "summary": "REQ-002 still needs work.",
            "findings": [],
            "requirement_assessment_mode": "pass_all_except",
            "requirement_assessment_defaults": {"notes": "Covered by prior verified evidence."},
            "requirement_assessment": [{"id": "REQ-002", "status": "changes_requested", "notes": "Missing proof."}],
            "acceptance_assessment": [],
        },
    )

    by_id = {row["id"]: row for row in parsed["requirement_assessment"]}
    assert by_id["REQ-001"] == {"id": "REQ-001", "status": "pass", "notes": "Covered by prior verified evidence."}
    assert by_id["REQ-002"]["status"] == "changes_requested"
    assert by_id["REQ-003"]["status"] == "pass"


def test_review_payload_normalizes_suggestion_and_string_technology_evidence() -> None:
    from delivery.review_payload import validate_pass_review_matrix

    task = Task.from_dict(
        {
            "id": "T019",
            "title": "Validation",
            "status": "review_pending",
            "task_kind": "validation",
            "requirements": [],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": [],
            "output_paths": [],
            "technology_constraints": [
                {"name": "PostgreSQL 16 + asyncpg", "requirement": "must_use"},
            ],
        }
    )

    parsed = validate_pass_review_matrix(
        task,
        {
            "status": "pass",
            "summary": "Looks good.",
            "findings": [{"severity": "suggestion", "description": "Optional depth improvement."}],
            "requirement_assessment": [],
            "acceptance_assessment": [],
            "technology_assessment": [
                {
                    "name": "PostgreSQL 16 + asyncpg",
                    "status": "pass",
                    "evidence": "backend/app/core/database.py uses asyncpg.create_pool",
                    "notes": "Production database access is asyncpg-backed.",
                }
            ],
        },
    )

    assert parsed["findings"] == [
        {
            "severity": "non_blocking",
            "requirement_ids": [],
            "acceptance_ids": [],
            "message": "Optional depth improvement.",
        }
    ]
    assert parsed["technology_assessment"][0]["evidence"] == ["backend/app/core/database.py uses asyncpg.create_pool"]


def test_ready_review_artifact_import_failure_marks_handoff_failed(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "review_pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/feature.py"],
                }
            ],
        },
    )
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(
        json.dumps({"status": "pass", "summary": "bad", "findings": [{"severity": "weird", "description": "invalid"}], "requirement_assessment": [], "acceptance_assessment": []}),
        encoding="utf-8",
    )
    handoff_path = tmp_path / ".app-delivery-runtime" / "host-handoff.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    step = {"skill": "code-review", "task_id": "T002", "expected_input_path": str(input_path)}
    handoff_path.write_text(
        json.dumps({"schema_version": "1", "status": "review_running", "handoff_id": "h1", **step}),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as excinfo:
        cli._execute_host_control_step_if_ready(step, tmp_path)

    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert excinfo.value.code == "review_assessment_invalid"
    assert handoff["status"] == "review_runner_failed"
    assert handoff["review_runner_status"] == "failed"
    assert handoff["review_runner_reason"] == "review_import_failed:review_assessment_invalid"
    assert handoff["review_runner_input_path"] == str(input_path)
    assert "findings[0].severity" in handoff["review_runner_error_message"]


def test_cmd_code_review_import_marks_task_verified(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "review_pending",
                    "requirements": ["REQ-001", "REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "status_session_id": "ses-1",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "The declared feature behavior is complete."},
                    {"id": "REQ-002", "status": "pass", "notes": "The related requirement behavior is complete."},
                ],
                "acceptance_assessment": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")
    spawned: list[str] = []
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: True)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: spawned.append(str(project_root)) or 123)

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    assert payload["items"][0]["git_commit"] == "abc123"
    assert payload["items"][0]["review_status"] == "pass"
    assert payload["items"][0]["review_artifact"] == "docs/reviews/code-review-T002.md"
    summary = json.loads((tmp_path / "docs" / "project-summary.json").read_text(encoding="utf-8"))
    metrics = {row["task_id"]: row for row in summary["task_metrics"]}
    assert metrics["T002"]["status"] == "verified"
    assert metrics["T002"]["completed_at"] is not None
    assert spawned == [str(tmp_path)]

def test_cmd_code_review_duplicate_import_for_imported_handoff_is_idempotent(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "review_status": "pass",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Looks good.", "findings": []}), encoding="utf-8")
    handoff_path = tmp_path / ".app-delivery-runtime" / "host-handoff.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(
            {
                "status": "imported",
                "skill": "code-review",
                "task_id": "T002",
                "input_path": str(input_path),
                "import_exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "already_imported"
    assert payload["import_outcome"] == "accepted_pass"

def test_cmd_code_review_task_contract_repair_moves_acceptance_to_later_task(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": [{"id": "AS-001", "title": "Scenario", "summary": "Needs a later workflow.", "source_requirement_ids": ["REQ-001"]}]}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(tmp_path, {"schema_version": "1", "coverage": []})
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "status_session_id": "ses-1",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                },
                {"id": "T003", "title": "Later Workflow", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_workflow.py"], "output_paths": ["backend/src/workflow.py"]},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Declared acceptance scenario belongs to a later workflow task.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": [],
                        "acceptance_ids": ["AS-001"],
                        "message": "AS-001 cannot be completed inside this task contract.",
                    }
                ],
                "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "Task-local requirement behavior is complete."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "changes_requested", "notes": "Scenario requires a later workflow task."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [
                        {
                            "op": "move_acceptance_scenario",
                            "id": "AS-001",
                            "from_task": "T002",
                            "to_task_id": "T003",
                            "rationale": "AS-001 belongs to the later workflow task.",
                        }
                    ],
                    "affected_requirement_ids": [],
                    "affected_acceptance_ids": ["AS-001"],
                    "notes": "Move AS-001 to the task that owns the workflow implementation.",
                },
            }
        ),
        encoding="utf-8",
    )
    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["status"] == "pending"
    assert by_id["T002"]["review_status"] is None
    assert by_id["T002"]["acceptance_scenarios"] == []
    assert by_id["T003"]["acceptance_scenarios"] == ["AS-001"]
    assert by_id["T003"]["requirements"] == ["REQ-001"]
    assert by_id["T003"]["status"] == "pending"
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_repair"]["applied_operations"][0]["mode"] == "moved_to_existing_task"

def test_cmd_code_review_task_contract_repair_creates_followup_when_no_target_exists(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": [{"id": "AS-001", "title": "Scenario", "summary": "Needs a later workflow.", "source_requirement_ids": ["REQ-001"]}]}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "status_session_id": "ses-1", "completed_at": "2026-06-24T01:00:00Z", "attempts": 1},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Declared acceptance scenario needs its own follow-up task.",
                "findings": [{"severity": "blocking", "requirement_ids": [], "acceptance_ids": ["AS-001"], "message": "AS-001 cannot be completed inside this task contract."}],
                "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "Task-local requirement behavior is complete."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "changes_requested", "notes": "Scenario requires a separate follow-up task."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [
                        {
                            "op": "move_acceptance_scenario",
                            "id": "AS-001",
                            "from_task": "T002",
                            "to_task_hint": "workflow implementation",
                            "followup_task": {
                                "title": "Acceptance Scenario Follow-up: AS-001 workflow",
                                "requirements": ["REQ-001"],
                                "output_tests": ["backend/tests/test_as_001_workflow.py"],
                                "output_paths": ["backend/src/workflow/as_001.py"],
                            },
                        }
                    ],
                    "affected_requirement_ids": [],
                    "affected_acceptance_ids": ["AS-001"],
                    "notes": "Create a follow-up task for AS-001.",
                },
            }
        ),
        encoding="utf-8",
    )
    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    followup = next(item for item in payload["items"] if item["title"] == "Acceptance Scenario Follow-up: AS-001 workflow")
    assert by_id["T002"]["acceptance_scenarios"] == []
    assert followup["acceptance_scenarios"] == ["AS-001"]
    assert followup["dependencies"] == ["T002"]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == ["T002", followup["id"]]
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_repair"]["applied_operations"][0]["mode"] == "created_followup_task"

def test_cmd_code_review_task_contract_repair_defers_acceptance_when_local_repair_is_unsafe(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": [{"id": "AS-001", "title": "Scenario", "summary": "Needs a later workflow."}]}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(tmp_path, {"schema_version": "1", "coverage": []})
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "status_session_id": "ses-1", "completed_at": "2026-06-24T01:00:00Z", "attempts": 1},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Declared acceptance scenario belongs elsewhere but no safe target was identified.",
                "findings": [{"severity": "blocking", "requirement_ids": [], "acceptance_ids": ["AS-001"], "message": "AS-001 cannot be completed inside this task contract."}],
                "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "Task-local requirement behavior is complete."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "changes_requested", "notes": "Scenario requires a later workflow task."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [{"op": "move_acceptance_scenario", "id": "AS-001", "from_task": "T002", "to_task_hint": "unknown workflow"}],
                    "affected_requirement_ids": [],
                    "affected_acceptance_ids": ["AS-001"],
                    "notes": "Move AS-001 when a safe target is available.",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.loop_review.git_commit_explicit_paths", lambda project_root, paths, message: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["status"] == "verified"
    assert by_id["T002"]["review_status"] == "pass"
    assert by_id["T002"]["acceptance_scenarios"] == []
    assert "verified with deferred acceptance" in by_id["T002"]["blocked_reason"]
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_deferred_acceptance"]["deferred_acceptance_scenarios"][0]["id"] == "AS-001"
    assert runtime_state["task_contract_blocker"] is None
    deferrals = json.loads((tmp_path / "docs" / "reviews" / "task-contract-deferrals.json").read_text(encoding="utf-8"))
    assert deferrals["deferred_acceptance_scenarios"][0]["policy"] == "delivery_continue"
    review_md = (tmp_path / "docs" / "reviews" / "code-review-T002.md").read_text(encoding="utf-8")
    assert "Deferred acceptance scenario assignment" in review_md
    capsys.readouterr()

    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    control_result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    routed = json.loads(capsys.readouterr().out)
    assert control_result == 0
    assert routed["planning_blocker_code"] is None
    assert routed["next_step"]["action"] == "run_final_verify"

def test_cmd_code_review_invalid_task_contract_repair_falls_back_to_implementation_repair(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T002",
                    "title": "Governance",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_governance.py"],
                    "output_paths": ["backend/src/governance/"],
                    "status_session_id": "ses-1",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                    "intent": {
                        "objective": "Governance behavior",
                        "done_when": ["Gateway is wired into agents"],
                        "non_goals": ["No agent business logic"],
                    },
                },
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Gateway agent wiring conflicts with non-goals and other implementation gaps remain.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": [],
                        "message": "The review requested an invalid task-contract move for a done_when item.",
                    }
                ],
                "requirement_assessment": [{"id": "REQ-001", "status": "changes_requested", "notes": "Implementation gaps remain."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "pass", "notes": "The acceptance scenario itself is not being moved."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [
                        {
                            "op": "move_acceptance_scenario",
                            "id": "done_when_item_2",
                            "from_task": "T002",
                            "to_task_hint": "agent integration follow-up",
                        }
                    ],
                    "affected_requirement_ids": ["REQ-001"],
                    "affected_acceptance_ids": [],
                    "notes": "The reviewer attempted to move a done_when item instead of an acceptance scenario.",
                },
                "intent_assessment": {
                    "status": "changes_requested",
                    "missing_done_when": ["Gateway is wired into agents"],
                    "violated_non_goals": [],
                    "notes": "The done_when gap remains.",
                },
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "pending"
    assert item["review_status"] == "changes_requested"
    assert "invalid task-contract repair" in item["blocked_reason"]
    assert "done_when_item_2 is not declared on source task T002" in item["blocked_reason"]
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_repair_invalid"]["error"] == "done_when_item_2 is not declared on source task T002"
    assert runtime_state["task_contract_blocker"] is None
    assert runtime_state["pending_scope_report"] is None

def test_cmd_code_review_rejects_pass_when_latest_task_tests_failed(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "status_session_id": "ses-1",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": "T002",
                    "timestamp": "2026-06-24T01:01:00Z",
                    "test_files": ["backend/tests/test_feature.py"],
                    "test_types": ["unit"],
                    "requirement_ids": ["REQ-001"],
                    "passed": False,
                    "passed_count": 0,
                    "failed_count": 1,
                    "failures": [{"test": "backend/tests/test_feature.py", "message": "assertion failed", "traceback": "..."}],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {},
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "Looks complete."},
                ],
                "acceptance_assessment": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert "latest task validation failed" in payload["items"][0]["blocked_reason"]
    assert (tmp_path / "docs" / "reviews" / "code-review-T002.md").exists()

def test_cmd_code_review_rejects_prefinal_audit_pass_when_report_missing(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Pre-final full-system audit",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS),
                    "status_session_id": "ses-audit",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": PREFINAL_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:01:00Z",
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
    input_path = tmp_path / "audit-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Audit ready.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id=PREFINAL_AUDIT_TASK_ID, input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert "required audit report is missing" in payload["items"][0]["blocked_reason"]
    assert (tmp_path / "docs" / "reviews" / "code-review-T-SYSTEM-AUDIT.md").exists()

def test_cmd_code_review_rejects_prefinal_audit_pass_when_report_declares_blockers(tmp_path: Path, monkeypatch) -> None:
    report_path = tmp_path / PREFINAL_AUDIT_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "# System Audit\n\n"
        "## Audit Scope\n\n"
        "## Executive Verdict\n\n**Overall: DEFER** — T-FINAL should not proceed until critical blockers are fixed.\n\n"
        "## Fixed Issues\n\n"
        "## Remaining Gaps / Blockers\n\nCritical release-blocking issue remains.\n\n"
        "## Requirement Gap Matrix\n\n"
        "## Validation Summary\n\n"
        "## Changed Files\n\n"
        "## Final Recommendation\n\nThe system fails pre-final validation.\n",
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
                    "title": "Pre-final full-system repair pass",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS),
                    "status_session_id": "ses-system-audit",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": PREFINAL_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:01:00Z",
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
    input_path = tmp_path / "audit-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Audit ready.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id=PREFINAL_AUDIT_TASK_ID, input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert "T-FINAL should not proceed" in payload["items"][0]["blocked_reason"]

def test_cmd_code_review_rejects_frontend_api_audit_pass_when_report_missing(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": FRONTEND_API_AUDIT_TASK_ID,
                    "title": "Frontend API integration audit",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(FRONTEND_API_AUDIT_OUTPUT_PATHS),
                    "status_session_id": "ses-frontend-audit",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": FRONTEND_API_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:01:00Z",
                    "test_files": list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
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
    input_path = tmp_path / "frontend-audit-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Audit ready.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id=FRONTEND_API_AUDIT_TASK_ID, input=str(input_path)))

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["import_exit_code"] == 2
    assert output["import_outcome"] == "accepted_changes_requested"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert "required frontend/API audit report is missing" in payload["items"][0]["blocked_reason"]
    assert (tmp_path / "docs" / "reviews" / f"code-review-{FRONTEND_API_AUDIT_TASK_ID}.md").exists()
    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "imported"
    assert handoff["skill"] == "code-review"
    assert handoff["task_id"] == FRONTEND_API_AUDIT_TASK_ID
    assert handoff["import_exit_code"] == 2
    assert handoff["import_outcome"] == "accepted_changes_requested"

def test_cmd_code_review_rejects_frontend_api_audit_pass_when_stubs_are_nonblocking(tmp_path: Path, monkeypatch) -> None:
    report_path = tmp_path / FRONTEND_API_AUDIT_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "# Frontend API Integration Audit\n\n"
        "## Audit Scope\n\n"
        "## API Surface Mapping\n\nBackend stub routes added and implemented as stubs.\n\n"
        "## Mocked Browser Test Assessment\n\n"
        "## Real Backend E2E Readiness\n\n"
        "## Test Data / Environment Readiness\n\n"
        "## Fixed Issues\n\nStub endpoint added in production route.\n\n"
        "## Remaining Gaps / Blockers\n\n### Critical (functional blockers)\n**None.**\n\n"
        "## Validation Summary\n\n"
        "## Final Recommendation\n",
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
                    "id": FRONTEND_API_AUDIT_TASK_ID,
                    "title": "Frontend API integration audit",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(FRONTEND_API_AUDIT_OUTPUT_PATHS),
                    "status_session_id": "ses-frontend-audit",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": FRONTEND_API_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:01:00Z",
                    "test_files": list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
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
    input_path = tmp_path / "frontend-audit-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Audit ready.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id=FRONTEND_API_AUDIT_TASK_ID, input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert "production-path stubs" in payload["items"][0]["blocked_reason"]

def test_cmd_final_review_import_marks_t_final_verified(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "review_pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": [],
                    "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"],
                }
            ],
        },
    )
    input_path = tmp_path / "final-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Release ready.", "findings": []}), encoding="utf-8")
    spawned: list[str] = []
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: True)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: spawned.append(str(project_root)) or 123)

    result = cli.cmd_final_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    assert payload["items"][0]["review_status"] == "pass"
    assert payload["items"][0]["review_artifact"] == "docs/reviews/final-review.md"
    assert spawned == [str(tmp_path)]

def test_cmd_final_review_import_changes_requested_creates_repair_bundle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "review_status": "pass",
                    "git_commit": "abc123",
                },
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "review_pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": [],
                    "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"],
                },
            ],
        },
    )
    input_path = tmp_path / "final-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Release still misses the user-visible feature behavior.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": ["AS-001"],
                        "message": "Feature behavior is not complete enough for release.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_final_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "imported"
    assert output["import_exit_code"] == 2
    assert output["import_outcome"] == "accepted_changes_requested"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    repair_id = next(item["id"] for item in payload["items"] if item.get("task_kind") == "repair")
    assert by_id[repair_id]["title"] == "Final Review Repair Bundle (T002)"
    assert by_id[repair_id]["requirements"] == ["REQ-001"]
    assert by_id[repair_id]["acceptance_scenarios"] == ["AS-001"]
    assert by_id[repair_id]["output_tests"] == ["backend/tests/test_feature.py"]
    assert by_id["T-FINAL"]["status"] == "blocked"
    assert by_id["T-FINAL"]["blocked_reason"] == f"final review created repair task {repair_id}; preserve verified tasks and repair through that bundle"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == repair_id
    assert runtime_state["repair_candidates"] == ["T002"]
    assert runtime_state["final_verify_status"] == "repair_required"
    review_md = (tmp_path / "docs" / "reviews" / "final-review.md").read_text(encoding="utf-8")
    assert "[blocking] Feature behavior is not complete enough for release. (REQ-001, AS-001)" in review_md

def test_cmd_final_review_import_changes_requested_stops_after_repair_iteration_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T004", "title": "Final Review Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "ghi789"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "jkl012"},
                {"id": "T-FINAL", "title": "最终验证", "status": "review_pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004", "T005"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "final-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Release still misses the user-visible feature behavior.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": [],
                        "message": "Feature behavior is still not release-ready.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_final_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "imported"
    assert output["import_exit_code"] == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    repair_tasks = [item for item in payload["items"] if item.get("task_kind") == "repair"]
    final = next(item for item in payload["items"] if item["id"] == "T-FINAL")
    assert [item["id"] for item in repair_tasks] == ["T003", "T004", "T005"]
    assert "maximum repair iterations (3)" in final["blocked_reason"]
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] is None
    assert runtime_state["repair_candidates"] == ["T002"]
    assert runtime_state["final_repair_limit_reached"] is True
    assert runtime_state["final_verify_status"] == "blocked"


def test_cmd_task_accept_manually_verifies_exception_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T010", "title": "Workflow", "status": "exception", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_workflow.py"], "output_paths": ["backend/src/workflow.py"], "blocked_reason": "review repair limit"},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T010", {"review_repair_limit_reached": True, "review_changes_requested_count": 4})

    result = cli.cmd_task(argparse.Namespace(project=str(tmp_path), task_id="T010", action="accept", reason="Host verified all tests and stale finding is resolved."))

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "verified"
    assert item["blocked_reason"] == "manual host acceptance: Host verified all tests and stale finding is resolved."
    runtime_state = load_task_runtime_state(tmp_path, "T010")
    assert runtime_state["manual_override"]["action"] == "accept"
    assert runtime_state["review_repair_limit_reached"] is False
    assert runtime_state["review_changes_requested_count"] == 0


def test_cmd_task_reset_repair_clears_repair_limit_and_returns_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T010", "title": "Workflow", "status": "exception", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_workflow.py"], "output_paths": ["backend/src/workflow.py"], "blocked_reason": "review repair limit", "attempts": 4},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T010", {"review_repair_limit_reached": True, "review_changes_requested_count": 4, "failure_count": 2})

    result = cli.cmd_task(argparse.Namespace(project=str(tmp_path), task_id="T010", action="reset-repair", reason="Allow one more repair turn after framework prompt update."))

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "pending"
    assert item["blocked_reason"] == "manual repair reset: Allow one more repair turn after framework prompt update."
    assert item["attempts"] == 0
    runtime_state = load_task_runtime_state(tmp_path, "T010")
    assert runtime_state["manual_override"]["action"] == "reset-repair"
    assert runtime_state["review_repair_limit_reached"] is False
    assert runtime_state["review_changes_requested_count"] == 0
    assert runtime_state["failure_count"] == 0


def test_cmd_task_reset_repair_refuses_verified_feature_task(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T010", "title": "Workflow", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_workflow.py"], "output_paths": ["backend/src/workflow.py"], "review_status": "pass", "verified_at": "2026-06-24T00:10:00Z"},
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_task(argparse.Namespace(project=str(tmp_path), task_id="T010", action="reset-repair", reason="Try to reopen."))

    assert exc_info.value.code == "verified_task_repair_forbidden"
    item = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"][0]
    assert item["status"] == "verified"
    assert item["verified_at"] == "2026-06-24T00:10:00Z"



def test_cmd_task_reset_repair_refuses_final_repair_after_iteration_limit(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "verified_at": "2026-06-24T00:10:00Z"},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "Final verification", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T003", "T004", "T005"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification reached maximum repair iterations (3); remaining failures require manual escalation"},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T-FINAL", {"final_repair_limit_reached": True, "repair_task_id": "T005"})

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_task(argparse.Namespace(project=str(tmp_path), task_id="T003", action="reset-repair", reason="Try again."))

    assert exc_info.value.code == "final_repair_limit_reached"
    item = next(item for item in json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"] if item["id"] == "T003")
    assert item["status"] == "verified"
    assert item["verified_at"] == "2026-06-24T00:10:00Z"


def test_final_repair_preserves_verified_prefinal_audit_and_updates_final_dependency() -> None:
    loop_module = import_module("delivery.loop")
    tasks = [
        Task.from_dict({"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []}),
        Task.from_dict({"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]}),
        Task.from_dict({"id": "T003", "title": "Final Verification Repair Bundle", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", PREFINAL_AUDIT_TASK_ID], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]}),
        Task.from_dict({"id": PREFINAL_AUDIT_TASK_ID, "title": "Pre-final full-system audit", "status": "verified", "task_kind": "audit", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS), "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS), "git_commit": "abc", "review_status": "pass"}),
        Task.from_dict({"id": FINAL_VERIFY_TASK_ID, "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": [PREFINAL_AUDIT_TASK_ID], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]}),
    ]

    updated = loop_module._invalidate_prefinal_audit_after_repair(tasks, "T003")
    audit = next(task for task in updated if task.id == PREFINAL_AUDIT_TASK_ID)
    final = next(task for task in updated if task.id == FINAL_VERIFY_TASK_ID)

    assert audit.status == "verified"
    assert audit.git_commit == "abc"
    assert audit.review_status == "pass"
    assert audit.blocked_reason is None
    assert final.dependencies == [PREFINAL_AUDIT_TASK_ID, "T003"]

def test_final_verify_creates_environment_repair_for_environment_failures(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop import DeliveryLoop
    from delivery.verify import TestFailure, TestResult

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["frontend/e2e/feature.spec.ts"], "output_paths": ["frontend/src/feature.tsx"]},
                {"id": PREFINAL_AUDIT_TASK_ID, "title": "Pre-final full-system audit", "status": "verified", "task_kind": "audit", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS), "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS)},
                {"id": FINAL_VERIFY_TASK_ID, "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [PREFINAL_AUDIT_TASK_ID], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    result = TestResult(
        task_id="T002",
        timestamp="2026-06-24T01:00:00Z",
        test_files=["frontend/e2e/feature.spec.ts"],
        test_types=["browser", "e2e"],
        requirement_ids=["REQ-001"],
        passed=False,
        passed_count=0,
        failed_count=1,
        failures=[TestFailure("frontend/e2e/feature.spec.ts", "test environment not ready (browser_e2e): shared test services did not become healthy", "{}", failure_kind="environment_not_ready")],
        attempt=1,
    )
    monkeypatch.setattr("delivery.loop.run_full_suite", lambda project_root, mode="all": [result])
    monkeypatch.setattr("delivery.loop.refresh_gates", lambda project_root: {"gates": []})
    monkeypatch.setattr("delivery.loop.check_requirements_coverage", lambda project_root, requirements_payload: {"total": 1, "covered": 1, "uncovered": [], "all_covered": True})
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])
    monkeypatch.setattr("delivery.loop.render_release_evidence", lambda project_root, summary, requirement_coverage, missing_test_types: docs_dir / "release-evidence.md")
    monkeypatch.setattr("delivery.loop.write_final_repair_report", lambda project_root, results, requirement_coverage, missing_test_types, repair_candidates: "docs/reviews/final-repair-report.md")

    payload = DeliveryLoop(tmp_path, runtime="opencode").final_verify()

    assert payload["status"] == "environment_blocked"
    items = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))["items"]
    repair = next(item for item in items if item["title"] == "Validation Environment Repair Bundle")
    assert payload["repair_candidates"] == [repair["id"]]
    assert repair["task_kind"] == "repair"
    assert repair["output_tests"] == ["frontend/e2e/feature.spec.ts"]
    assert "docker-compose.yml" in repair["output_paths"] or "docs/reviews/final-repair-report.md" in repair["output_paths"]
    final_task = next(item for item in items if item["id"] == FINAL_VERIFY_TASK_ID)
    assert final_task["status"] == "blocked"
    assert "environment repair task" in final_task["blocked_reason"]

def test_default_python_react_environment_repair_scope_is_explicit_stack_heuristic(tmp_path: Path) -> None:
    from delivery.environment_repair import default_python_react_environment_repair_scope_paths

    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "playwright.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core").mkdir(parents=True)
    (tmp_path / "backend" / ".env").write_text("DATABASE_URL=sqlite://\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    paths = default_python_react_environment_repair_scope_paths(tmp_path)

    assert "docker-compose.yml" in paths
    assert "frontend/playwright.config.ts" in paths
    assert "backend/.env" in paths

def test_cmd_verify_rejects_full_verify_when_non_final_tasks_are_incomplete(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T001",
                    "title": "Shared infra",
                    "status": "active",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/src/tests/test_health.py"],
                    "output_paths": ["backend/src/core/"],
                },
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": [],
                    "output_paths": [],
                },
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_verify(argparse.Namespace(project=str(tmp_path), runtime=None, mode="all", _locked=False))

    assert exc_info.value.code == "verify_project_incomplete"

def test_cmd_verify_non_verified_mode_allows_diagnostic_run_on_incomplete_project(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T001",
                    "title": "Shared infra",
                    "status": "active",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/src/tests/test_health.py"],
                    "output_paths": ["backend/src/core/"],
                },
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": [],
                    "output_paths": [],
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.__main__.DeliveryLoop.final_verify",
        lambda self, suite_mode="all": {"passed": False, "status": "blocked", "suite_mode": suite_mode},
    )

    result = cli.cmd_verify(argparse.Namespace(project=str(tmp_path), runtime=None, mode="non-verified", _locked=False))

    assert result == 1

