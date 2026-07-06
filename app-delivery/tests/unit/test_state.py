from __future__ import annotations

import json
import os
import importlib.util
import time
from pathlib import Path

from delivery.state import (
    acquire_lock,
    active_task_record_path,
    clear_task_runtime_failure,
    latest_task_log_event,
    lock_file_is_locked,
    load_active_task_records,
    load_all_task_runtime_states,
    load_task_runtime_state,
    load_architecture_meta,
    load_stale_active_task_records,
    prune_stale_active_task_records,
    read_lock_metadata,
    remember_task_runtime_failure,
    repeated_task_runtime_failure_block,
    save_task_runtime_state,
    load_test_plan,
    load_test_results,
    load_work_items,
    normalize_task_runtime_state,
    save_architecture_meta,
    save_test_plan,
    save_test_results,
    save_work_items,
    write_active_task_record,
)


def test_work_items_round_trip(tmp_path: Path) -> None:
    payload = {
        "schema_version": "2",
        "project": "demo",
        "generated_at": "2026-06-24T00:00:00Z",
        "last_updated_commit": "abc",
        "items": [{"id": "T000", "title": "脚手架", "status": "pending"}],
    }
    save_work_items(tmp_path, payload)
    loaded = load_work_items(tmp_path)
    assert loaded["project"] == "demo"
    assert loaded["items"][0]["id"] == "T000"
    assert (tmp_path / "docs" / "work-items.md").exists()
    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "| ID | Title | Status | Depends | Sessions | Requirements | Commit |" in work_items_md


def test_work_items_markdown_compacts_long_requirement_lists(tmp_path: Path) -> None:
    payload = {
        "schema_version": "2",
        "project": "demo",
        "generated_at": "2026-06-24T00:00:00Z",
        "last_updated_commit": "abc",
        "items": [
            {
                "id": "T100",
                "title": "Long reqs",
                "status": "pending",
                "requirements": [f"REQ-{idx:03d}" for idx in range(1, 11)],
                "dependencies": [],
            }
        ],
    }
    save_work_items(tmp_path, payload)
    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "REQ-001, REQ-002, REQ-003, REQ-004, REQ-005, ... See docs/work-items.json for details" in work_items_md


def test_work_items_markdown_compacts_long_dependency_lists(tmp_path: Path) -> None:
    payload = {
        "schema_version": "2",
        "project": "demo",
        "generated_at": "2026-06-24T00:00:00Z",
        "last_updated_commit": "abc",
        "items": [
            {
                "id": "T100",
                "title": "Long deps",
                "status": "pending",
                "requirements": [],
                "dependencies": [f"T{idx:03d}" for idx in range(1, 11)],
            }
        ],
    }
    save_work_items(tmp_path, payload)
    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "T001, T002, T003, T004, T005, ... See docs/work-items.json for details" in work_items_md


def test_work_items_markdown_compacts_session_history(tmp_path: Path) -> None:
    payload = {
        "schema_version": "2",
        "project": "demo",
        "generated_at": "2026-06-24T00:00:00Z",
        "last_updated_commit": "abc",
        "items": [
            {
                "id": "T100",
                "title": "Many sessions",
                "status": "verified",
                "status_session_id": "session-4",
                "session_ids": ["session-1", "session-2", "session-3", "session-4"],
                "requirements": [],
                "dependencies": [],
            }
        ],
    }
    save_work_items(tmp_path, payload)
    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "session-1, ... session-4 (4 total; see docs/work-items.json)" in work_items_md


def test_work_items_markdown_overlays_active_validation_subtask(tmp_path: Path) -> None:
    record_path = active_task_record_path(tmp_path, runtime="validation", task_id="T-FINAL:frontend-browser-qa", session_id="test")
    write_active_task_record(
        record_path,
        {
            "pid": os.getpid(),
            "runtime": "validation",
            "task_id": "T-FINAL:frontend-browser-qa",
            "task_title": "T-FINAL:frontend-browser-qa",
            "phase": "verification",
        },
    )
    payload = {
        "schema_version": "2",
        "project": "demo",
        "generated_at": "2026-06-24T00:00:00Z",
        "items": [
            {
                "id": "T-FINAL",
                "title": "Final verification",
                "status": "blocked",
                "requirements": [],
                "dependencies": [],
            }
        ],
    }

    save_work_items(tmp_path, payload)

    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "| T-FINAL | Final verification | active (verification: frontend-browser-qa) |" in work_items_md


