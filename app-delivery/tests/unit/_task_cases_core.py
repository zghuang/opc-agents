from __future__ import annotations

import json
from pathlib import Path

import pytest

from delivery.builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from delivery.state import save_gates, save_task_runtime_state, save_test_plan, save_test_results, save_work_items
from delivery.task import Task, check_requirements_coverage, check_test_type_coverage, decompose_tasks, lint_task_contract, mark_task, pick_next_task, referenced_req_ids, reset_task, save_task_ledger, save_tasks
from delivery.gates import normalize_complexity_override, normalize_stage_gates, refresh_gates, validate_validation_tasks, validate_gate_references


def test_referenced_req_ids_expands_ranges() -> None:
    text = "REQ-001 to REQ-003 and NFR-001"
    assert referenced_req_ids(text) == ["REQ-001", "REQ-002", "REQ-003", "NFR-001"]

def test_pick_next_task_honors_verified_dependencies() -> None:
    tasks = [
        Task("T000", "脚手架", "verified", [], [], [], [], [], task_kind="feature"),
        Task("T001", "共享基础设施", "pending", [], [], ["T000"], [], [], task_kind="feature"),
        Task("T002", "功能", "pending", ["REQ-001"], [], ["T001"], [], [], task_kind="feature"),
    ]
    assert pick_next_task(tasks).id == "T001"

def test_pick_next_task_returns_none_when_dependencies_not_verified() -> None:
    tasks = [
        Task("T000", "脚手架", "verified", [], [], [], [], [], task_kind="feature"),
        Task("T001", "共享基础设施", "blocked", [], [], ["T000"], [], [], task_kind="feature"),
        Task("T002", "功能", "pending", ["REQ-001"], [], ["T001"], [], [], task_kind="feature"),
        Task("T003", "功能二", "pending", ["REQ-002"], [], ["T002"], [], [], task_kind="feature"),
    ]
    assert pick_next_task(tasks) is None

def test_pick_next_task_skips_dependency_exception_and_returns_independent_ready_task() -> None:
    tasks = [
        Task("T000", "脚手架", "verified", [], [], [], [], [], task_kind="feature"),
        Task("T001", "共享基础设施", "exception", [], [], ["T000"], [], [], task_kind="feature"),
        Task("T002", "功能", "pending", ["REQ-001"], [], ["T001"], [], [], task_kind="feature"),
        Task("T003", "功能二", "pending", ["REQ-002"], [], ["T000"], [], [], task_kind="feature"),
    ]
    assert pick_next_task(tasks).id == "T003"

def test_mark_task_sets_verified_timestamp() -> None:
    tasks = [Task("T001", "功能", "pending", [], [], [], [], [], task_kind="feature")]
    updated = mark_task(tasks, "T001", "verified")
    assert updated[0].status == "verified"
    assert updated[0].verified_at is not None


def test_save_tasks_rejects_unproven_verified_transition(tmp_path: Path) -> None:
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
                    "status": "exception",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/app/feature.py"],
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/exception-report-T002.md",
                }
            ],
        },
    )
    stale = Task.from_dict(
        {
            "id": "T002",
            "title": "Feature",
            "status": "verified",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/app/feature.py"],
            "review_status": "changes_requested",
            "review_artifact": "docs/reviews/exception-report-T002.md",
        }
    )

    with pytest.raises(ValueError, match="refusing invalid task transition T002"):
        save_tasks(tmp_path, [stale])

    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "exception"


def test_save_tasks_allows_proven_code_review_transition(tmp_path: Path) -> None:
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
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/app/feature.py"],
                }
            ],
        },
    )
    accepted = Task.from_dict(
        {
            "id": "T002",
            "title": "Feature",
            "status": "verified",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/app/feature.py"],
            "review_status": "pass",
            "review_artifact": "docs/reviews/code-review-T002.md",
            "git_commit": "abc123",
            "verification_source": "code_review",
            "verification_actor": "code_review",
            "verification_reason": "independent code review passed",
        }
    )

    save_tasks(tmp_path, [accepted])

    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    task_log = (tmp_path / ".app-delivery-runtime" / "task-log.jsonl").read_text(encoding="utf-8")
    assert '"message": "Verified task transition accepted"' in task_log
    assert '"verification_source": "code_review"' in task_log


def test_save_tasks_allows_manual_accept_transition(tmp_path: Path) -> None:
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
                    "status": "exception",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/app/feature.py"],
                }
            ],
        },
    )
    accepted = Task.from_dict(
        {
            "id": "T002",
            "title": "Feature",
            "status": "verified",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/app/feature.py"],
            "verification_source": "manual_accept",
            "verification_actor": "host",
            "verification_reason": "accept known external dependency",
        }
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "manual_override": {
                "action": "accept",
                "actor": "host",
                "reason": "accept known external dependency",
                "accepted_at": "2026-06-24T00:00:00Z",
            }
        },
    )

    save_tasks(tmp_path, [accepted])

    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"


