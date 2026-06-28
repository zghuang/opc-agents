from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from delivery.state import save_work_items


def _run_cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "delivery", *args],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_status_bootstraps_empty_project(tmp_path: Path) -> None:
    result = _run_cli(Path(__file__).resolve().parents[2], "status", "--project", str(tmp_path))
    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["counts"] == {}
    assert payload["next_task"] is None


def test_control_status_bootstraps_empty_project(tmp_path: Path) -> None:
    result = _run_cli(Path(__file__).resolve().parents[2], "control", "--goal", "status", "--project", str(tmp_path))
    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["counts"] == {}
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"] is not None


def test_scaffold_marks_t000_verified(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    result = _run_cli(repo_root, "scaffold", "--project", str(tmp_path))
    assert result.returncode == 0, result.stdout
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    assert payload["items"][0]["git_commit"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert head.returncode == 0, head.stdout
    assert head.stdout.strip() == payload["items"][0]["git_commit"]


def test_pause_resume_and_fix_commands_are_deterministic(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "blocked", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "blocked_reason": "failed", "attempts": 2},
            ],
        },
    )
    pause = _run_cli(repo_root, "pause", "--project", str(tmp_path))
    assert pause.returncode == 0, pause.stdout
    assert (tmp_path / ".app-delivery-pause").exists()
    resume = _run_cli(repo_root, "resume", "--project", str(tmp_path), "--no-run")
    assert resume.returncode == 0, resume.stdout
    assert not (tmp_path / ".app-delivery-pause").exists()
    fix = _run_cli(repo_root, "fix", "--project", str(tmp_path), "--task-id", "T002", "--no-start")
    assert fix.returncode == 0, fix.stdout
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    fixed = next(item for item in payload["items"] if item["id"] == "T002")
    assert fixed["status"] == "pending"
    assert fixed["attempts"] == 0