def test_test_results_round_trip(tmp_path: Path) -> None:
    payload = {
        "schema_version": "1",
        "project": "demo",
        "generated_at": "2026-06-24T00:00:00Z",
        "results": [{"task_id": "T001", "passed": True}],
        "full_suite_results": {"passed": True, "scores": {}},
    }
    save_test_results(tmp_path, payload)
    loaded = load_test_results(tmp_path)
    assert loaded["results"][0]["task_id"] == "T001"


def test_test_plan_round_trip(tmp_path: Path) -> None:
    payload = {
        "schema_version": "1",
        "generated_at": "2026-06-24T00:00:00Z",
        "coverage": [{"requirement_id": "REQ-001", "test_types": ["api"], "suite": "tests/auth/"}],
    }
    save_test_plan(tmp_path, payload)
    loaded = load_test_plan(tmp_path)
    assert loaded["coverage"][0]["requirement_id"] == "REQ-001"


def test_architecture_meta_round_trip(tmp_path: Path) -> None:
    payload = {
        "schema_version": "1",
        "generated_at": "2026-06-24T00:00:00Z",
        "ui_required": True,
    }
    save_architecture_meta(tmp_path, payload)
    loaded = load_architecture_meta(tmp_path)
    assert loaded["ui_required"] is True


def test_acquire_lock_creates_runtime_dir(tmp_path: Path) -> None:
    with acquire_lock(tmp_path):
        assert (tmp_path / ".app-delivery-runtime" / "locks").exists()


def test_acquire_lock_writes_metadata(tmp_path: Path) -> None:
    with acquire_lock(tmp_path, name="execution") as lock_path:
        metadata = read_lock_metadata(lock_path)
        assert metadata["lock_name"] == "execution"
        assert metadata["project"] == str(tmp_path.resolve())
        assert metadata["pid"] > 0

    assert read_lock_metadata(lock_path) == {}
    assert lock_file_is_locked(lock_path) is False


def test_acquire_lock_updates_heartbeat_metadata(tmp_path: Path, monkeypatch) -> None:
    counter = {"value": 0}

    def fake_now() -> str:
        value = counter["value"]
        counter["value"] += 1
        return f"2026-06-24T00:00:{value:02d}Z"

    monkeypatch.setattr("delivery.state.utc_now_iso", fake_now)

    with acquire_lock(tmp_path, name="execution", heartbeat_interval=0.01) as lock_path:
        initial_heartbeat = read_lock_metadata(lock_path)["heartbeat_at"]
        updated_heartbeat = initial_heartbeat
        deadline = time.time() + 0.3
        while time.time() < deadline:
            updated_heartbeat = read_lock_metadata(lock_path)["heartbeat_at"]
            if updated_heartbeat != initial_heartbeat:
                break
            time.sleep(0.01)

    assert updated_heartbeat != initial_heartbeat


def test_load_active_task_records_filters_dead_pids(tmp_path: Path) -> None:
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "live.json").write_text(json.dumps({"pid": os.getpid(), "task_id": "T001"}) + "\n", encoding="utf-8")
    (active_dir / "dead.json").write_text(json.dumps({"pid": 0, "task_id": "T999"}) + "\n", encoding="utf-8")

    payloads = load_active_task_records(tmp_path)

    assert [payload["task_id"] for payload in payloads] == ["T001"]


def test_load_stale_active_task_records_returns_dead_pids(tmp_path: Path) -> None:
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "live.json").write_text(json.dumps({"pid": os.getpid(), "task_id": "T001"}) + "\n", encoding="utf-8")
    (active_dir / "dead.json").write_text(json.dumps({"pid": 0, "task_id": "T999"}) + "\n", encoding="utf-8")

    payloads = load_stale_active_task_records(tmp_path)

    assert [payload["task_id"] for payload in payloads] == ["T999"]


