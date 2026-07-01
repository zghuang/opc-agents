from __future__ import annotations

import json
import os
from pathlib import Path

from delivery.loop import DeliveryLoop
from delivery.loop import recover
from delivery.loop_gitops import git_commit_task, task_scope_delta
from delivery.loop_reporting import project_summary, status as runtime_status
from delivery.loop_review import _parse_review_payload, _validate_pass_review_matrix, build_code_review_request, code_review_request_path, write_code_review_request
from delivery.loop_task_prompt import build_fix_prompt, build_stalled_recovery_prompt, build_task_prompt
from delivery.session import RuntimeErrorResponse, RuntimeSession, save_current_session
from delivery.state import load_session_state, load_task_runtime_state, save_session_state, save_task_runtime_state, save_test_results, save_work_items


def test_run_continues_after_task_exception_when_other_task_is_ready(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks, mark_task, save_tasks

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"]},
                {"id": "T002", "title": "功能", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T001", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(loop, "_pause_requested", lambda: False)
    monkeypatch.setattr(loop, "all_done", lambda tasks: False)

    def fake_execute(task):
        tasks = all_tasks(tmp_path)
        if task.id == "T001":
            save_tasks(tmp_path, mark_task(tasks, "T001", "exception", blocked_reason="need follow-up"))
            return (False, "exception")
        save_tasks(tmp_path, mark_task(tasks, "T002", "verified", verified_at="2026-06-24T00:30:00Z"))
        return (True, "verified")

    monkeypatch.setattr(loop, "_execute_task", fake_execute)

    result = loop.run()

    assert result == {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": ["T001"]}
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T001"]["status"] == "exception"
    assert by_id["T002"]["status"] == "verified"

def test_run_retries_exception_task_when_pending_tasks_depend_on_it(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"], "blocked_reason": "need retry"},
                {"id": "T002", "title": "功能", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T001", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    calls: list[str] = []

    def fake_pause_requested() -> bool:
        return len(calls) > 0

    monkeypatch.setattr(loop, "_pause_requested", fake_pause_requested)

    def fake_fix_exception_task(tasks):
        return next(task for task in tasks if task.id == "T001")

    monkeypatch.setattr(loop, "_next_exception_task_to_retry", fake_fix_exception_task)

    def fake_execute(task):
        calls.append(task.id)
        return (False, "exception")

    monkeypatch.setattr(loop, "_execute_task", fake_execute)

    result = loop.run()

    assert calls == ["T001"]
    assert result == {"status": "paused"}

def test_run_does_not_retry_exception_task_after_repeated_identical_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"], "blocked_reason": "same scope violation"},
                {"id": "T002", "title": "功能", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T001",
        {
            "status": "failed",
            "failure_kind": "scope_violation",
            "failure_signature": "abc123",
            "failure_count": 2,
        },
    )
    loop = DeliveryLoop(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(loop, "_pause_requested", lambda: False)
    monkeypatch.setattr(loop, "all_done", lambda tasks: False)
    monkeypatch.setattr(loop, "_execute_task", lambda task: calls.append(task.id) or (False, "exception"))

    result = loop.run()

    assert calls == []
    assert result == {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": ["T001"]}

def test_run_does_not_retry_exception_task_after_local_repair_budget_is_consumed(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "脚手架",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": [],
                },
                {
                    "id": "T001",
                    "title": "共享基础设施",
                    "status": "exception",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/"],
                    "blocked_reason": "failed after retry budget was exhausted",
                    "attempts": 4,
                },
                {
                    "id": "T002",
                    "title": "功能",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature/"],
                },
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(loop, "_pause_requested", lambda: False)
    monkeypatch.setattr(loop, "all_done", lambda tasks: False)
    monkeypatch.setattr(loop, "_execute_task", lambda task: calls.append(task.id) or (False, "exception"))

    result = loop.run()

    assert calls == []
    assert result == {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": ["T001"]}

def test_recover_preserves_matching_active_session_for_reuse(tmp_path: Path) -> None:
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
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/feature/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                    "attempts": 2,
                }
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "claude",
                "task_count": 1,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    updated = recover(tmp_path)

    payload = load_session_state(tmp_path)
    assert updated[0].status == "active"
    assert updated[0].status_session_id == "session-1"
    assert updated[0].started_at == "2026-06-24T00:01:00Z"
    assert updated[0].attempts == 2
    assert payload["active"]["id"] == "session-1"
    assert payload["active"]["current_task_id"] == "T002"
    assert payload["active"]["title"] == "Feature"
    assert payload["retired"] == []

def test_recover_keeps_running_active_task_in_place(tmp_path: Path) -> None:
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
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/feature/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                    "attempts": 2,
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "running",
            "runtime_pid": 1,
            "wrapper_pid": 1,
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "claude",
                "task_count": 1,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "active"
    assert updated[0].status_session_id == "session-1"
    assert updated[0].started_at == "2026-06-24T00:01:00Z"
    assert updated[0].attempts == 2

def test_run_prefers_resuming_active_task_before_pending_tasks(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Active feature",
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/active/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                },
                {
                    "id": "T003",
                    "title": "Pending feature",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/pending/"],
                },
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "claude",
                "task_count": 1,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Active feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    seen: list[str] = []

    def fake_execute_task(self, task):
        seen.append(task.id)
        return False, "review_pending"

    monkeypatch.setattr(DeliveryLoop, "_execute_task", fake_execute_task)

    loop = DeliveryLoop(tmp_path, runtime="claude")
    result = loop.run()

    assert seen == ["T002"]
    assert result["status"] == "review_pending"
    assert result["task_id"] == "T002"

def test_run_resumes_interrupted_active_task_before_pending_tasks(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Interrupted active feature",
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/active/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                },
                {
                    "id": "T003",
                    "title": "Pending feature",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/pending/"],
                },
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "running",
            "runtime_pid": 0,
            "wrapper_pid": 0,
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "claude",
                "task_count": 1,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Interrupted active feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    seen: list[str] = []

    def fake_execute_task(self, task):
        seen.append(task.id)
        return False, "review_pending"

    monkeypatch.setattr(DeliveryLoop, "_execute_task", fake_execute_task)

    loop = DeliveryLoop(tmp_path, runtime="claude")
    result = loop.run()

    assert seen == ["T002"]
    assert result["status"] == "review_pending"
    assert result["task_id"] == "T002"

def test_recover_does_not_reset_pending_tasks_with_history(tmp_path: Path) -> None:
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
                    "title": "Shared",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/"],
                    "git_commit": "abc123",
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/code-review-T001.md",
                    "reviewed_at": "2026-06-24T00:02:00Z",
                    "attempts": 2,
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "pending"
    assert updated[0].git_commit == "abc123"
    assert updated[0].review_status == "changes_requested"
    assert updated[0].attempts == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["id"] == "T001"
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["git_commit"] == "abc123"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert payload["items"][0]["attempts"] == 2

def test_recover_preserves_changes_requested_feedback_when_resetting_stale_pending_metadata(tmp_path: Path) -> None:
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
                    "title": "Login",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_login.py"],
                    "output_paths": ["backend/src/login/"],
                    "status_session_id": "ses-stale",
                    "started_at": "2026-06-24T00:01:00Z",
                    "completed_at": "2026-06-24T00:02:00Z",
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "reviewed_at": "2026-06-24T00:03:00Z",
                    "blocked_reason": "Tenant isolation behavior is still missing.",
                    "attempts": 2,
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "pending"
    assert updated[0].status_session_id is None
    assert updated[0].started_at is None
    assert updated[0].completed_at is None
    assert updated[0].review_status == "changes_requested"
    assert updated[0].review_artifact == "docs/reviews/code-review-T002.md"
    assert updated[0].blocked_reason == "Tenant isolation behavior is still missing."
    assert updated[0].attempts == 2

def test_recover_reconciles_verified_task_from_pass_review_and_commit(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "shared").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "shared" / "response.py"
    target.write_text("print('base')\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    review_path = tmp_path / "docs" / "reviews" / "code-review-T001.md"
    review_path.write_text("status: pass\nreview_type: code\nwork_item: T001\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "feat(T001): Shared"], cwd=tmp_path)

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
                    "title": "Shared",
                    "status": "active",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/response.py"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "verified"
    assert updated[0].git_commit is not None
    assert updated[0].review_status == "pass"
    assert updated[0].review_artifact == "docs/reviews/code-review-T001.md"

def test_recover_preserves_invalid_verified_scaffold_without_git_history(tmp_path: Path) -> None:
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
                    "title": "脚手架",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/"],
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T000.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T001",
                    "title": "共享基础设施",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": [],
                    "output_paths": ["backend/src/core/"],
                },
            ],
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T000"].status == "verified"
    assert by_id["T000"].review_status == "pass"
    assert by_id["T000"].review_artifact == "docs/reviews/code-review-T000.md"
    assert by_id["T000"].verified_at == "2026-06-24T00:02:00Z"

def test_recover_preserves_verified_task_without_commit_or_review_evidence(tmp_path: Path) -> None:
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
                    "title": "共享基础设施",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/"],
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T001.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "verified"
    assert updated[0].git_commit is None
    assert updated[0].review_status == "pass"
    assert updated[0].review_artifact == "docs/reviews/code-review-T001.md"
    assert updated[0].verified_at == "2026-06-24T00:02:00Z"
    assert updated[0].blocked_reason is None

def test_recover_preserves_verified_task_when_validation_gate_is_blocked(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git

    ensure_git_repo(tmp_path)
    (tmp_path / "frontend" / "src" / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "auth" / "LoginPage.tsx").write_text("export const LoginPage = () => null\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "reviews" / "code-review-T002.md").write_text(
        "status: pass\nreview_type: code\nwork_item: T002\n",
        encoding="utf-8",
    )
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "feat(T002): Auth"], cwd=tmp_path)
    commit_sha = git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

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
                    "title": "Auth",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/src/auth/LoginPage.test.tsx"],
                    "output_paths": ["frontend/src/auth/"],
                    "git_commit": commit_sha,
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T003",
                    "title": "Review",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/review-flow.spec.ts"],
                    "output_paths": ["frontend/src/rfq/"],
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.refresh_gates",
        lambda project_root: {
            "gates": [
                {
                    "id": "GATE-auth",
                    "status": "blocked",
                    "repair_candidates": ["T002"],
                }
            ]
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T002"].status == "verified"
    assert by_id["T002"].review_status == "pass"
    assert by_id["T002"].verified_at == "2026-06-24T00:02:00Z"
    assert by_id["T002"].blocked_reason is None

def test_run_does_not_reopen_verified_gate_repair_candidate_before_downstream_execution(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Auth",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/src/auth/LoginPage.test.tsx"],
                    "output_paths": ["frontend/src/auth/"],
                    "git_commit": "abc123",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T003",
                    "title": "Review",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/review-flow.spec.ts"],
                    "output_paths": ["frontend/src/rfq/"],
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.refresh_gates",
        lambda project_root: {
            "gates": [
                {
                    "id": "GATE-auth",
                    "status": "blocked",
                    "repair_candidates": ["T002"],
                }
            ]
        },
    )

    executed: list[str] = []

    def fake_execute(task):
        executed.append(task.id)
        return False, "review_pending"

    loop = DeliveryLoop(tmp_path)
    monkeypatch.setattr(loop, "_execute_task", fake_execute)

    result = loop.run()

    assert executed == ["T003"]
    assert result["status"] == "review_pending"
    assert result["task_id"] == "T003"

def test_recover_does_not_reopen_multiple_verified_gate_repair_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
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
                    "title": "Auth",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/src/auth/LoginPage.test.tsx"],
                    "output_paths": ["frontend/src/auth/"],
                    "git_commit": "abc123",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T003",
                    "title": "Review",
                    "status": "verified",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/review-flow.spec.ts"],
                    "output_paths": ["frontend/src/rfq/"],
                    "git_commit": "def456",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T003.md",
                    "verified_at": "2026-06-24T00:03:00Z",
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.refresh_gates",
        lambda project_root: {
            "gates": [
                {
                    "id": "GATE-release",
                    "status": "blocked",
                    "repair_candidates": ["T002", "T003"],
                }
            ]
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T002"].status == "verified"
    assert by_id["T003"].status == "verified"

def test_recover_resets_stale_pending_metadata_and_retires_orphan_session(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T003",
                    "title": "Feature",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature/"],
                    "status_session_id": "session-9",
                    "started_at": "2026-06-24T00:01:00Z",
                    "git_commit": "deadbeef",
                    "attempts": 1,
                }
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-9",
                "runtime": "claude",
                "task_count": 2,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
            },
            "retired": [],
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T003"].status == "pending"
    assert by_id["T003"].status_session_id is None
    assert by_id["T003"].started_at is None
    assert by_id["T003"].git_commit is None
    assert by_id["T003"].attempts == 0
    payload = load_session_state(tmp_path)
    assert payload["active"] is None
    assert payload["retired"][-1]["id"] == "session-9"

def test_recover_preserves_reusable_opencode_session_while_work_remains(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T003",
                    "title": "Feature",
                    "status": "exception",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature/"],
                    "attempts": 3,
                },
                {
                    "id": "T004",
                    "title": "Dependent",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T003"],
                    "output_tests": ["backend/tests/test_dependent.py"],
                    "output_paths": ["backend/src/dependent/"],
                },
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "ses-op-keep",
                "runtime": "opencode",
                "task_count": 4,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
                "last_heartbeat": "2026-06-24T00:05:00Z",
                "current_task_id": None,
            },
            "retired": [],
        },
    )

    recover(tmp_path)

    payload = load_session_state(tmp_path)
    assert payload["active"] is None
    assert payload["retired"][-1]["id"] == "ses-op-keep"
    assert payload["retired"][-1]["runtime"] == "opencode"
    assert payload["retired"][-1]["current_task_id"] is None

def test_run_blocks_active_repair_task_until_non_repair_feature_tasks_finish(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T007", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T010", "title": "Final Verification Repair Bundle (T007)", "status": "active", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T007"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T007", "T010"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )

    loop = DeliveryLoop(tmp_path)
    monkeypatch.setattr(loop, "_next_active_task_to_resume", lambda tasks: next(task for task in tasks if task.id == "T010"))
    monkeypatch.setattr(loop, "_execute_task", lambda task: (_ for _ in ()).throw(AssertionError("repair task should not execute")))

    result = loop.run()

    assert result["status"] == "blocked"
    assert result["task_id"] == "T010"
    assert result["blocked_task_ids"] == ["T007"]
