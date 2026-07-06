from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
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


def test_cmd_init_project_defaults_watchdog_enabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    assert metadata["watchdog_enabled"] is True
    assert metadata["host_fallback_enabled"] is True
    assert metadata["host_fallback_after_seconds"] == 600
    assert metadata["host_fallback_max_attempts"] == 1


def test_cmd_init_project_can_disable_watchdog_explicitly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path / "demo"

    result = cli.cmd_init_project(
        argparse.Namespace(
            project=str(project_root),
            description="demo",
            runtime="opencode",
            stack="python-react",
            framework_root=str(Path(__file__).resolve().parents[2]),
            force=False,
            watchdog=False,
        )
    )

    assert result == 0
    metadata = json.loads((project_root / "docs" / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert metadata["watchdog_enabled"] is False
    assert metadata["host_fallback_enabled"] is False


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
    prompt_path = tmp_path / ".app-delivery-runtime" / "prompts" / "T002.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("old prompt", encoding="utf-8")
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
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["force_task_prompt"] is True
    assert runtime_state["force_task_prompt_reason"] == "manual_fix"
    assert "status" not in runtime_state
    assert "session_id" not in runtime_state
    assert not review_path.exists()
    assert not prompt_path.exists()

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



def test_cmd_fix_refuses_final_repair_after_iteration_limit(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "Final verification", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T003", "T004", "T005"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification reached maximum repair iterations (3); remaining failures require manual escalation"},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T-FINAL", {"final_repair_limit_reached": True, "repair_task_id": "T005"})
    save_task_runtime_state(tmp_path, "T005", {"task_id": "T005", "status": "completed"})

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_fix(argparse.Namespace(project=str(tmp_path), task_id="T005", no_start=True, runtime="claude"))

    assert exc_info.value.code == "final_repair_limit_reached"
    task = next(item for item in json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"] if item["id"] == "T005")
    assert task["status"] == "verified"
    assert load_task_runtime_state(tmp_path, "T005")["status"] == "completed"


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


def test_spawn_watchdog_logs_output_and_replaces_stale_framework_process(tmp_path: Path, monkeypatch) -> None:
    from delivery.watchdog import spawn_watchdog

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"watchdog_enabled": True}), encoding="utf-8")
    state_path = tmp_path / ".app-delivery-runtime" / "watchdog-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"pid": 111, "framework_signature": "old"}), encoding="utf-8")
    killed: list[tuple[int, int]] = []
    popen_calls: list[dict[str, object]] = []

    class FakeProcess:
        pid = 222

    def fake_popen(command, **kwargs):
        popen_calls.append({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr("delivery.watchdog.process_alive", lambda pid: pid == 111)
    monkeypatch.setattr("delivery.watchdog._process_looks_like_project_watchdog", lambda pid, project_root: pid == 111 and project_root == tmp_path.resolve())
    monkeypatch.setattr("delivery.watchdog.os.kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("delivery.watchdog.subprocess.Popen", fake_popen)

    pid = spawn_watchdog(tmp_path)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert pid == 222
    assert killed == [(111, signal.SIGTERM)]
    assert len(popen_calls) == 1
    assert str(popen_calls[0]["stdout"].name).endswith(".out.log")
    assert str(popen_calls[0]["stderr"].name).endswith(".err.log")
    assert state["pid"] == 222
    assert state["framework_signature"]
    assert state["stdout_log"].endswith(".out.log")
    assert state["stderr_log"].endswith(".err.log")


def test_save_watchdog_state_preserves_existing_log_paths(tmp_path: Path) -> None:
    from delivery.watchdog import save_watchdog_state

    state_path = tmp_path / ".app-delivery-runtime" / "watchdog-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "pid": 111,
                "status": "running",
                "started_at": "2026-06-24T00:00:00Z",
                "stdout_log": str(tmp_path / ".app-delivery-runtime" / "logs" / "watchdog.out.log"),
                "stderr_log": str(tmp_path / ".app-delivery-runtime" / "logs" / "watchdog.err.log"),
            }
        ),
        encoding="utf-8",
    )

    save_watchdog_state(tmp_path, {"pid": 111, "status": "waiting_for_host", "last_action": "run_code_review"})

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "waiting_for_host"
    assert state["started_at"] == "2026-06-24T00:00:00Z"
    assert state["stdout_log"].endswith("watchdog.out.log")
    assert state["stderr_log"].endswith("watchdog.err.log")

