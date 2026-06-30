from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from delivery.builtin_tasks import PREFINAL_AUDIT_TASK_ID
from delivery.runtime_liveness import classify_running_runtime, wrapper_should_interrupt
from delivery.task import Task


def test_classify_running_runtime_flags_dead_recorded_processes(tmp_path: Path) -> None:
    task = Task("T002", "Feature", "active", [], [], [], [], [])

    decision = classify_running_runtime(
        tmp_path,
        task,
        {
            "status": "running",
            "started_at": "2026-06-28T00:00:00Z",
            "runtime_pid": 99999999,
            "wrapper_pid": 99999998,
        },
        now=dt.datetime(2026, 6, 28, 0, 1, tzinfo=dt.timezone.utc),
    )

    assert decision.suspected is True
    assert decision.kind == "orphaned_runtime"


def test_classify_running_runtime_accepts_recent_relevant_file_progress(tmp_path: Path) -> None:
    source = tmp_path / "backend" / "src" / "feature.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('ok')\n", encoding="utf-8")
    mtime = dt.datetime(2026, 6, 28, 0, 15, tzinfo=dt.timezone.utc).timestamp()
    os.utime(source, (mtime, mtime))
    task = Task("T002", "Feature", "active", [], [], [], [], ["backend/src/feature.py"])

    decision = classify_running_runtime(
        tmp_path,
        task,
        {
            "status": "running",
            "started_at": "2026-06-28T00:00:00Z",
            "runtime_pid": os.getpid(),
            "last_tool_at": "2026-06-28T00:01:00Z",
        },
        now=dt.datetime(2026, 6, 28, 0, 16, tzinfo=dt.timezone.utc),
    )

    assert decision.suspected is False
    assert decision.kind == "running"


def test_wrapper_should_interrupt_after_hard_stall_without_activity(tmp_path: Path) -> None:
    decision = wrapper_should_interrupt(
        tmp_path,
        "T002",
        {"read_only_streak": 0, "last_tool_at": "", "last_mutation_at": ""},
        started_monotonic=0.0,
        now_monotonic=901.0,
        threshold_seconds=900,
    )

    assert decision.suspected is True
    assert decision.kind == "silent_stall"


def test_wrapper_uses_longer_hard_stall_for_audit_tasks(tmp_path: Path) -> None:
    early = wrapper_should_interrupt(
        tmp_path,
        PREFINAL_AUDIT_TASK_ID,
        {"read_only_streak": 0, "last_tool_at": "", "last_mutation_at": ""},
        started_monotonic=0.0,
        now_monotonic=901.0,
    )
    late = wrapper_should_interrupt(
        tmp_path,
        PREFINAL_AUDIT_TASK_ID,
        {"read_only_streak": 0, "last_tool_at": "", "last_mutation_at": ""},
        started_monotonic=0.0,
        now_monotonic=1501.0,
    )

    assert early.suspected is False
    assert late.suspected is True
    assert late.kind == "silent_stall"