def test_prune_stale_active_task_records_removes_dead_entries(tmp_path: Path) -> None:
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    live_path = active_dir / "live.json"
    dead_path = active_dir / "dead.json"
    live_path.write_text(json.dumps({"pid": os.getpid(), "task_id": "T001"}) + "\n", encoding="utf-8")
    dead_path.write_text(json.dumps({"pid": 0, "task_id": "T999"}) + "\n", encoding="utf-8")

    removed = prune_stale_active_task_records(tmp_path)

    assert [payload["task_id"] for payload in removed] == ["T999"]
    assert live_path.exists()
    assert not dead_path.exists()


def test_latest_task_log_event_returns_last_json_object(tmp_path: Path) -> None:
    log_path = tmp_path / ".app-delivery-runtime" / "task-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"message":"first"}\nnot-json\n{"message":"second","level":"INFO"}\n', encoding="utf-8")

    payload = latest_task_log_event(tmp_path)

    assert payload == {"message": "second", "level": "INFO"}


def test_active_task_record_updates_preserve_existing_fields(tmp_path: Path) -> None:
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "app_delivery_wrapper_common.py"
    spec = importlib.util.spec_from_file_location("app_delivery_wrapper_common", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    record_path = tmp_path / ".app-delivery-runtime" / "active-tasks" / "T002.json"
    module.write_active_task_record(record_path, {"pid": 1, "task_id": "T002", "task_title": "Feature", "session_id": "session-1"})
    module.write_active_task_record(record_path, {"runtime_pid": 999})

    payload = json.loads(record_path.read_text(encoding="utf-8"))

    assert payload["task_id"] == "T002"
    assert payload["task_title"] == "Feature"
    assert payload["session_id"] == "session-1"
    assert payload["runtime_pid"] == 999


def test_task_runtime_state_round_trip_and_merge(tmp_path: Path) -> None:
    save_task_runtime_state(tmp_path, "T002", {"status": "running", "session_id": "session-1"})
    save_task_runtime_state(tmp_path, "T002", {"status": "completed", "exit_code": 0})

    payload = load_task_runtime_state(tmp_path, "T002")

    assert payload["task_id"] == "T002"
    assert payload["session_id"] == "session-1"
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0


def test_load_all_task_runtime_states_returns_by_task_id(tmp_path: Path) -> None:
    save_task_runtime_state(tmp_path, "T001", {"status": "completed"})
    save_task_runtime_state(tmp_path, "T002", {"status": "failed"})

    payload = load_all_task_runtime_states(tmp_path)

    assert payload["T001"]["status"] == "completed"
    assert payload["T002"]["status"] == "failed"


def test_normalize_task_runtime_state_marks_dead_running_process_interrupted() -> None:
    payload = normalize_task_runtime_state(
        {
            "task_id": "T002",
            "status": "running",
            "runtime_pid": 0,
            "wrapper_pid": 0,
        }
    )

    assert payload["status"] == "interrupted"


def test_remember_task_runtime_failure_counts_repeated_scope_violation(tmp_path: Path) -> None:
    remember_task_runtime_failure(tmp_path, "T002", kind="scope_violation", message="Task produced changes outside its declared scope.")
    remember_task_runtime_failure(tmp_path, "T002", kind="scope_violation", message="Task produced changes outside its declared scope.")

    payload = load_task_runtime_state(tmp_path, "T002")

    assert payload["failure_kind"] == "scope_violation"
    assert payload["failure_count"] == 2
    block = repeated_task_runtime_failure_block(tmp_path, "T002")
    assert block is not None
    assert block["failure_kind"] == "scope_violation"


def test_clear_task_runtime_failure_resets_failure_memory(tmp_path: Path) -> None:
    remember_task_runtime_failure(tmp_path, "T002", kind="task_failed", message="tests still failing")
    clear_task_runtime_failure(tmp_path, "T002")

    payload = load_task_runtime_state(tmp_path, "T002")

    assert payload["failure_count"] == 0
    assert payload["failure_signature"] == ""
    assert repeated_task_runtime_failure_block(tmp_path, "T002") is None