def test_save_tasks_rejects_review_promotion_from_exception(tmp_path: Path) -> None:
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
                    "status": "exception",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/app/feature.py"],
                }
            ],
        },
    )
    stale = Task.from_dict(
        {
            "id": "T002",
            "title": "Feature",
            "status": "verified",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/app/feature.py"],
            "review_status": "pass",
            "review_artifact": "docs/reviews/code-review-T002.md",
            "git_commit": "abc123",
            "verification_source": "code_review",
            "verification_actor": "code_review",
            "verification_reason": "independent code review passed",
        }
    )

    with pytest.raises(ValueError, match="code_review is not valid from exception"):
        save_tasks(tmp_path, [stale])


def test_save_tasks_rejects_stale_verified_feature_regression(tmp_path: Path) -> None:
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
                    "output_paths": ["backend/app/feature.py"],
                }
            ],
        },
    )
    stale = Task.from_dict(
        {
            "id": "T002",
            "title": "Feature",
            "status": "active",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/app/feature.py"],
        }
    )

    with pytest.raises(ValueError, match="verified feature tasks cannot regress"):
        save_tasks(tmp_path, [stale])

    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"


def test_save_task_ledger_rejects_decompose_overwrite_of_verified_feature(tmp_path: Path) -> None:
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
                    "output_paths": ["backend/app/feature.py"],
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="verified feature tasks cannot regress"):
        save_task_ledger(
            tmp_path,
            {
                "schema_version": "2",
                "project": "demo",
                "generated_at": "2026-06-24T00:01:00Z",
                "last_updated_commit": "",
                "items": [
                    {
                        "id": "T002",
                        "title": "Feature",
                        "status": "pending",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": [],
                        "output_tests": ["backend/tests/test_feature.py"],
                        "output_paths": ["backend/app/feature.py"],
                    }
                ],
            },
        )

def test_mark_task_accumulates_session_history() -> None:
    tasks = [Task("T001", "功能", "pending", [], [], [], [], [], task_kind="feature")]
    updated = mark_task(tasks, "T001", "active", status_session_id="session-1")
    updated = mark_task(updated, "T001", "pending", status_session_id=None)
    updated = mark_task(updated, "T001", "active", status_session_id="session-2")
    updated = mark_task(updated, "T001", "review_pending", status_session_id="session-2")

    assert updated[0].status_session_id == "session-2"
    assert updated[0].session_ids == ["session-1", "session-2"]

def test_task_preserves_structured_technology_constraints() -> None:
    task = Task.from_dict(
        {
            "id": "T009",
            "title": "Orchestrator",
            "status": "pending",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": [],
            "output_paths": ["backend/demo_domain/workflows/incident_workflow.py"],
            "technology_constraints": [
                {
                    "name": "LangGraph",
                    "ecosystem": "backend",
                    "requirement": "must_use",
                    "reason": "ADR selects LangGraph.",
                    "source": "docs/adr/001-agent-framework.md",
                    "expected_evidence": ["imports StateGraph", "uses checkpointing"],
                },
                {"name": "", "ecosystem": "backend"},
                {"name": "LangGraph", "ecosystem": "backend"},
            ],
        }
    )

    assert task.technology_constraints == [
        {
            "name": "LangGraph",
            "ecosystem": "backend",
            "requirement": "must_use",
            "reason": "ADR selects LangGraph.",
            "source": "docs/adr/001-agent-framework.md",
            "expected_evidence": ["imports StateGraph", "uses checkpointing"],
        }
    ]
    assert Task.from_dict(task.to_dict()).technology_constraints == task.technology_constraints

def test_reset_task_clears_review_and_block_state() -> None:
    tasks = [
        Task(
            "T002",
            "功能",
            "blocked",
            "feature",
            ["REQ-001"],
            [],
            ["T001"],
            ["tests/test_feature.py"],
            ["backend/src/feature.py"],
            git_commit="abc",
            status_session_id="session-1",
            started_at="2026-06-24T00:00:00Z",
            completed_at="2026-06-24T00:10:00Z",
            review_status="changes_requested",
            review_artifact="docs/reviews/code-review-T002.md",
            reviewed_at="2026-06-24T00:11:00Z",
            verified_at="2026-06-24T00:12:00Z",
            blocked_reason="failed",
            attempts=3,
        )
    ]
    updated = reset_task(tasks, "T002")
    assert updated[0].status == "pending"
    assert updated[0].git_commit is None
    assert updated[0].review_status is None
    assert updated[0].blocked_reason is None
    assert updated[0].attempts == 0


def test_reset_task_refuses_verified_feature_task() -> None:
    tasks = [
        Task(
            "T002",
            "Feature",
            "verified",
            ["REQ-001"],
            [],
            [],
            [],
            ["backend/src/feature/"],
            task_kind="feature",
            verified_at="2026-06-24T00:12:00Z",
        )
    ]

    with pytest.raises(ValueError, match="do not reopen verified feature tasks"):
        reset_task(tasks, "T002")

