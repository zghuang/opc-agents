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


def test_execute_task_stops_for_external_review_when_tests_pass(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

def test_session_for_task_does_not_rotate_while_retrying_same_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    loop = DeliveryLoop(tmp_path, runtime="opencode", max_tasks_per_session=1)
    existing = RuntimeSession(
        id="ses-existing",
        runtime="opencode",
        task_count=99,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="T002 old",
        current_task_id="T002",
        last_heartbeat="2026-06-24T00:00:00Z",
    )
    touched: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr("delivery.loop.start_task_session", lambda project_root, runtime, task_id, title="": existing)
    monkeypatch.setattr("delivery.loop.retire_session", lambda project_root, session: (_ for _ in ()).throw(AssertionError("should not rotate same-task session")))
    monkeypatch.setattr("delivery.loop.save_current_session", lambda project_root, session: None)
    monkeypatch.setattr("delivery.loop.touch_session", lambda project_root, session, task_id=None, title=None: touched.append((task_id, title)))

    session = loop._session_for_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert session is existing
    assert touched == [("T002", "Login")]

def test_execute_task_skips_initial_runtime_when_scoped_changes_exist(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "feature.py"
    target.write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)
    target.write_text("print('updated')\n", encoding="utf-8")

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/feature.py"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/feature.py"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == []
    assert code_review_request_path(tmp_path, "T002").exists()

def test_execute_task_skips_relaunch_when_runtime_already_completed(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/feature.py"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "session_id": "ses-op-done",
            "completed_at": "2026-06-24T01:00:00Z",
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/feature.py"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == []

def test_execute_task_uses_persisted_recovery_prompt_before_validation(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T003", "title": "RFQ", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/src/tests/test_rfq/test_rfq_crud.py"], "output_paths": ["backend/src/rfq/"]},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T003", {"recovery_prompt": "Recover this task from the stalled run."})
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "RFQ"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T003"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T003", "RFQ", "pending", ["REQ-002"], [], [], ["backend/src/tests/test_rfq/test_rfq_crud.py"], ["backend/src/rfq/"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == ["Recover this task from the stalled run."]

def test_execute_task_relaunches_after_review_changes_requested_even_with_scoped_changes(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

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
                    "output_paths": ["backend/src/feature.py"],
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                },
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "session_id": "ses-op-done",
            "completed_at": "2026-06-24T01:00:00Z",
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: ["backend/src/feature.py"])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(
        Task(
            "T002",
            "Login",
            "pending",
            ["REQ-001"],
            [],
            [],
            ["backend/tests/test_login.py"],
            ["backend/src/feature.py"],
            review_status="changes_requested",
            review_artifact="docs/reviews/code-review-T002.md",
        )
    )

    assert success is False
    assert state == "review_pending"
    assert len(prompts) == 1

def test_execute_task_routes_new_out_of_scope_changes_into_review_without_relaunch(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    in_scope = tmp_path / "backend" / "src" / "auth" / "service.py"
    out_of_scope = tmp_path / "frontend" / "src" / "features" / "review" / "ReviewFlow.tsx"
    in_scope.parent.mkdir(parents=True, exist_ok=True)
    out_of_scope.parent.mkdir(parents=True, exist_ok=True)
    in_scope.write_text("print('base service')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'base'\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    in_scope.write_text("print('task change')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'task dirt'\n", encoding="utf-8")

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
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "session_id": "ses-op-done",
            "completed_at": "2026-06-24T01:00:00Z",
            "initial_changed_paths": [],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []
    attempts: list[int] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Auth"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    def _passed_result(task_payload: dict[str, object], attempt: int) -> object:
        attempts.append(attempt)
        return type(
            "R",
            (),
            {
                "passed": True,
                "passed_count": 1,
                "failed_count": 0,
                "failures": [],
                "test_files": task_payload.get("output_tests", []),
                "test_types": ["unit"],
                "requirement_ids": task_payload.get("requirements", []),
                "task_id": task_payload.get("id", ""),
            },
        )()

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: _passed_result(task, attempt))

    success, state = loop._execute_task(Task("T002", "Auth", "pending", ["REQ-001"], [], [], ["backend/tests/test_auth/test_login.py"], ["backend/src/auth/service.py"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == []
    assert attempts == [1]
    assert in_scope.read_text(encoding="utf-8") == "print('task change')\n"
    assert out_of_scope.read_text(encoding="utf-8") == "export const review = 'task dirt'\n"
    review_request = code_review_request_path(tmp_path, "T002").read_text(encoding="utf-8")
    assert "out_of_scope: frontend/src/features/review/ReviewFlow.tsx" in review_request
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["pending_scope_report"]["out_of_scope"] == ["frontend/src/features/review/ReviewFlow.tsx"]

def test_import_task_review_pass_commits_review_accepted_scope_paths(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.loop_review import import_task_review
    from delivery.task import all_tasks

    ensure_git_repo(tmp_path)
    in_scope = tmp_path / "backend" / "src" / "auth" / "service.py"
    out_of_scope = tmp_path / "frontend" / "src" / "shared" / "auth.ts"
    in_scope.parent.mkdir(parents=True, exist_ok=True)
    out_of_scope.parent.mkdir(parents=True, exist_ok=True)
    in_scope.write_text("print('base service')\n", encoding="utf-8")
    out_of_scope.write_text("export const auth = 'base'\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    in_scope.write_text("print('task change')\n", encoding="utf-8")
    out_of_scope.write_text("export const auth = 'task change'\n", encoding="utf-8")
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
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                    "status_session_id": "ses-op-1",
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "pending_scope_report": {
                "changed_paths": [
                    "backend/src/auth/service.py",
                    "frontend/src/shared/auth.ts",
                ],
                "staged_paths": ["backend/src/auth/service.py"],
                "out_of_scope": ["frontend/src/shared/auth.ts"],
                "preserved_paths": [],
            },
        },
    )

    input_path = tmp_path / "review-input.json"
    exit_code = import_task_review(
        tmp_path,
        "T002",
        {
            "status": "pass",
            "summary": "Scope deviation is acceptable for this task.",
            "findings": [],
            "requirement_assessment": [
                {"id": "REQ-001", "status": "pass", "notes": "The declared auth behavior is complete for this task."}
            ],
            "acceptance_assessment": [],
        },
        input_path,
    )

    assert exit_code == 0
    tasks = all_tasks(tmp_path)
    assert tasks[0].status == "verified"
    committed = git(["show", "--stat", "--name-only", "HEAD"], cwd=tmp_path).stdout
    assert "backend/src/auth/service.py" in committed
    assert "frontend/src/shared/auth.ts" in committed
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["accepted_scope_paths"] == ["frontend/src/shared/auth.ts"]
    assert runtime_state["pending_scope_report"] is None

def test_import_task_review_pass_ignores_unrelated_later_task_changes(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.loop_review import import_task_review
    from delivery.task import all_tasks

    ensure_git_repo(tmp_path)
    t005_file = tmp_path / "backend" / "src" / "otif" / "engine.py"
    t007_file = tmp_path / "backend" / "src" / "ai_infra" / "gateway.py"
    t005_file.parent.mkdir(parents=True, exist_ok=True)
    t007_file.parent.mkdir(parents=True, exist_ok=True)
    t005_file.write_text("print('base otif')\n", encoding="utf-8")
    t007_file.write_text("print('base gateway')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    t005_file.write_text("print('t005 change')\n", encoding="utf-8")
    t007_file.write_text("print('t007 unrelated change')\n", encoding="utf-8")

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T005",
                    "title": "OTIF",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_otif/test_engine.py"],
                    "output_paths": ["backend/src/otif/"],
                    "status_session_id": "ses-op-5",
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T005",
        {
            "status": "completed",
            "pending_scope_report": {
                "changed_paths": [
                    "backend/src/otif/engine.py",
                    "backend/src/ai_infra/gateway.py",
                ],
                "staged_paths": ["backend/src/otif/engine.py"],
                "out_of_scope": [],
                "preserved_paths": [],
            },
        },
    )

    input_path = tmp_path / "review-input.json"
    exit_code = import_task_review(
        tmp_path,
        "T005",
        {
            "status": "pass",
            "summary": "Looks good.",
            "findings": [],
            "requirement_assessment": [
                {"id": "REQ-001", "status": "pass", "notes": "The declared OTIF behavior is complete for this task."}
            ],
            "acceptance_assessment": [],
        },
        input_path,
    )

    assert exit_code == 0
    tasks = all_tasks(tmp_path)
    assert tasks[0].status == "verified"
    committed = git(["show", "--stat", "--name-only", "HEAD"], cwd=tmp_path).stdout
    assert "backend/src/otif/engine.py" in committed
    assert "backend/src/ai_infra/gateway.py" not in committed

def test_import_task_review_changes_requested_retires_matching_active_session(tmp_path: Path) -> None:
    from delivery.loop_review import import_task_review
    from delivery.task import all_tasks

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
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                    "status_session_id": "ses-op-1",
                    "attempts": 1,
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "pending_scope_report": {
                "changed_paths": ["backend/src/auth/service.py"],
                "staged_paths": ["backend/src/auth/service.py"],
                "out_of_scope": [],
                "preserved_paths": [],
            },
        },
    )
    save_current_session(
        tmp_path,
        RuntimeSession(
            id="ses-op-1",
            runtime="opencode",
            task_count=3,
            created_at="2026-06-24T00:00:00Z",
            status="active",
            title="Auth",
            last_heartbeat="2026-06-24T00:05:00Z",
            current_task_id="T002",
        ),
    )

    input_path = tmp_path / "review-input.json"
    exit_code = import_task_review(
        tmp_path,
        "T002",
        {
            "status": "changes_requested",
            "summary": "Needs rework.",
            "findings": [
                {
                    "severity": "blocking",
                    "requirement_ids": ["REQ-001"],
                    "acceptance_ids": [],
                    "message": "future task leakage",
                }
            ],
            "requirement_assessment": [
                {"id": "REQ-001", "status": "changes_requested", "notes": "Needs rework."}
            ],
            "acceptance_assessment": [],
        },
        input_path,
    )

    assert exit_code == 2
    tasks = all_tasks(tmp_path)
    assert tasks[0].status == "pending"
    assert tasks[0].status_session_id == "ses-op-1"
    assert tasks[0].review_status == "changes_requested"
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"] is None
    assert session_payload["retired"][-1]["id"] == "ses-op-1"

def test_import_task_review_pass_requires_explicit_passing_requirement_assessments(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

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
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                }
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        import_task_review(
            tmp_path,
            "T002",
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [],
                "acceptance_assessment": [],
            },
            tmp_path / "review-input.json",
        )

    assert exc_info.value.code == "review_assessment_invalid"
    assert "status=pass requires no blocking findings" in exc_info.value.message

def test_import_task_review_pass_rejects_mock_only_browser_e2e(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

    (tmp_path / "frontend" / "e2e").mkdir(parents=True)
    (tmp_path / "frontend" / "e2e" / "case.spec.ts").write_text(
        "import { test } from '@playwright/test'\n"
        "test('case flow', async ({ page }) => {\n"
        "  await page.route('**/api/incidents', route => route.fulfill({ status: 200, body: '{}' }))\n"
        "})\n",
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
                    "id": "T002",
                    "title": "Cases",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/e2e/case.spec.ts"],
                    "output_paths": ["frontend/src/routes/cases.tsx"],
                }
            ],
        },
    )

    result = import_task_review(
        tmp_path,
        "T002",
        {
            "status": "pass",
            "summary": "Looks good.",
            "findings": [],
            "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "ok"}],
            "acceptance_assessment": [],
        },
        tmp_path / "review-input.json",
    )

    assert result == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "pending"
    assert item["review_status"] == "changes_requested"
    assert "mocked browser proof is not real backend E2E evidence" in item["blocked_reason"]

def test_import_task_review_pass_accepts_route_fetch_passthrough_e2e(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_review import import_task_review

    (tmp_path / "frontend" / "e2e").mkdir(parents=True)
    (tmp_path / "frontend" / "e2e" / "case.spec.ts").write_text(
        "import { test } from '@playwright/test'\n"
        "test('case flow', async ({ page }) => {\n"
        "  await page.route('**/api/incidents', async route => {\n"
        "    try { const response = await route.fetch(); await route.fulfill({ response }) }\n"
        "    catch { await route.fulfill({ status: 200, body: '{}' }) }\n"
        "  })\n"
        "})\n",
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
                    "id": "T002",
                    "title": "Cases",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/e2e/case.spec.ts"],
                    "output_paths": ["frontend/src/routes/cases.tsx"],
                }
            ],
        },
    )
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = import_task_review(
        tmp_path,
        "T002",
        {
            "status": "pass",
            "summary": "Looks good.",
            "findings": [],
            "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "ok"}],
            "acceptance_assessment": [],
        },
        tmp_path / "review-input.json",
    )

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"