def test_cmd_watchdog_run_keeps_monitoring_host_step_without_busy_loop(tmp_path: Path, monkeypatch) -> None:
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
    sleeps: list[int] = []
    saved_states: list[dict[str, object]] = []
    real_save_watchdog_state = cli.save_watchdog_state

    def capture_watchdog_state(project_root, payload):
        saved_states.append(dict(payload))
        real_save_watchdog_state(project_root, payload)

    monkeypatch.setattr(cli, "routed_status", lambda project_root: snapshots.pop(0))
    monkeypatch.setattr(cli, "cmd_control", lambda args: calls.append(str(args.goal)) or 0)
    monkeypatch.setattr(cli, "save_watchdog_state", capture_watchdog_state)
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    result = cli.cmd_watchdog_run(argparse.Namespace(project=str(tmp_path), interval_seconds=5))

    assert result == 0
    assert calls == []
    assert sleeps == [5]
    assert any(state.get("status") == "waiting_for_host" and state.get("task_id") == "T004" for state in saved_states)
    state = json.loads((tmp_path / ".app-delivery-runtime" / "watchdog-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "paused"


def test_cmd_watchdog_run_records_host_notice_and_exits(tmp_path: Path, monkeypatch) -> None:
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "kind": "host_notice",
                "owner": "host",
                "action": "review_gate_blocker",
                "task_id": "T004",
                "message": "Review gate report and decide whether to repair.",
            },
        },
    ]
    sleeps: list[int] = []

    monkeypatch.setattr(cli, "routed_status", lambda project_root: snapshots.pop(0))
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    result = cli.cmd_watchdog_run(argparse.Namespace(project=str(tmp_path), interval_seconds=5))

    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".app-delivery-runtime" / "watchdog-state.json").read_text(encoding="utf-8"))
    assert result == 0
    assert sleeps == []
    assert handoff["status"] == "notice"
    assert handoff["action"] == "review_gate_blocker"
    assert state["status"] == "host_notice"
    assert state["last_action"] == "review_gate_blocker"


def test_cmd_watchdog_run_imports_ready_host_review_artifact(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T004.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "pass", "summary": "ok", "findings": []}), encoding="utf-8")
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
                "expected_input_path": str(input_path),
            },
        },
        {
            "control_status": "paused",
            "active_task": None,
            "runtime_attention": None,
        },
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []

    monkeypatch.setattr(cli, "routed_status", lambda project_root: snapshots.pop(0))
    monkeypatch.setattr(cli, "import_task_review", lambda project_root, task_id, payload, input_path_arg: imported.append((task_id, dict(payload), input_path_arg)) or 0)

    result = cli.cmd_watchdog_run(argparse.Namespace(project=str(tmp_path), interval_seconds=5))

    assert result == 0
    assert imported == [("T004", {"status": "pass", "summary": "ok", "findings": []}, input_path.resolve())]
    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "imported"
    assert handoff["task_id"] == "T004"


