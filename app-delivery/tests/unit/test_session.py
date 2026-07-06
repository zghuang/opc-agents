from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from delivery.session import RuntimeErrorResponse, RuntimeSession, _build_runtime_command, _extract_opencode_failure_metadata, _run, execute_in_session, retire_session, save_current_session, session_context_for, start_task_session, touch_session
from delivery.state import load_session_state, save_session_state


def test_build_runtime_command_uses_claude_wrapper_and_resume(tmp_path: Path) -> None:
    session = RuntimeSession(
        id="session-1",
        runtime="claude",
        task_count=1,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="bootstrap",
        current_task_id="T001",
    )
    command, log_file = _build_runtime_command(tmp_path, session)
    assert command[0] == sys.executable
    assert command[1].endswith("scripts/cc-exec.py")
    assert "--resume" in command
    assert "--task-id" in command
    assert "--task-title" in command
    assert log_file.name.startswith("claude-session-1-turn-")


def test_build_runtime_command_uses_opencode_wrapper(tmp_path: Path) -> None:
    session = RuntimeSession(
        id="session-2",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="bootstrap",
    )
    command, _ = _build_runtime_command(tmp_path, session)
    assert command[0] == sys.executable
    assert command[1].endswith("scripts/oc-exec.py")
    assert "--session-id" in command


def test_start_task_session_retires_previous_task_session(tmp_path: Path) -> None:
    existing = RuntimeSession(
        id="session-3",
        runtime="claude",
        task_count=2,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="Task A",
        current_task_id="T001",
    )
    save_current_session(tmp_path, existing)

    started = start_task_session(tmp_path, "claude", task_id="T002", title="Task B")

    payload = load_session_state(tmp_path)
    assert payload["active"]["id"] == started.id
    assert payload["active"]["current_task_id"] == "T002"
    assert payload["retired"][-1]["id"] == "session-3"


def test_start_task_session_reuses_same_task_session(tmp_path: Path) -> None:
    existing = RuntimeSession(
        id="session-4",
        runtime="claude",
        task_count=2,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="Task A",
        current_task_id="T001",
    )
    save_current_session(tmp_path, existing)

    started = start_task_session(tmp_path, "claude", task_id="T001", title="Task A")

    assert started.id == "session-4"
    payload = load_session_state(tmp_path)
    assert payload["active"]["id"] == "session-4"
    assert payload.get("retired", []) == []


def test_start_task_session_binds_one_task_per_session(tmp_path: Path) -> None:
    first = start_task_session(tmp_path, "opencode", task_id="T001", title="Task 1")
    second = start_task_session(tmp_path, "opencode", task_id="T002", title="Task 2")

    payload = load_session_state(tmp_path)
    assert payload["active"]["current_task_id"] == "T002"
    assert payload["retired"][-1]["current_task_id"] == "T001"
    assert first.current_task_id == "T001"
    assert second.current_task_id == "T002"


def test_session_context_for_enforces_single_task_boundary() -> None:
    session = RuntimeSession(
        id="session-5",
        runtime="opencode",
        task_count=1,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="Task A",
        current_task_id="T001",
    )

    prompt = session_context_for(session, "Task A", "Build only this task")

    assert "Session scope: this turn is for the current task only." in prompt
    assert "Do not begin another task or future work item in this session." in prompt
    assert "When the current task is complete, blocked, or ready for review, stop immediately." in prompt


def test_execute_in_session_can_skip_persisting_active_session(tmp_path: Path, monkeypatch) -> None:
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "main-session",
                "runtime": "claude",
                "task_count": 3,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Main task",
            },
            "retired": [],
        },
    )

    monkeypatch.setattr(
        "delivery.session._run",
        lambda command, cwd, input_text=None: json.dumps({"session_id": "review-session", "result": "ok"}),
    )

    review_session = RuntimeSession(
        id="review-session",
        runtime="claude",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="review-T001",
    )

    result = execute_in_session(tmp_path, review_session, "Review prompt", persist=False)

    assert result["text"] == "ok"
    payload = load_session_state(tmp_path)
    assert payload["active"]["id"] == "main-session"
    assert any(event.get("event") == "turn_completed" and event.get("session_id") == "review-session" for event in payload["history"])