def test_import_task_review_pass_rejects_non_passing_acceptance_assessment(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

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
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                }
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        import_task_review(
            tmp_path,
            "T002",
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "Requirement is complete."}
                ],
                "acceptance_assessment": [
                    {"id": "AS-001", "status": "changes_requested", "notes": "Acceptance flow is incomplete."}
                ],
            },
            tmp_path / "review-input.json",
        )

    assert exc_info.value.code == "review_assessment_invalid"
    assert "non-passing acceptance assessments: AS-001" in exc_info.value.message

def test_import_task_review_pass_rejects_blocking_finding(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

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
                    "title": "Case workspace",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/e2e/case.spec.ts"],
                    "output_paths": ["frontend/src/case/CaseWorkspace.tsx"],
                }
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        import_task_review(
            tmp_path,
            "T002",
            {
                "status": "pass",
                "summary": "Mostly complete, but one behavior is missing.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": ["REQ-001"],
                        "message": "AI chat is still a placeholder.",
                    }
                ],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "The reviewer incorrectly marked this as pass."}
                ],
                "acceptance_assessment": [],
            },
            tmp_path / "review-input.json",
        )

    assert exc_info.value.code == "review_assessment_invalid"
    assert "blocking findings present" in exc_info.value.message

def test_import_task_review_pass_rejects_non_passing_intent_assessment(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

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
                    "title": "Case workspace",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/e2e/case.spec.ts"],
                    "output_paths": ["frontend/src/case/CaseWorkspace.tsx"],
                    "intent": {"objective": "Build case workspace", "done_when": ["AI chat streams agent output"]},
                }
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        import_task_review(
            tmp_path,
            "T002",
            {
                "status": "pass",
                "summary": "Looks good except intent is incomplete.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "Requirement assessment says pass."}
                ],
                "acceptance_assessment": [],
                "intent_assessment": {
                    "status": "changes_requested",
                    "missing_done_when": ["AI chat streams agent output"],
                    "violated_non_goals": [],
                    "notes": "AI chat is a local placeholder.",
                },
            },
            tmp_path / "review-input.json",
        )

    assert exc_info.value.code == "review_assessment_invalid"
    assert "intent_assessment is changes_requested" in exc_info.value.message

