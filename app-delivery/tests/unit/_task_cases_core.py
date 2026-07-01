from __future__ import annotations

import json
from pathlib import Path

from delivery.builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from delivery.state import save_gates, save_test_plan, save_test_results, save_work_items
from delivery.task import Task, check_requirements_coverage, check_test_type_coverage, decompose_tasks, lint_task_contract, mark_task, pick_next_task, referenced_req_ids, reset_task
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

def test_mark_task_accumulates_session_history() -> None:
    tasks = [Task("T001", "功能", "pending", [], [], [], [], [], task_kind="feature")]
    updated = mark_task(tasks, "T001", "active", status_session_id="session-1")
    updated = mark_task(updated, "T001", "pending", status_session_id=None)
    updated = mark_task(updated, "T001", "active", status_session_id="session-2")
    updated = mark_task(updated, "T001", "review_pending", status_session_id="session-2")

    assert updated[0].status_session_id == "session-2"
    assert updated[0].session_ids == ["session-1", "session-2"]

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