def test_execute_in_session_syncs_runtime_support_for_opencode(tmp_path: Path, monkeypatch) -> None:
    seeded = RuntimeSession(
        id="",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="bootstrap",
        current_task_id="T001",
    )
    save_current_session(tmp_path, seeded)

    calls: list[tuple[str, str | None]] = []

    def fake_sync(project_root_arg, *, template_root=None, runtime=None):
        calls.append((str(project_root_arg), runtime))

    monkeypatch.setattr("delivery.session.sync_runtime_support", fake_sync)
    monkeypatch.setattr(
        "delivery.session._run",
        lambda command, cwd, input_text=None: '\n'.join([
            '{"sessionID":"ses-opencode"}',
            '{"type":"text","part":{"text":"ok"}}',
        ]),
    )

    session = RuntimeSession(
        id="session-2",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="bootstrap",
    )

    result = execute_in_session(tmp_path, session, "Build prompt", persist=False)

    assert result["text"] == "ok"
    assert calls == [(str(tmp_path), "opencode")]
    payload = load_session_state(tmp_path)
    assert any(event.get("event") == "session_id_resolved" and event.get("session_id") == "ses-opencode" for event in payload["history"])


def test_execute_in_session_persists_resolved_active_session_id(tmp_path: Path, monkeypatch) -> None:
    seeded = RuntimeSession(
        id="",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="bootstrap",
        current_task_id="T001",
    )
    save_current_session(tmp_path, seeded)

    monkeypatch.setattr("delivery.session.sync_runtime_support", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "delivery.session._run",
        lambda command, cwd, input_text=None: '\n'.join([
            '{"sessionID":"ses-opencode"}',
            '{"type":"text","part":{"text":"ok"}}',
        ]),
    )

    session = RuntimeSession(
        id="",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="bootstrap",
        current_task_id="T001",
    )

    result = execute_in_session(tmp_path, session, "Build prompt", persist=True)

    assert result["session_id"] == "ses-opencode"
    payload = load_session_state(tmp_path)
    assert payload["active"]["id"] == "ses-opencode"
    assert payload["active"]["current_task_id"] == "T001"


def test_extract_opencode_failure_metadata_reads_session_id_and_scope_violation() -> None:
    output = "\n".join(
        [
            '{"sessionID":"ses-op-42"}',
            "Task produced changes outside its declared scope.",
        ]
    )

    payload = _extract_opencode_failure_metadata(output)

    assert payload["session_id"] == "ses-op-42"
    assert payload["kind"] == "scope_violation"


def test_run_classifies_signal_exit_as_runtime_interrupted(monkeypatch, tmp_path: Path) -> None:
    command = ["python", "/tmp/oc-exec.py"]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(command, -15, stdout='{"sessionID":"ses-op-99"}\npermission auth token text\n')

    monkeypatch.setattr("delivery.session.subprocess.run", fake_run)

    with pytest.raises(RuntimeErrorResponse) as exc_info:
        _run(command, cwd=tmp_path)

    assert exc_info.value.kind == "runtime_interrupted"
    assert exc_info.value.session_id == "ses-op-99"
    assert exc_info.value.returncode == -15


def test_touch_session_updates_heartbeat_and_task_metadata(tmp_path: Path) -> None:
    session = RuntimeSession(
        id="session-4",
        runtime="claude",
        task_count=0,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="Old title",
    )
    save_current_session(tmp_path, session)

    touch_session(tmp_path, session, task_id="T007", title="New title")

    payload = load_session_state(tmp_path)
    assert payload["active"]["title"] == "New title"
    assert payload["active"]["current_task_id"] == "T007"
    assert payload["active"]["last_heartbeat"] is not None


def test_retire_session_preserves_transient_opencode_session_history(tmp_path: Path) -> None:
    session = RuntimeSession(
        id="ses-op-1",
        runtime="opencode",
        task_count=1,
        created_at="2026-06-25T00:00:00Z",
        status="active",
        title="review-T003",
    )

    retire_session(tmp_path, session)

    payload = load_session_state(tmp_path)
    assert payload["retired"][-1]["id"] == "ses-op-1"
    assert any(event.get("event") == "retired" and event.get("session_id") == "ses-op-1" for event in payload["history"])


def test_retire_session_keeps_implementation_opencode_session(tmp_path: Path) -> None:
    session = RuntimeSession(
        id="ses-op-2",
        runtime="opencode",
        task_count=1,
        created_at="2026-06-25T00:00:00Z",
        status="active",
        title="Full-Stack Auth: Login & Tenant Isolation",
    )

    retire_session(tmp_path, session)

    payload = load_session_state(tmp_path)
    assert payload["retired"][-1]["id"] == "ses-op-2"
    assert any(event.get("event") == "retired" and event.get("session_id") == "ses-op-2" for event in payload["history"])