def test_execute_task_blocks_and_retires_session_on_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: (_ for _ in ()).throw(RuntimeErrorResponse("runtime command failed", "boom")))

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert item["status_session_id"] == "session-1"
    assert item["blocked_reason"] == "boom"
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"]["id"] == "session-1"

def test_execute_task_converts_unexpected_local_exception_into_task_exception(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: (_ for _ in ()).throw(ValueError("unexpected local failure")))

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert item["status_session_id"] == "session-1"
    assert item["blocked_reason"].startswith("framework task execution error: unexpected local failure")
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"]["id"] == "session-1"

def test_execute_task_runtime_failure_preserves_reusable_opencode_session(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path, runtime="opencode")

    class DummySession:
        id = "ses-op-1"
        runtime = "opencode"
        task_count = 2
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:05:00Z"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: (_ for _ in ()).throw(RuntimeErrorResponse("runtime command failed", "boom")))

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert item["status_session_id"] == "ses-op-1"
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"]["id"] == "ses-op-1"
    assert session_payload["active"]["runtime"] == "opencode"
    assert session_payload["active"]["current_task_id"] == "T002"
    assert session_payload["retired"] == []

def test_execute_task_reused_session_id_is_written_to_work_items(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    save_current_session(
        tmp_path,
        RuntimeSession(
            id="ses-shared",
            runtime="opencode",
            task_count=1,
            created_at="2026-06-24T00:00:00Z",
            status="active",
            title="Previous Task",
            last_heartbeat="2026-06-24T00:05:00Z",
            current_task_id=None,
        ),
    )
    loop = DeliveryLoop(tmp_path, runtime="opencode")
    session = RuntimeSession(
        id="ses-task",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:10:00Z",
        status="active",
        title="Login",
        last_heartbeat="2026-06-24T00:10:00Z",
        current_task_id="T002",
    )

    monkeypatch.setattr("delivery.loop.start_task_session", lambda project_root, runtime, task_id, title="": session)
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "review_pending"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status_session_id"] == "ses-task"
    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "ses-task" in work_items_md

def test_session_for_task_starts_new_session_for_new_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    loop = DeliveryLoop(tmp_path, runtime="opencode")
    new_session = RuntimeSession(
        id="ses-new",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:10:00Z",
        status="active",
        title="New Task",
        last_heartbeat="2026-06-24T00:10:00Z",
        current_task_id="T002",
    )
    touched: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr("delivery.loop.start_task_session", lambda project_root, runtime, task_id, title="": new_session)
    monkeypatch.setattr("delivery.loop.touch_session", lambda project_root, session, task_id=None, title=None: touched.append((task_id, title)))

    session = loop._session_for_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert session.id == "ses-new"
    assert touched == [("T002", "Login")]

def test_execute_task_does_not_loop_review_inside_core(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "review_pending"
    assert len(prompts) == 1
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["attempts"] == 1
    assert payload["items"][0]["status"] == "review_pending"
    assert code_review_request_path(tmp_path, "T002").exists()

def test_execute_task_exception_parks_scope_changes(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "workspace").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "workspace" / "router.py"
    target.write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    target.write_text("print('changed')\n", encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Workspace", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/workspace/router.py"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Workspace"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": [], "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.write_code_review_request", lambda project_root, task, scope_report=None: (_ for _ in ()).throw(RuntimeError("review request failed")))

    success, state = loop._execute_task(Task("T002", "Workspace", "pending", ["REQ-001"], [], [], [], ["backend/src/workspace/router.py"]))

    assert success is False
    assert state == "exception"
    assert target.read_text(encoding="utf-8") == "print('base')\n"
    patch_path = tmp_path / ".app-delivery-runtime" / "exception-patches" / "T002.patch"
    assert patch_path.exists()

def test_execute_task_writes_exception_report_artifact(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    target = tmp_path / "backend" / "src" / "workspace" / "router.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('base')\n", encoding="utf-8")
    ensure_git_repo(tmp_path)
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Workspace", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/workspace/router.py"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Workspace"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": [], "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.write_code_review_request", lambda project_root, task, scope_report=None: (_ for _ in ()).throw(RuntimeError("review request failed")))

    success, state = loop._execute_task(Task("T002", "Workspace", "pending", ["REQ-001"], [], [], [], ["backend/src/workspace/router.py"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["review_artifact"] == "docs/reviews/exception-report-T002.md"
    report_text = (tmp_path / "docs" / "reviews" / "exception-report-T002.md").read_text(encoding="utf-8")
    assert "# Exception Report" in report_text
    assert "review request failed" in report_text

def test_execute_task_blocks_if_task_disappears_mid_run(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: save_work_items(project_root, {"schema_version": "2", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "last_updated_commit": "", "items": []}) or {"text": "ok"})

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "blocked"

