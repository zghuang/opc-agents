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


def test_git_stage_task_snapshot_ignores_preserved_out_of_scope_paths(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_stage_task_snapshot
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

    in_scope.write_text("print('changed service')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'dirty'\n", encoding="utf-8")

    task = Task(
        "T002",
        "Auth",
        "pending",
        ["REQ-001"],
        [],
        [],
        ["backend/tests/test_auth/test_login.py"],
        ["backend/src/auth/service.py"],
    )

    staged = git_stage_task_snapshot(tmp_path, task, preserved_paths=["frontend/src/features/review/ReviewFlow.tsx"])

    assert staged == ["backend/src/auth/service.py"]

def test_restore_paths_to_head_removes_staged_added_paths_not_in_head(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, restore_paths_to_head

    ensure_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    package_init = tmp_path / "backend" / "src" / "domain" / "__init__.py"
    package_init.parent.mkdir(parents=True, exist_ok=True)
    package_init.write_text("# domain package\n", encoding="utf-8")
    git(["add", "--", "backend/src/domain/__init__.py"], cwd=tmp_path)

    restored = restore_paths_to_head(tmp_path, ["backend/src/domain/__init__.py"])

    assert restored == ["backend/src/domain/__init__.py"]
    assert not package_init.exists()
    assert git(["status", "--porcelain", "--", "backend/src/domain/__init__.py"], cwd=tmp_path).stdout.strip() == ""

def test_park_task_exception_changes_handles_staged_added_paths_not_in_head(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, park_task_exception_changes
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    package_init = tmp_path / "backend" / "src" / "domain" / "__init__.py"
    model_path = tmp_path / "backend" / "src" / "domain" / "models.py"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    package_init.write_text("# domain package\n", encoding="utf-8")
    model_path.write_text("MODEL = True\n", encoding="utf-8")
    git(["add", "--", "backend/src/domain/__init__.py", "backend/src/domain/models.py"], cwd=tmp_path)

    task = Task(
        "T002",
        "Core Backend Foundation",
        "pending",
        ["REQ-001"],
        [],
        [],
        [],
        ["backend/src/domain/models.py"],
    )

    patch_relative_path = park_task_exception_changes(tmp_path, task)

    assert patch_relative_path == ".app-delivery-runtime/exception-patches/T002.patch"
    assert (tmp_path / patch_relative_path).exists()
    assert not package_init.exists()
    assert not model_path.exists()
    assert git(
        ["status", "--porcelain", "--", "backend/src/domain/__init__.py", "backend/src/domain/models.py"],
        cwd=tmp_path,
    ).stdout.strip() == ""

def test_git_stage_task_snapshot_ignores_deleted_added_extra_paths(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_stage_task_snapshot
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    service_path = tmp_path / "backend" / "src" / "auth" / "service.py"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("VALUE = 'base'\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    service_path.write_text("VALUE = 'changed'\n", encoding="utf-8")
    stale_path = tmp_path / "frontend" / "src" / "pages" / "Dashboard.tsx"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text("export const Dashboard = () => null\n", encoding="utf-8")
    git(["add", "--", "frontend/src/pages/Dashboard.tsx"], cwd=tmp_path)
    stale_path.unlink()

    task = Task(
        "T017",
        "Dashboard",
        "pending",
        ["REQ-001"],
        [],
        [],
        ["backend/tests/test_auth.py"],
        ["backend/src/auth/service.py"],
    )

    staged = git_stage_task_snapshot(tmp_path, task, extra_paths=["frontend/src/pages/Dashboard.tsx"])

    assert staged == ["backend/src/auth/service.py"]
    status = git(["status", "--porcelain"], cwd=tmp_path).stdout
    assert "frontend/src/pages/Dashboard.tsx" not in status

def test_git_commit_explicit_paths_ignores_deleted_added_paths(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_commit_explicit_paths

    ensure_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    real_path = tmp_path / "docs" / "reviews" / "code-review-T017.md"
    stale_path = tmp_path / "docs" / "reviews" / "code-review-T018.md"
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_text("status: pass\n", encoding="utf-8")
    stale_path.write_text("status: pass\n", encoding="utf-8")
    git(["add", "--", "docs/reviews/code-review-T018.md"], cwd=tmp_path)
    stale_path.unlink()

    commit_sha = git_commit_explicit_paths(
        tmp_path,
        ["docs/reviews/code-review-T017.md", "docs/reviews/code-review-T018.md"],
        "feat(T017): review",
    )

    assert commit_sha
    status = git(["status", "--porcelain"], cwd=tmp_path).stdout
    assert "docs/reviews/code-review-T018.md" not in status
    assert git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=tmp_path).stdout.splitlines() == [
        "README.md",
        "docs/reviews/code-review-T017.md",
    ]

def test_git_commit_explicit_paths_restores_staged_framework_review_deletions(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_commit_explicit_paths

    ensure_git_repo(tmp_path)
    old_review = tmp_path / "docs" / "reviews" / "code-review-T017.md"
    new_review = tmp_path / "docs" / "reviews" / "code-review-T020.md"
    old_review.parent.mkdir(parents=True, exist_ok=True)
    old_review.write_text("status: pass\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    old_review.unlink()
    git(["add", "--", "docs/reviews/code-review-T017.md"], cwd=tmp_path)
    new_review.write_text("status: pass\n", encoding="utf-8")

    commit_sha = git_commit_explicit_paths(
        tmp_path,
        ["docs/reviews/code-review-T017.md", "docs/reviews/code-review-T020.md"],
        "feat(T020): review",
    )

    assert commit_sha
    assert old_review.exists()
    assert new_review.exists()
    assert git(["status", "--porcelain"], cwd=tmp_path).stdout.strip() == ""
    assert git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=tmp_path).stdout.splitlines() == [
        "docs/reviews/code-review-T017.md",
        "docs/reviews/code-review-T020.md",
    ]

def test_gate_report_paths_are_treated_as_framework_managed() -> None:
    from delivery.loop_gitops import _is_always_allowed_framework_path

    assert _is_always_allowed_framework_path("docs/reviews/gate-report-gate-auth.md") is True
    assert _is_always_allowed_framework_path("docs/reviews/exception-report-T007.md") is True

def test_project_summary_reports_unplanned_requirements(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": False, "scores": {}}})
    save_session_state(tmp_path, {"active": {"id": "session-1", "runtime": "claude", "task_count": 1, "created_at": "2026-06-24T00:00:00Z", "status": "active", "title": "Feature", "current_task_id": "T002", "last_heartbeat": "2026-06-24T00:05:00Z"}, "retired": []})

    payload = project_summary(tmp_path)

    assert payload["requirements"]["total"] == 2
    assert payload["requirements"]["unplanned"] == ["REQ-002"]
    assert payload["active_session"]["id"] == "session-1"
    assert payload["active_task"]["id"] == "T002"
    assert payload["delivery_claim_allowed"] is False
    assert payload["control_status_hint"] == "not_ready"
    assert "planning" in payload
    assert "tokens" in payload
    assert "task_metrics" in payload


def test_project_summary_task_duration_uses_first_runtime_start_across_review_repairs(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
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
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": [], "started_at": "2026-06-24T00:00:00Z", "completed_at": "2026-06-24T00:01:00Z", "verified_at": "2026-06-24T00:01:00Z"},
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": [], "started_at": "2026-06-24T00:20:00Z", "completed_at": "2026-06-24T00:30:00Z", "review_status": "pass", "reviewed_at": "2026-06-24T00:40:00Z", "verified_at": "2026-06-24T00:40:00Z"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "started_at": "2026-06-24T00:20:00Z",
            "completed_at": "2026-06-24T00:30:00Z",
        },
    )
    task_log = tmp_path / ".app-delivery-runtime" / "task-log.jsonl"
    task_log.parent.mkdir(parents=True, exist_ok=True)
    task_log.write_text(
        json.dumps({"ts": "2026-06-24T00:05:00Z", "runtime": "opencode", "level": "INFO", "message": "Started implementation T002", "task_id": "T002", "phase": "implementation"}) + "\n"
        + json.dumps({"ts": "2026-06-24T00:20:00Z", "runtime": "opencode", "level": "INFO", "message": "Started implementation T002", "task_id": "T002", "phase": "implementation"}) + "\n",
        encoding="utf-8",
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": True, "scores": {}}})

    payload = project_summary(tmp_path)
    metrics = {row["task_id"]: row for row in payload["task_metrics"]}

    assert metrics["T002"]["started_at"] == "2026-06-24T00:05:00Z"
    assert metrics["T002"]["completed_at"] == "2026-06-24T00:40:00Z"
    assert metrics["T002"]["duration_minutes"] == 35
    assert payload["duration"]["started_at"] == "2026-06-24T00:00:00Z"


def test_save_task_runtime_state_preserves_first_start_when_started_at_is_overwritten(tmp_path: Path) -> None:
    save_task_runtime_state(tmp_path, "T002", {"started_at": "2026-06-24T00:05:00Z", "status": "running"})
    save_task_runtime_state(tmp_path, "T002", {"started_at": "2026-06-24T00:20:00Z", "status": "running"})

    state = load_task_runtime_state(tmp_path, "T002")
    assert state["started_at"] == "2026-06-24T00:20:00Z"
    assert state["first_started_at"] == "2026-06-24T00:05:00Z"

def test_project_summary_prefers_active_task_record_phase(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
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
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": False, "scores": {}}})
    save_session_state(tmp_path, {"active": {"id": "session-1", "runtime": "claude", "task_count": 1, "created_at": "2026-06-24T00:00:00Z", "status": "active", "title": "Feature", "current_task_id": "T002", "last_heartbeat": "2026-06-24T00:05:00Z"}, "retired": []})
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "review.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "task_id": "T002",
                "task_title": "review-T002",
                "phase": "review",
                "updated_at": "2026-06-24T00:06:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = project_summary(tmp_path)

    assert payload["active_task"]["id"] == "T002"
    assert payload["active_task"]["phase"] == "review"

def test_project_summary_marks_delivery_claim_allowed_only_when_final_is_verified(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
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
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "最终验证", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": True, "scores": {}}})

    payload = project_summary(tmp_path)

    assert payload["delivery_claim_allowed"] is True
    assert payload["control_status_hint"] == "complete"

def test_git_commit_task_allows_always_managed_artifacts(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "feature.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "work-items.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "work-items.md").write_text("# work items\n", encoding="utf-8")
    (tmp_path / "docs" / "test-results.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "project-summary.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "project-summary.md").write_text("# summary\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "prompts" / "T002.md").write_text("prompt\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "session-state.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "logs" / "turn-1.log").write_text("runtime log\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "active-tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "active-tasks" / "T002.json").write_text('{"pid": 0}\n', encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "task-log.jsonl").write_text('{"message":"started"}\n', encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "feature.py").write_text("print('updated')\n", encoding="utf-8")
    (tmp_path / "docs" / "work-items.json").write_text('{"updated": true}\n', encoding="utf-8")
    (tmp_path / "docs" / "work-items.md").write_text("# updated\n", encoding="utf-8")
    (tmp_path / "docs" / "test-results.json").write_text('{"results": []}\n', encoding="utf-8")
    (tmp_path / "docs" / "project-summary.json").write_text('{"updated": true}\n', encoding="utf-8")
    (tmp_path / "docs" / "project-summary.md").write_text("# updated summary\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "prompts" / "T002.md").write_text("updated prompt\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "session-state.json").write_text('{"active": null}\n', encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "logs" / "turn-1.log").write_text("updated runtime log\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "task-log.jsonl").write_text('{"message":"updated"}\n', encoding="utf-8")

    task = Task("T002", "Feature", "pending", ["REQ-001"], [], [], [], ["backend/src/feature.py"])

    commit_sha = git_commit_task(tmp_path, task, "feat(T002): feature")

    assert commit_sha
    committed_paths = git(["show", "--name-only", "--pretty=", "HEAD"], cwd=tmp_path).stdout
    assert ".app-delivery-runtime/logs/turn-1.log" not in committed_paths
    assert ".app-delivery-runtime/task-log.jsonl" not in committed_paths

def test_git_commit_task_allows_t001_foundation_entrypoints_and_lockfiles(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "e2e").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "backend" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "backend" / "conftest.py").write_text("# root conftest\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "main.py").write_text("print('main')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core" / "event_bus.py").write_text("print('event')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "core" / "test_event_bus.py").write_text("def test_event():\n    assert True\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => {})\n", encoding="utf-8")
    (tmp_path / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "frontend" / "e2e" / "app-shell.spec.ts").write_text("import { test } from '@playwright/test'\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='demo-updated'\n", encoding="utf-8")
    (tmp_path / "backend" / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    (tmp_path / "backend" / "conftest.py").write_text("# updated root conftest\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "main.py").write_text("print('updated main')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core" / "event_bus.py").write_text("print('updated event')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "core" / "test_event_bus.py").write_text("def test_event():\n    assert 1 == 1\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export default function App() { return 'ok' }\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => expect(true).toBe(true))\n", encoding="utf-8")
    (tmp_path / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.1'\n", encoding="utf-8")
    (tmp_path / "frontend" / "e2e" / "app-shell.spec.ts").write_text("import { test, expect } from '@playwright/test'\n", encoding="utf-8")

    task = Task(
        "T001",
        "共享基础设施",
        "pending",
        [],
        [],
        ["T000"],
        [
            "backend/tests/core/test_event_bus.py",
            "frontend/e2e/app-shell.spec.ts",
        ],
        [
            "backend/pyproject.toml",
            "backend/uv.lock",
            "backend/conftest.py",
            "backend/src/main.py",
            "backend/src/core/",
            "backend/src/shared/",
            "backend/src/tests/",
            "backend/tests/core/",
            "frontend/src/shared/",
            "frontend/src/lib/",
            "frontend/src/layouts/",
            "frontend/src/styles/",
            "frontend/src/App.tsx",
            "frontend/src/App.test.tsx",
            "frontend/pnpm-lock.yaml",
        ],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T001): shared infrastructure")

    assert commit_sha

def test_git_commit_task_allows_feature_integration_entry_files(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "frontend" / "src" / "layouts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "features" / "city").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export function App(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => {})\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "layouts" / "AppShell.tsx").write_text("export function AppShell(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "city" / "CityListPage.tsx").write_text("export function CityListPage(){return null}\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export function App(){return 'city'}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => expect(true).toBe(true))\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "layouts" / "AppShell.tsx").write_text("export function AppShell(){return 'shell'}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "city" / "CityListPage.tsx").write_text("export function CityListPage(){return 'page'}\n", encoding="utf-8")

    task = Task(
        "T003",
        "城市、区域与车辆管理",
        "pending",
        ["REQ-001"],
        [],
        ["T001"],
        ["frontend/e2e/city-management.spec.ts"],
        ["frontend/src/features/city/"],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T003): city feature")

    assert commit_sha

def test_git_commit_task_allows_package_init_for_declared_python_module_files(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "merchant" / "services").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "merchant" / "router.py").write_text("print('router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "services" / "merchant_service.py").write_text("print('service')\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "merchant" / "router.py").write_text("print('updated router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "services" / "merchant_service.py").write_text("print('updated service')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "__init__.py").write_text("\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "services" / "__init__.py").write_text("\n", encoding="utf-8")

    task = Task(
        "T004",
        "仓站与商家基础管理",
        "pending",
        ["REQ-003"],
        [],
        ["T003"],
        ["backend/tests/merchant/test_merchant_api.py"],
        [
            "backend/src/merchant/router.py",
            "backend/src/merchant/services/merchant_service.py",
        ],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T004): merchant feature")

    assert commit_sha

def test_git_commit_task_allows_feature_support_files_for_declared_services_and_pages(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "station" / "services").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "station" / "models").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "station" / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "station").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "features" / "station" / "pages").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "features" / "station" / "components").mkdir(parents=True, exist_ok=True)

    (tmp_path / "backend" / "src" / "station" / "services" / "pick_service.py").write_text("print('pick')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "handoff_service.py").write_text("print('handoff')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "readiness_service.py").write_text("print('ready')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "station_service.py").write_text("print('station')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "router.py").write_text("print('router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "models" / "station_operations.py").write_text("print('models')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "schemas" / "operations.py").write_text("print('schemas')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "station" / "test_pick_workflow.py").write_text("def test_pick():\n    assert True\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "pages" / "PickPage.tsx").write_text("export function PickPage(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "pages" / "StationDetailPage.tsx").write_text("export function StationDetailPage(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "api.ts").write_text("export const api = {}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "components" / "AlertPanel.tsx").write_text("export function AlertPanel(){return null}\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "station" / "services" / "pick_service.py").write_text("print('updated pick')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "station_service.py").write_text("print('updated station')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "router.py").write_text("print('updated router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "models" / "station_operations.py").write_text("print('updated models')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "schemas" / "operations.py").write_text("print('updated schemas')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "station" / "conftest.py").write_text("def helper():\n    return True\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "api.ts").write_text("export const api = { updated: true }\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "components" / "AlertPanel.tsx").write_text("export function AlertPanel(){return 'ok'}\n", encoding="utf-8")

    task = Task(
        "T007",
        "仓站运营：拣货、取货与备货确认",
        "pending",
        ["REQ-012"],
        [],
        ["T006"],
        ["backend/tests/station/test_pick_workflow.py", "frontend/e2e/station-operations.spec.ts"],
        [
            "backend/src/station/services/pick_service.py",
            "backend/src/station/services/handoff_service.py",
            "backend/src/station/services/readiness_service.py",
                "backend/src/station/services/station_service.py",
                "backend/src/station/router.py",
                "backend/src/station/models/station_operations.py",
                "backend/src/station/schemas/operations.py",
            "frontend/src/features/station/pages/PickPage.tsx",
            "frontend/src/features/station/pages/StationDetailPage.tsx",
        ],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T007): station ops")

    assert commit_sha

def test_git_stage_task_snapshot_includes_untracked_foundation_tests(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_stage_task_snapshot
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "e2e").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "main.py").write_text("print('main')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "tests" / "core" / "test_auth.py").write_text("def test_auth():\n    assert True\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core" / "auth.py").write_text("AUTH = True\n", encoding="utf-8")

    task = Task(
        "T001",
        "共享基础设施",
        "pending",
        [],
        [],
        ["T000"],
        ["backend/tests/core/"],
        ["backend/src/core/", "backend/tests/core/"],
    )

    staged = git_stage_task_snapshot(tmp_path, task)

    assert "backend/tests/core/test_auth.py" in staged
    assert "backend/src/core/auth.py" in staged
    diff_names = git(["diff", "--cached", "--name-only"], cwd=tmp_path).stdout.splitlines()
    assert "backend/tests/core/test_auth.py" in diff_names
    assert "backend/src/core/auth.py" in diff_names

def test_git_commit_task_allows_t000_planning_artifacts(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mock-server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "modules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "adr").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "ui").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "architecture.md").write_text("arch\n", encoding="utf-8")
    (tmp_path / "docs" / "shared-components.md").write_text("shared\n", encoding="utf-8")
    (tmp_path / "docs" / "requirements.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "project-bootstrap.json").write_text('{"runtime": "claude"}\n', encoding="utf-8")
    (tmp_path / "docs" / "test-plan.json").write_text('{"coverage": []}\n', encoding="utf-8")
    (tmp_path / "docs" / "modules" / "01-module.md").write_text("module\n", encoding="utf-8")
    (tmp_path / "docs" / "adr" / "001-adr.md").write_text("adr\n", encoding="utf-8")
    (tmp_path / "docs" / "ui" / "states.md").write_text("states\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "stage-inputs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "docs" / "architecture.md").write_text("updated\n", encoding="utf-8")
    (tmp_path / "docs" / "project-bootstrap.json").write_text('{"runtime": "claude", "updated": true}\n', encoding="utf-8")
    (tmp_path / "docs" / "modules" / "01-module.md").write_text("updated\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json").write_text('{"updated": true}\n', encoding="utf-8")

    task = Task("T000", "脚手架", "pending", [], [], [], [], ["backend/", "frontend/"])

    commit_sha = git_commit_task(tmp_path, task, "chore(T000): scaffold")

    assert commit_sha

def test_git_commit_task_allows_t000_raw_planning_evidence(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_commit_task, task_scoped_changed_paths
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mock-server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "project-bootstrap.json").write_text('{"runtime": "opencode"}\n', encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "docs" / "arch-raw.json").write_text('{"architecture_md": "raw"}\n', encoding="utf-8")
    task = Task("T000", "脚手架", "pending", [], [], [], [], ["backend/", "frontend/"])

    scoped = task_scoped_changed_paths(tmp_path, task)
    commit_sha = git_commit_task(tmp_path, task, "chore(T000): scaffold")

    assert "docs/arch-raw.json" in scoped
    assert commit_sha

def test_git_commit_task_raises_when_commit_fails(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_commit_task as real_git_commit_task
    from delivery.task import Task
    import delivery.loop_gitops as loop_gitops

    ensure_git_repo(tmp_path)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "file.py").write_text("print('x')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)
    (tmp_path / "backend" / "file.py").write_text("print('y')\n", encoding="utf-8")
    task = Task("T000", "脚手架", "pending", [], [], [], [], ["backend/"])

    original_git = loop_gitops.git

    def fake_git(args, *, cwd):
        if args[:2] == ["commit", "-m"]:
            class Result:
                returncode = 1
                stdout = "simulated git commit failure"
            return Result()
        return original_git(args, cwd=cwd)

    loop_gitops.git = fake_git

    try:
        real_git_commit_task(tmp_path, task, "chore(T000): scaffold")
    except RuntimeError as exc:
        assert "simulated git commit failure" in str(exc)
    else:
        raise AssertionError("expected git commit failure to raise")
    finally:
        loop_gitops.git = original_git

def test_git_commit_task_rejects_unrelated_head_for_non_scaffold_task(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "main.py").write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "chore(T000): scaffold project"], cwd=tmp_path)

    task = Task("T007", "Feature", "pending", ["REQ-001"], [], [], [], ["backend/src/main.py"])

    try:
        git_commit_task(tmp_path, task, "feat(T007): feature")
    except RuntimeError as exc:
        assert "refusing to assign existing HEAD commit" in str(exc)
        assert "chore(T000): scaffold project" in str(exc)
    else:
        raise AssertionError("expected unrelated HEAD assignment to be rejected")

def test_run_builtin_scaffold_blocks_invalid_yaml_config(tmp_path: Path, monkeypatch) -> None:
    def fake_scaffold(project_root):
        root = Path(project_root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "docker-compose.yml").write_text(
            """services:\n  redpanda:\n    image: redpandadata/redpanda:v24.3.13\n    command:\n      - redpanda\n      - start\n      - --set\n      - redpanda.kafka_api=[{address: \"0.0.0.0\", port: 9092}]\n""",
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
                {"id": "T000", "title": "Scaffold", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["docker-compose.yml"]},
            ],
        },
    )
    monkeypatch.setattr("delivery.loop.scaffold_project", fake_scaffold)

    result = DeliveryLoop(tmp_path).run_builtin_scaffold()

    assert result["status"] == "exception"
    assert result["config_failures"][0]["path"] == "docker-compose.yml"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert "configuration validation failed" in item["blocked_reason"]
    assert (tmp_path / "docs" / "reviews" / "exception-report-T000.md").exists()

def test_final_verify_creates_dedicated_repair_task_without_reopening_verified_tasks(tmp_path: Path, monkeypatch) -> None:
    from delivery.verify import TestResult
    from delivery.task import all_tasks

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/service.py", "frontend/src/routes/login.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Review", "status": "verified", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/review.spec.ts"], "output_paths": ["backend/src/review/service.py", "frontend/src/routes/app/rfqs/$rfqId/route.tsx"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr(
        "delivery.loop.check_test_type_coverage",
        lambda project_root: [("REQ-002", "integration"), ("REQ-001", "typecheck"), ("REQ-001", "lint"), ("REQ-001", "build")],
    )

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    by_id = {task.id: task for task in tasks}
    repair_task_id = result["repair_task_id"]
    assert repair_task_id is not None
    assert by_id["T002"].status == "verified"
    assert by_id["T003"].status == "verified"
    assert by_id[repair_task_id].task_kind == "repair"
    assert by_id[repair_task_id].status == "pending"
    assert "frontend/e2e/auth.spec.ts" in by_id[repair_task_id].output_tests
    assert "npm run typecheck" in by_id[repair_task_id].output_tests
    assert "backend/tests/" in by_id[repair_task_id].output_tests
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == repair_task_id
    repair_runtime_state = load_task_runtime_state(tmp_path, repair_task_id)
    assert repair_runtime_state["force_task_prompt"] is True
    assert repair_runtime_state["force_task_prompt_reason"] == "final_verification_repair"

def test_final_verify_includes_blocked_gate_missing_type_specs_in_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.state import save_gates
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/service.py", "frontend/src/routes/login.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Review", "status": "verified", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/review.spec.ts"], "output_paths": ["backend/src/review/service.py", "frontend/src/routes/app/rfqs/$rfqId/route.tsx"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-release",
                    "kind": "release",
                    "title": "Release gate",
                    "status": "blocked",
                    "scope_tasks": ["T002", "T003"],
                    "scope_requirements": ["REQ-001", "REQ-002"],
                    "required_test_types": ["lint", "typecheck", "build", "browser"],
                    "missing_test_types": ["lint", "typecheck", "build"],
                    "repair_candidates": ["T002", "T003"],
                    "report_artifact": "docs/reviews/gate-report-GATE-release.md",
                    "source": "task-decompose",
                }
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [("REQ-002", "integration")])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    by_id = {task.id: task for task in tasks}
    repair_task_id = result["repair_task_id"]
    assert repair_task_id is not None
    assert "npm run lint" in by_id[repair_task_id].output_tests
    assert "npm run typecheck" in by_id[repair_task_id].output_tests
    assert "npm run build" in by_id[repair_task_id].output_tests
    assert "backend/tests/" in by_id[repair_task_id].output_tests

def test_final_verify_prioritizes_missing_type_specs_when_failed_test_list_is_already_full(tmp_path: Path, monkeypatch) -> None:
    from delivery.state import save_gates
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/service.py", "frontend/src/routes/login.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Review", "status": "verified", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/review.spec.ts"], "output_paths": ["backend/src/review/service.py", "frontend/src/routes/app/rfqs/$rfqId/route.tsx"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-release",
                    "kind": "release",
                    "title": "Release gate",
                    "status": "blocked",
                    "scope_tasks": ["T002", "T003"],
                    "scope_requirements": ["REQ-001", "REQ-002"],
                    "required_test_types": ["lint", "typecheck", "build", "browser"],
                    "missing_test_types": ["lint", "typecheck", "build"],
                    "repair_candidates": ["T002", "T003"],
                    "report_artifact": "docs/reviews/gate-report-GATE-release.md",
                    "source": "task-decompose",
                }
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)

    failed_specs = [f"tests/test_{index}.py" for index in range(10)]
    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=failed_specs,
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=10,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [("REQ-002", "integration")])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    by_id = {task.id: task for task in tasks}
    repair_task_id = result["repair_task_id"]
    assert repair_task_id is not None
    assert "npm run lint" in by_id[repair_task_id].output_tests
    assert "npm run typecheck" in by_id[repair_task_id].output_tests
    assert "npm run build" in by_id[repair_task_id].output_tests
    assert "backend/tests/" in by_id[repair_task_id].output_tests

def test_final_verify_normalizes_package_relative_failed_specs_for_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/src/App.test.tsx"], "output_paths": ["frontend/src/App.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)
    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["src/App.test.tsx", "e2e/smoke.spec.ts", "tests/test_health.py"],
                test_types=["unit", "browser", "e2e"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=3,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    repair_task = next(task for task in tasks if task.id == result["repair_task_id"])
    assert "frontend/src/App.test.tsx" in repair_task.output_tests
    assert "frontend/e2e/smoke.spec.ts" in repair_task.output_tests
    assert "backend/tests/test_health.py" in repair_task.output_tests
    assert "src/App.test.tsx" not in repair_task.output_tests
    assert "e2e/smoke.spec.ts" not in repair_task.output_tests
    assert "tests/test_health.py" not in repair_task.output_tests

def test_final_verify_reuses_exception_repair_task_without_creating_another(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "exception", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "blocked_reason": "repair budget exhausted", "attempts": 3},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "repair_candidates": ["T002"],
            "repair_task_id": "T003",
            "final_verify_status": "repair_required",
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    repair_tasks = [task for task in tasks if task.task_kind == "repair"]
    assert result["repair_task_id"] == "T003"
    assert len(repair_tasks) == 1
    assert repair_tasks[0].id == "T003"
    assert repair_tasks[0].status == "exception"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == "T003"

def test_final_verify_creates_next_repair_task_after_verified_repair_task_when_failures_remain(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_task_id": "T003", "final_verify_status": "repair_required"},
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    repair_tasks = [task for task in tasks if task.task_kind == "repair"]
    assert result["status"] == "repair_required"
    assert result["repair_task_id"] == "T004"
    assert [task.id for task in repair_tasks] == ["T003", "T004"]
    next_repair = next(task for task in repair_tasks if task.id == "T004")
    assert next_repair.status == "pending"
    assert next_repair.dependencies == ["T002", "T003"]
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == "T004"

def test_final_verify_stops_creating_repair_tasks_after_iteration_limit(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "ghi789"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "jkl012"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004", "T005"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_task_id": "T005", "final_verify_status": "repair_required"},
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    repair_tasks = [task for task in tasks if task.task_kind == "repair"]
    assert result["status"] == "blocked"
    assert result["repair_task_id"] == "T005"
    assert [task.id for task in repair_tasks] == ["T003", "T004", "T005"]
    final_task = next(task for task in tasks if task.id == "T-FINAL")
    assert "maximum repair iterations (3)" in str(final_task.blocked_reason)
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["final_verify_status"] == "blocked"
    assert runtime_state["final_repair_limit_reached"] is True

def test_final_verify_writes_repair_report_and_candidates(tmp_path: Path, monkeypatch) -> None:
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "NFR-001", "title": "Frontend", "summary": "Frontend checks"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["backend/tests/test_feature.py"],
                test_types=["api"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    assert result["status"] == "repair_required"
    assert result["repair_candidates"] == ["T002"]
    assert result["repair_report"] == "docs/reviews/final-repair-report.md"
    repair_report = (tmp_path / "docs" / "reviews" / "final-repair-report.md").read_text(encoding="utf-8")
    assert "repair_candidates: T002" in repair_report
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_candidates"] == ["T002"]
    assert runtime_state["final_verify_status"] == "repair_required"

def test_final_verify_blocks_when_validation_gate_is_not_verified(tmp_path: Path, monkeypatch) -> None:
    from delivery.state import save_gates
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "NFR-001", "title": "Frontend", "summary": "Frontend checks"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-auth",
                    "kind": "module",
                    "title": "Auth gate",
                    "status": "pending",
                    "scope_tasks": ["T002"],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["browser"],
                    "source": "task-decompose",
                }
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["backend/tests/test_feature.py"],
                test_types=["api"],
                requirement_ids=["REQ-001"],
                passed=True,
                passed_count=1,
                failed_count=0,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    assert result["status"] == "repair_required"
    assert result["repair_candidates"] == ["T002"]
    assert result["blocked_gates"][0]["id"] == "GATE-auth"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["final_verify_gate_statuses"][0]["id"] == "GATE-auth"
    assert runtime_state["final_verify_status"] == "repair_required"

def test_final_verify_blocks_unattributed_missing_test_coverage_without_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "NFR-002", "title": "Integration coverage", "summary": "Project-wide integration checks"},
                ],
                "acceptance_scenarios": [],
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["backend/tests/test_feature.py"],
                test_types=["api"],
                requirement_ids=["REQ-001"],
                passed=True,
                passed_count=1,
                failed_count=0,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [("NFR-002", "integration")])

    result = loop.final_verify()

    assert result["status"] == "blocked"
    assert result["repair_candidates"] == []
    assert result["repair_task_id"] is None
    by_id = {task.id: task for task in all_tasks(tmp_path)}
    assert by_id["T-FINAL"].blocked_reason == "final verification has missing test coverage without a strongly attributed repair owner: NFR-002:integration"

def test_final_verify_blocks_production_semantic_findings(tmp_path: Path, monkeypatch) -> None:
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "Dashboard", "summary": "Dashboard uses real backend data."}], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    api_path = tmp_path / "backend" / "src" / "api" / "v1" / "dashboard.py"
    api_path.parent.mkdir(parents=True, exist_ok=True)
    api_path.write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\nMOCK_DASHBOARD = {}\n@router.get('/dashboard')\nasync def dashboard():\n    return MOCK_DASHBOARD\n",
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
                {"id": "T002", "title": "Dashboard", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_dashboard.py"], "output_paths": ["backend/src/api/v1/dashboard.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["backend/tests/test_dashboard.py"],
                test_types=["unit"],
                requirement_ids=["REQ-001"],
                passed=True,
                passed_count=1,
                failed_count=0,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    assert result["status"] == "repair_required"
    assert result["semantic_findings"]
    assert result["semantic_report"] == "docs/reviews/production-semantic-scan.md"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["final_verify_semantic_findings"]
    assert runtime_state["final_verify_status"] == "repair_required"

def test_final_verify_defers_when_non_final_feature_tasks_are_incomplete(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
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
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )

    loop = DeliveryLoop(tmp_path)
    result = loop.final_verify()

    assert result["status"] == "deferred"
    assert result["deferred_task_ids"] == ["T002"]

def test_shared_foundation_enters_exception_when_shared_env_cannot_warm(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "共享基础设施", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/src/tests/test_health.py"], "output_paths": ["backend/src/core/"]},
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.warm_shared_test_environment",
        lambda project_root, reason="": type("Result", (), {"ready": False, "summary": "shared env unavailable"})(),
    )

    loop = DeliveryLoop(tmp_path)
    task = next(task for task in all_tasks(tmp_path) if task.id == "T001")
    success, state = loop._execute_task(task)

    assert success is False
    assert state == "exception"

def test_shared_foundation_enters_exception_when_browser_env_cannot_warm(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "共享基础设施", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/src/tests/test_health.py"], "output_paths": ["backend/src/core/"]},
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.warm_shared_test_environment",
        lambda project_root, reason="": type("Result", (), {"ready": True, "summary": "shared env ready"})(),
    )
    monkeypatch.setattr("delivery.loop.project_has_browser_e2e", lambda project_root: True)
    monkeypatch.setattr(
        "delivery.loop.warm_browser_e2e_environment",
        lambda project_root, reason="": type("Result", (), {"ready": False, "summary": "browser env unavailable"})(),
    )

    loop = DeliveryLoop(tmp_path)
    task = next(task for task in all_tasks(tmp_path) if task.id == "T001")
    success, state = loop._execute_task(task)

    assert success is False
    assert state == "exception"
    updated = next(task for task in all_tasks(tmp_path) if task.id == "T001")
    assert updated.blocked_reason == "browser env unavailable"

