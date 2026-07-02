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


def test_cmd_init_project_defaults_watchdog_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path / "demo"

    result = cli.cmd_init_project(
        argparse.Namespace(
            project=str(project_root),
            description="demo",
            runtime="opencode",
            stack="python-react",
            framework_root=str(Path(__file__).resolve().parents[2]),
            force=False,
        )
    )

    assert result == 0
    metadata = json.loads((project_root / "docs" / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert metadata["watchdog_enabled"] is False


def test_cmd_init_project_can_enable_watchdog_explicitly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path / "demo"

    result = cli.cmd_init_project(
        argparse.Namespace(
            project=str(project_root),
            description="demo",
            runtime="opencode",
            stack="python-react",
            framework_root=str(Path(__file__).resolve().parents[2]),
            force=False,
            watchdog=True,
        )
    )

    assert result == 0
    metadata = json.loads((project_root / "docs" / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert metadata["watchdog_enabled"] is True

def test_cmd_resume_no_run_spawns_watchdog(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: calls.append("watchdog") or 123)

    result = cli.cmd_resume(argparse.Namespace(project=str(tmp_path), runtime="claude", no_run=True, _locked=False))

    assert result == 0
    assert calls == []

def test_cmd_fix_retires_matching_active_session(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"], "status_session_id": "session-1"},
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
            },
            "retired": [],
        },
    )

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert result == 0
    payload = load_session_state(tmp_path)
    assert payload["active"] is None
    assert payload["retired"][-1]["id"] == "session-1"

def test_cmd_fix_clears_completed_task_runtime_state(tmp_path: Path) -> None:
    review_path = tmp_path / "docs" / "reviews" / "code-review-T002.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("status: pass\nsummary: ok\n", encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "exception", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "task_id": "T002",
            "status": "completed",
            "started_at": "2026-06-24T00:00:00Z",
            "completed_at": "2026-06-24T00:05:00Z",
            "exit_code": 0,
            "session_id": "ses-old",
        },
    )

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert result == 0
    assert load_task_runtime_state(tmp_path, "T002") == {}
    assert not review_path.exists()

    from delivery.loop import recover

    updated = recover(tmp_path)
    task = next(task for task in updated if task.id == "T002")
    assert task.status == "pending"

def test_cmd_fix_refuses_to_reopen_verified_feature_task(tmp_path: Path) -> None:
    review_path = tmp_path / "docs" / "reviews" / "code-review-T002.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("status: pass\nsummary: ok\n", encoding="utf-8")
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
                    "output_paths": ["backend/src/feature/"],
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:05:00Z",
                },
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {"task_id": "T002", "status": "completed", "completed_at": "2026-06-24T00:05:00Z"},
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert exc_info.value.code == "verified_task_repair_forbidden"
    task = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"][0]
    assert task["status"] == "verified"
    assert task["verified_at"] == "2026-06-24T00:05:00Z"
    assert load_task_runtime_state(tmp_path, "T002")["status"] == "completed"
    assert review_path.exists()

def test_cmd_fix_terminates_active_runtime_processes_for_same_task(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"], "status_session_id": "session-1"},
            ],
        },
    )
    terminated: list[int] = []

    monkeypatch.setattr(
        cli,
        "load_active_task_records",
        lambda project_root: [{"task_id": "T002", "pid": 111, "runtime_pid": 222}],
    )
    monkeypatch.setattr(cli, "read_lock_metadata", lambda path: {"pid": 333})
    live_pids = {111, 222, 333}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid in live_pids:
                return
            raise ProcessLookupError(pid)
        terminated.append(pid)
        live_pids.discard(pid)

    monkeypatch.setattr(cli.os, "kill", fake_kill)

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert result == 0
    assert terminated == [222, 111, 333]


def test_cmd_fix_does_not_terminate_other_task_runtime_records(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"], "status_session_id": "session-1"},
            ],
        },
    )
    terminated: list[int] = []
    monkeypatch.setattr(
        cli,
        "load_active_task_records",
        lambda project_root: [
            {"task_id": "T999", "pid": 111, "runtime_pid": 222},
            {"task_id": "T002", "pid": 333, "runtime_pid": 444},
        ],
    )
    monkeypatch.setattr(cli, "read_lock_metadata", lambda path: {})
    live_pids = {111, 222, 333, 444}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid in live_pids:
                return
            raise ProcessLookupError(pid)
        terminated.append(pid)
        live_pids.discard(pid)

    monkeypatch.setattr(cli.os, "kill", fake_kill)

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert result == 0
    assert terminated == [444, 333]

