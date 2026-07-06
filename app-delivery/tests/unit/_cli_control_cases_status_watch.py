from __future__ import annotations

import argparse
import json
import os
from importlib import import_module
from pathlib import Path

import pytest

from delivery.state import save_work_items, write_active_task_record


cli = import_module("delivery.__main__")


def test_cmd_watch_status_emits_task_level_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-07-05T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T007", "title": "Implement auth", "status": "active", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
            ],
        },
    )
    active_record_path = tmp_path / ".app-delivery-runtime" / "active-tasks" / "opencode-T007-s1.json"
    write_active_task_record(
        active_record_path,
        {
            "pid": os.getpid(),
            "task_id": "T007",
            "task_title": "Implement auth",
            "phase": "implementation",
        },
    )

    step = {"count": 0}

    def _fake_sleep(seconds: int) -> None:
        step["count"] += 1
        if step["count"] == 1:
            save_work_items(
                tmp_path,
                {
                    "schema_version": "2",
                    "project": tmp_path.name,
                    "generated_at": "2026-07-05T00:00:00Z",
                    "last_updated_commit": "",
                    "items": [
                        {"id": "T007", "title": "Implement auth", "status": "review_pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                    ],
                },
            )
            active_record_path.unlink()
            (tmp_path / ".app-delivery-runtime" / "host-handoff.json").write_text(
                json.dumps({"status": "waiting_for_host", "skill": "code-review", "task_id": "T007"}),
                encoding="utf-8",
            )
        elif step["count"] == 2:
            save_work_items(
                tmp_path,
                {
                    "schema_version": "2",
                    "project": tmp_path.name,
                    "generated_at": "2026-07-05T00:00:00Z",
                    "last_updated_commit": "",
                    "items": [
                        {"id": "T007", "title": "Implement auth", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                    ],
                },
            )
            (tmp_path / ".app-delivery-runtime" / "host-handoff.json").unlink()

    monkeypatch.setattr(cli.time, "sleep", _fake_sleep)

    result = cli.cmd_watch_status(argparse.Namespace(project=str(tmp_path), interval_seconds=5, max_failures=3))

    assert result == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3
    events = [json.loads(line.split(" ", 1)[1]) for line in lines]
    assert events[0]["reason"] == "initial"
    assert events[0]["task_kind"] == "active"
    assert events[0]["task_id"] == "T007"
    assert events[1]["reason"] == "changed"
    assert events[1]["task_kind"] == "review_pending"
    assert events[1]["host_skill"] == "code-review"
    assert events[2]["reason"] == "changed"
    assert events[2]["control_status"] == "complete"
    assert events[2]["task_kind"] is None


def test_cmd_watch_status_emits_exception_task_id_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-07-05T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "Feature", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
            ],
        },
    )

    step = {"count": 0}

    def _fake_sleep(seconds: int) -> None:
        step["count"] += 1
        if step["count"] == 1:
            save_work_items(
                tmp_path,
                {
                    "schema_version": "2",
                    "project": tmp_path.name,
                    "generated_at": "2026-07-05T00:00:00Z",
                    "last_updated_commit": "",
                    "items": [
                        {"id": "T001", "title": "Feature", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                        {"id": "T011", "title": "Approval", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": [], "review_artifact": "docs/reviews/exception-report-T011.md"},
                    ],
                },
            )
        elif step["count"] == 2:
            save_work_items(
                tmp_path,
                {
                    "schema_version": "2",
                    "project": tmp_path.name,
                    "generated_at": "2026-07-05T00:00:00Z",
                    "last_updated_commit": "",
                    "items": [
                        {"id": "T001", "title": "Feature", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                        {"id": "T011", "title": "Approval", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": [], "review_artifact": "docs/reviews/exception-report-T011.md"},
                    ],
                },
            )

    monkeypatch.setattr(cli.time, "sleep", _fake_sleep)

    result = cli.cmd_watch_status(argparse.Namespace(project=str(tmp_path), interval_seconds=5, max_failures=3))

    assert result == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line.split(" ", 1)[1]) for line in lines]
    assert events[1]["control_status"] == "blocked"
    assert events[1]["exception_task_ids"] == ["T011"]
    assert events[2]["control_status"] == "complete"
    assert events[2]["exception_task_ids"] == []


def test_cmd_watch_status_exits_after_repeated_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "all_tasks", lambda project_root: (_ for _ in ()).throw(RuntimeError("status unavailable")))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    result = cli.cmd_watch_status(argparse.Namespace(project=str(tmp_path), interval_seconds=5, max_failures=2))

    assert result == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0].split(" ", 1)[1])
    assert event["reason"] == "watch_error"
    assert event["control_status"] == "watch_error"
    assert "status unavailable" in event["error"]