def test_cmd_host_fallback_starts_code_review_oneshot_when_handoff_is_due(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps({"watchdog_enabled": True, "host_fallback_enabled": True, "host_fallback_after_seconds": 1, "host_fallback_max_attempts": 1}),
        encoding="utf-8",
    )
    request_path = tmp_path / ".app-delivery-runtime" / "review-requests" / "code-review-T004.md"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("review request\n", encoding="utf-8")
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T004.json"
    handoff_path = tmp_path / ".app-delivery-runtime" / "host-handoff.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "handoff_id": "handoff-1",
                "status": "waiting_for_host",
                "project": str(tmp_path),
                "requested_at": "2026-06-24T00:00:00Z",
                "updated_at": "2026-06-24T00:00:00Z",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T004",
                "expected_input_path": str(input_path),
                "next_step": {
                    "owner": "host",
                    "skill": "code-review",
                    "action": "run_code_review",
                    "task_id": "T004",
                    "expected_input_path": str(input_path),
                    "import_command": "app-delivery code-review --project demo --task-id T004 --input input.json",
                },
            }
        ),
        encoding="utf-8",
    )
    popen_calls: list[dict[str, object]] = []

    class FakeProcess:
        pid = 12345

    def fake_popen(command, **kwargs):
        popen_calls.append({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr(cli, "_resolve_hermes_bin", lambda: "/bin/hermes")
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = cli.cmd_host_fallback(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    lock = json.loads((tmp_path / ".app-delivery-runtime" / "host-fallback-lock.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".app-delivery-runtime" / "host-fallback.json").read_text(encoding="utf-8"))
    assert result == 0
    assert payload["status"] == "started"
    assert payload["task_id"] == "T004"
    assert len(popen_calls) == 1
    assert popen_calls[0]["command"][:2] == ["/bin/hermes", "--oneshot"]
    assert "--skills" in popen_calls[0]["command"]
    assert "app-delivery" in popen_calls[0]["command"]
    assert lock["status"] == "running"
    assert lock["handoff_id"] == "handoff-1"
    assert state["attempts"]["handoff-1"] == 1


def test_cmd_host_fallback_skips_non_code_review_handoff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"watchdog_enabled": True, "host_fallback_enabled": True}), encoding="utf-8")
    handoff_path = tmp_path / ".app-delivery-runtime" / "host-handoff.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(json.dumps({"status": "waiting_for_host", "skill": "final-review", "task_id": "T-FINAL"}), encoding="utf-8")

    result = cli.cmd_host_fallback(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {"status": "skipped", "reason": "unsupported_host_skill", "skill": "final-review"}


def test_cmd_host_fallback_reconciles_dead_superseded_fallback_lock(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"watchdog_enabled": True, "host_fallback_enabled": True}), encoding="utf-8")
    runtime_dir = tmp_path / ".app-delivery-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "host-fallback-lock.json").write_text(
        json.dumps({"status": "running", "handoff_id": "old", "task_id": "T004", "pid": 999999}),
        encoding="utf-8",
    )
    (runtime_dir / "host-handoff.json").write_text(
        json.dumps({"status": "waiting_for_host", "handoff_id": "new", "skill": "final-review", "task_id": "T-FINAL"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "process_alive", lambda pid: False)

    result = cli.cmd_host_fallback(argparse.Namespace(project=str(tmp_path)))

    output = json.loads(capsys.readouterr().out)
    state = json.loads((runtime_dir / "host-fallback.json").read_text(encoding="utf-8"))
    assert result == 0
    assert output == {"status": "skipped", "reason": "unsupported_host_skill", "skill": "final-review"}
    assert not (runtime_dir / "host-fallback-lock.json").exists()
    assert state["status"] == "superseded"
    assert state["handoff_id"] == "old"


def test_cmd_host_fallback_reconciles_imported_handoff_even_when_process_alive(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"watchdog_enabled": True, "host_fallback_enabled": True}), encoding="utf-8")
    runtime_dir = tmp_path / ".app-delivery-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "host-fallback-lock.json").write_text(
        json.dumps({"status": "running", "handoff_id": "done", "task_id": "T007", "pid": 12345}),
        encoding="utf-8",
    )
    (runtime_dir / "host-handoff.json").write_text(
        json.dumps({"status": "imported", "handoff_id": "done", "skill": "code-review", "task_id": "T007"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "process_alive", lambda pid: True)

    result = cli.cmd_host_fallback(argparse.Namespace(project=str(tmp_path)))

    output = json.loads(capsys.readouterr().out)
    state = json.loads((runtime_dir / "host-fallback.json").read_text(encoding="utf-8"))
    assert result == 0
    assert output == {"status": "skipped", "reason": "host_handoff_not_waiting", "handoff_status": "imported"}
    assert not (runtime_dir / "host-fallback-lock.json").exists()
    assert state["status"] == "completed"
    assert state["reason"] == "handoff_imported_while_fallback_process_alive"
    assert state["handoff_id"] == "done"


def test_host_fallback_start_clears_previous_handoff_terminal_fields(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps({"watchdog_enabled": True, "host_fallback_enabled": True, "host_fallback_after_seconds": 1, "host_fallback_max_attempts": 1}),
        encoding="utf-8",
    )
    runtime_dir = tmp_path / ".app-delivery-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "host-fallback.json").write_text(
        json.dumps(
            {
                "attempts": {"old-handoff": 1},
                "status": "completed",
                "handoff_id": "old-handoff",
                "task_id": "T006",
                "completed_at": "2026-06-24T00:10:00Z",
            }
        ),
        encoding="utf-8",
    )
    input_path = runtime_dir / "review-inputs" / "code-review-T007.json"
    handoff_path = runtime_dir / "host-handoff.json"
    handoff_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "handoff_id": "new-handoff",
                "status": "waiting_for_host",
                "project": str(tmp_path),
                "requested_at": "2026-06-24T00:00:00Z",
                "updated_at": "2026-06-24T00:00:00Z",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T007",
                "expected_input_path": str(input_path),
                "next_step": {
                    "owner": "host",
                    "skill": "code-review",
                    "action": "run_code_review",
                    "task_id": "T007",
                    "expected_input_path": str(input_path),
                    "import_command": "app-delivery code-review --project demo --task-id T007 --input input.json",
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeProcess:
        pid = 12345

    monkeypatch.setattr(cli, "_resolve_hermes_bin", lambda: "/bin/hermes")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: FakeProcess())

    result = cli.cmd_host_fallback(argparse.Namespace(project=str(tmp_path)))

    output = json.loads(capsys.readouterr().out)
    state = json.loads((runtime_dir / "host-fallback.json").read_text(encoding="utf-8"))
    assert result == 0
    assert output["status"] == "started"
    assert state["status"] == "running"
    assert state["handoff_id"] == "new-handoff"
    assert state["task_id"] == "T007"
    assert "completed_at" not in state
    assert state["attempts"] == {"old-handoff": 1, "new-handoff": 1}

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

def test_project_execution_guard_prunes_dead_pid_lock_before_zero_timeout_busy_check(tmp_path: Path, monkeypatch) -> None:
    lock_dir = tmp_path / ".app-delivery-runtime" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "execution.lock"
    lock_path.write_text('{"pid":999999,"heartbeat_at":"2026-06-24T00:00:00Z"}\n', encoding="utf-8")

    monkeypatch.setenv("APP_DELIVERY_EXECUTION_LOCK_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False if pid == 999999 else True)

    with cli._project_execution_guard(tmp_path, already_locked=False):
        metadata = cli.read_lock_metadata(lock_path)
        assert metadata["pid"] == os.getpid()

    assert cli.read_lock_metadata(lock_path) == {}