def test_run_stalled_recovery_once_preserves_session_and_stages_recovery_prompt(tmp_path: Path, monkeypatch) -> None:
    from delivery.session import RuntimeSession, save_current_session
    from delivery.state import load_session_state, load_task_runtime_state, save_task_runtime_state

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T003", "title": "RFQ", "status": "active", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/src/tests/test_rfq/test_rfq_crud.py"], "output_paths": ["backend/src/rfq/"]},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T003", {"task_id": "T003", "status": "running", "started_at": "2026-06-24T00:00:00Z", "session_id": "ses-op-stall", "runtime_pid": 123, "wrapper_pid": 124})
    save_current_session(
        tmp_path,
        RuntimeSession(
            id="",
            runtime="opencode",
            task_count=0,
            created_at="2026-06-24T00:00:00Z",
            status="active",
            title="RFQ",
            last_heartbeat="2026-06-24T00:00:00Z",
            current_task_id="T003",
        ),
    )

    monkeypatch.setattr(cli, "_terminate_runtime_for_task", lambda project_root, task_id: None)
    monkeypatch.setattr(cli, "_run_resume_once", lambda project_root, runtime, no_run=False, locked=False, spawn_watchdog_after=True: {"status": "resumed", "task_id": "T003"})

    result = cli._run_stalled_recovery_once(tmp_path, task_id="T003", runtime="opencode", runtime_attention={"kind": "silent_stall", "message": "stalled"}, spawn_watchdog_after=False)

    payload = load_session_state(tmp_path)
    state = load_task_runtime_state(tmp_path, "T003")
    items = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"]
    item = next(row for row in items if row["id"] == "T003")

    assert result == {"status": "resumed", "task_id": "T003"}
    assert payload["active"]["id"] == "ses-op-stall"
    assert payload["active"]["current_task_id"] == "T003"
    assert item["status"] == "pending"
    assert state["recovery_reason"] == "stalled_runtime"
    assert "Previous run for task T003 appears stalled." in state["recovery_prompt"]

def test_cmd_watchdog_run_triggers_stalled_recovery(tmp_path: Path, monkeypatch) -> None:
    snapshots = [
        {
            "control_status": "running",
            "active_task": {"id": "T003"},
            "runtime_attention": {"suspected": True, "kind": "silent_stall", "message": "stalled"},
        },
        {
            "control_status": "paused",
            "active_task": None,
            "runtime_attention": None,
        },
    ]
    calls: list[str] = []

    monkeypatch.setattr(cli, "routed_status", lambda project_root: snapshots.pop(0))
    monkeypatch.setattr(cli, "_run_stalled_recovery_once", lambda project_root, task_id, runtime, runtime_attention=None, spawn_watchdog_after=True: calls.append(task_id) or {"status": "resumed"})

    result = cli.cmd_watchdog_run(argparse.Namespace(project=str(tmp_path), interval_seconds=5))

    assert result == 0
    assert calls == ["T003"]

def test_cmd_watchdog_run_waits_for_host_step_without_busy_loop(tmp_path: Path, monkeypatch) -> None:
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "kind": "host_skill",
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T004",
                "prompt": "review prompt",
                "expected_input_path": str(tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T004.json"),
            },
        },
        {
            "control_status": "paused",
            "active_task": None,
            "runtime_attention": None,
        },
    ]
    calls: list[str] = []

    monkeypatch.setattr(cli, "routed_status", lambda project_root: snapshots.pop(0))
    monkeypatch.setattr(cli, "cmd_control", lambda args: calls.append(str(args.goal)) or 0)

    result = cli.cmd_watchdog_run(argparse.Namespace(project=str(tmp_path), interval_seconds=5))

    assert result == 0
    assert calls == []
    state = json.loads((tmp_path / ".app-delivery-runtime" / "watchdog-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "waiting_for_host"
    assert state["last_action"] == "run_code_review"
    assert state["skill"] == "code-review"
    assert state["task_id"] == "T004"

def test_cmd_fix_reapplies_exception_patch(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "project").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "project" / "router.py"
    target.write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    patch_dir = tmp_path / ".app-delivery-runtime" / "exception-patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / "T002.patch"
    patch_path.write_text(
        """diff --git a/backend/src/project/router.py b/backend/src/project/router.py
index 1840f4f..0c54728 100644
--- a/backend/src/project/router.py
+++ b/backend/src/project/router.py
@@ -1 +1 @@
-print('base')
+print('patched')
""",
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
                {"id": "T002", "title": "Feature", "status": "exception", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/project/router.py"]},
            ],
        },
    )

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert result == 0
    assert target.read_text(encoding="utf-8") == "print('patched')\n"
    assert not patch_path.exists()

def test_cmd_fix_tolerates_already_applied_exception_patch(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "project").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "project" / "router.py"
    target.write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    patch_dir = tmp_path / ".app-delivery-runtime" / "exception-patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / "T002.patch"
    patch_path.write_text(
        """diff --git a/backend/src/project/router.py b/backend/src/project/router.py
index 1840f4f..0c54728 100644
--- a/backend/src/project/router.py
+++ b/backend/src/project/router.py
@@ -1 +1 @@
-print('base')
+print('patched')
""",
        encoding="utf-8",
    )

    target.write_text("print('patched')\n", encoding="utf-8")

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "exception", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/project/router.py"]},
            ],
        },
    )

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T002", no_start=True, runtime="claude"))

    assert result == 0
    assert target.read_text(encoding="utf-8") == "print('patched')\n"
    assert not patch_path.exists()


def test_cmd_fix_returns_nonzero_when_resume_finds_no_runnable_tasks(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        cli,
        "_run_fix_once",
        lambda project_root, task_id, runtime, no_start=False, spawn_watchdog_after=True: {
            "status": "exception",
            "reason": "no runnable tasks",
            "exception_task_ids": [task_id],
        },
    )

    result = cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T007", no_start=False, runtime="opencode"))

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["status"] == "exception"
    assert payload["fix_status"] == "not_resumed"
    assert "next_actions" in payload

def test_project_execution_guard_prunes_dead_pid_lock(tmp_path: Path, monkeypatch) -> None:
    lock_dir = tmp_path / ".app-delivery-runtime" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "execution.lock"
    lock_path.write_text('{"pid":999999,"heartbeat_at":"2026-06-24T00:00:00Z"}\n', encoding="utf-8")

    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False if pid == 999999 else True)

    removed = cli._prune_stale_execution_lock(tmp_path)

    assert removed is True
    assert not lock_path.exists()

