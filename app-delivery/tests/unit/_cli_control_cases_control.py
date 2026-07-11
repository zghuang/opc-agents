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
from delivery.control_plane_host import build_planning_host_step, build_review_host_step
from delivery.runtime_config import resolve_project_root
from delivery.skill_prompts import render_skill_prompt
from delivery.stage_harness import stage_import_command, stage_input_path
from delivery.state import load_gates, load_session_state, load_task_runtime_state, save_architecture_meta, save_session_state, save_task_runtime_state, save_test_plan, save_test_results, save_work_items
from delivery.task import Task
from delivery.token_usage import append_token_usage_record


cli = import_module("delivery.__main__")


def test_cmd_start_requires_external_spec_review_from_requirements(tmp_path: Path) -> None:
    requirements = tmp_path / "req.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(tmp_path), requirements=str(requirements), runtime="claude"))

    assert exc_info.value.code == "requirements_missing"
    assert exc_info.value.details["required_skill"] == "spec-review"
    assert exc_info.value.details["requirements_path"] == str(requirements.resolve())
    assert (tmp_path / "docs" / "requirements-source.md").read_text(encoding="utf-8") == "# Raw requirements\n"

def test_spec_review_prompt_treats_source_ids_as_traceability_not_always_canonical() -> None:
    prompt = render_skill_prompt("spec-review.md", source_document="# Requirements\n\n- REQ-001: Large capability bucket\n")

    assert "Preserve a source REQ- or NFR-style ID as the canonical requirement ID only when" in prompt
    assert "source_requirement_ids" in prompt
    assert "fields, UI fragments, endpoint fragments" in prompt
    assert "Do not invent domain-specific canonical ID prefixes" in prompt


def test_cmd_spec_review_rejects_domain_specific_canonical_requirement_ids(tmp_path: Path) -> None:
    input_path = tmp_path / "spec-review.json"
    input_path.write_text(
        json.dumps(
            {
                "requirements": [{"id": "DOMAIN-001", "title": "Domain Metric", "summary": "Domain-labeled ID should not be canonical."}],
                "acceptance_scenarios": [],
                "clarifications": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_spec_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "input_invalid_shape"
    assert "REQ-###/NFR-###" in exc_info.value.message
    assert exc_info.value.details["invalid_requirement_ids"] == ["DOMAIN-001"]

def test_cmd_spec_review_archives_source_requirements_when_payload_includes_path(tmp_path: Path) -> None:
    requirements = tmp_path / "raw-req.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")
    input_path = tmp_path / "spec-review.json"
    input_path.write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
                "clarifications": [],
                "source_requirements_path": str(requirements),
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_spec_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    assert (tmp_path / "docs" / "requirements-source.md").read_text(encoding="utf-8") == "# Raw requirements\n"

def test_cmd_spec_review_preserves_blocking_clarifications(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "spec-review.json"
    input_path.write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
                "clarifications": [
                    {
                        "severity": "C1",
                        "question": "持久化存储方案未指定",
                        "rationale": "数据库选型未指定。",
                        "affected_requirement_ids": ["REQ-001"],
                        "blocking": True,
                    }
                ],
                "source_requirements_path": str(tmp_path / "raw.md"),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "raw.md").write_text("# Raw requirements\n", encoding="utf-8")

    result = cli.cmd_spec_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 2
    clarification_text = (docs_dir / "clarification-needed.md").read_text(encoding="utf-8")
    assert "blocking_count: 1" in clarification_text
    assert "## C1 - 持久化存储方案未指定" in clarification_text

def test_cmd_start_bare_project_name_uses_opc_projects_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPC_HOME", str(tmp_path))
    requirements = tmp_path / "req.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project="scms-01", requirements=str(requirements), runtime="claude"))

    expected_root = tmp_path / "projects" / "scms-01"
    assert exc_info.value.code == "requirements_missing"
    assert exc_info.value.details["project"] == str(expected_root)
    assert (expected_root / "docs" / "requirements-source.md").read_text(encoding="utf-8") == "# Raw requirements\n"

def test_cmd_status_bare_project_name_uses_opc_projects_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OPC_HOME", str(tmp_path))

    result = cli.cmd_status(argparse.Namespace(project="scms-01"))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["project"] == str(tmp_path / "projects" / "scms-01")

def test_cmd_status_writes_project_summary_and_backfills_missing_task_session_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    backend_dir = tmp_path / "backend" / "src" / "feature"
    backend_dir.mkdir(parents=True, exist_ok=True)
    backend_dir.joinpath("service.py").write_text("def feature():\n    return True\n\n", encoding="utf-8")
    backend_test_dir = tmp_path / "backend" / "tests"
    backend_test_dir.mkdir(parents=True, exist_ok=True)
    backend_test_dir.joinpath("test_feature.py").write_text("def test_feature():\n    value = 1\n    assert value == 1\n\n", encoding="utf-8")
    frontend_dir = tmp_path / "frontend" / "src"
    frontend_dir.mkdir(parents=True, exist_ok=True)
    frontend_dir.joinpath("App.tsx").write_text("export function App() {\n  return null\n}\n", encoding="utf-8")
    tmp_path.joinpath("docs", "ignored.py").write_text("print('not counted')\n", encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "status_session_id": None},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "task_id": "T002",
            "session_id": "ses-runtime-1",
            "status": "completed",
        },
    )

    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["project"] == str(tmp_path)
    work_items = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))
    assert work_items["items"][0]["status_session_id"] == "ses-runtime-1"
    summary_json = json.loads((docs_dir / "project-summary.json").read_text(encoding="utf-8"))
    assert summary_json["code_lines"]["total"] == 8
    assert summary_json["code_lines"]["application"] == 5
    assert summary_json["code_lines"]["test"] == 3
    assert summary_json["code_lines"]["backend"] == 5
    assert summary_json["code_lines"]["frontend"] == 3
    summary_md = (docs_dir / "project-summary.md").read_text(encoding="utf-8")
    assert "- Effective Code Lines: 8" in summary_md
    assert "- Application Code Lines: 5" in summary_md
    assert "- Test Code Lines: 3" in summary_md
    assert "- Code Line Formula: Effective Code Lines = Application Code Lines + Test Code Lines" in summary_md
    assert "- Backend Code Lines: 5" in summary_md
    assert "- Frontend Code Lines: 3" in summary_md
    assert "## Planning Lead" not in summary_md
    assert "## Delivery Status" not in summary_md
    assert "## Requirements" not in summary_md
    assert "## Next Task" not in summary_md
    assert "## Tests" not in summary_md
    assert "## Active Session" not in summary_md
    assert "## Active Task" not in summary_md
    assert "## Stale Runtime Records" not in summary_md
    assert "## Invalid Verified Tasks" not in summary_md
    assert "## Recent Activity" not in summary_md

def test_cmd_status_backfills_missing_task_session_id_from_token_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"], "status_session_id": None},
            ],
        },
    )
    append_token_usage_record(
        tmp_path,
        runtime="opencode",
        task_id="T002",
        task_title="Feature",
        session_id="ses-token-2",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        log_file=".app-delivery-runtime/logs/opencode-ses-token-2-turn-1.log",
    )

    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["project"] == str(tmp_path)
    work_items = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))
    assert work_items["items"][0]["status_session_id"] == "ses-token-2"
    summary_json = json.loads((docs_dir / "project-summary.json").read_text(encoding="utf-8"))
    assert summary_json["task_metrics"][0]["session_id"] == "ses-token-2"

def test_cmd_status_tolerates_project_summary_refresh_failure(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [],
        },
    )
    monkeypatch.setattr(cli, "project_summary", lambda project_root: (_ for _ in ()).throw(RuntimeError("summary boom")))

    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["project"] == str(tmp_path)

def test_cmd_control_tolerates_project_summary_refresh_failure(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        cli,
        "run_control",
        lambda project_root, *, goal, requirements_path=None, repair_task_id=None: {
            "control_status": "in_progress",
            "must_continue": False,
            "next_step": None,
        },
    )
    monkeypatch.setattr(cli, "project_summary", lambda project_root: (_ for _ in ()).throw(RuntimeError("summary boom")))
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"

def test_routed_status_explains_normal_active_task_wait(tmp_path: Path, monkeypatch) -> None:
    from delivery import control_plane

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature.py"]},
            ],
        },
    )
    monkeypatch.setattr(control_plane, "_planning_blocker_code", lambda project_root: None)
    monkeypatch.setattr(
        control_plane,
        "runtime_status",
        lambda project_root: {
            "project": str(tmp_path),
            "counts": {"active": 1},
            "active_task": {"id": "T002", "title": "Feature", "status": "active", "phase": "implementation", "runtime_state": {"status": "running"}},
            "gates": {"gates": []},
        },
    )

    payload = control_plane.routed_status(tmp_path)

    assert payload["control_status"] == "running"
    assert payload["must_continue"] is False
    assert payload["next_step"] is None
    assert payload["waiting_for"] == {
        "kind": "active_task_completion",
        "task_id": "T002",
        "phase": "implementation",
        "message": "Waiting for active task T002 completion.",
    }
    assert payload["blocking_condition"] is None

def test_cmd_start_reuses_existing_bootstrap(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    (project_root / "backend").mkdir(parents=True, exist_ok=True)
    calls: list[str] = []

    monkeypatch.setattr(cli, "cmd_scaffold", lambda args: calls.append("scaffold") or 0)
    monkeypatch.setattr(cli, "cmd_loop", lambda args: calls.append("loop") or 0)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: calls.append("watchdog") or 123)

    result = cli.cmd_start(argparse.Namespace(project=str(project_root), requirements=None, runtime="claude"))

    assert result == 0
    assert calls == ["loop"]

def test_cmd_control_status_reports_planning_next_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    requirements = tmp_path / "req.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")
    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=str(requirements),
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["planning_blocker_code"] == "requirements_missing"
    assert payload["must_continue"] is True
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"]["skill"] == "spec-review"

def test_cmd_control_status_surfaces_blocking_clarification_questions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    spec_input_path = tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json"
    spec_input_path.parent.mkdir(parents=True, exist_ok=True)
    spec_input_path.write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
                "clarifications": [
                    {
                        "severity": "C1",
                        "question": "Need storage decision",
                        "rationale": "This changes the persistence design.",
                        "affected_requirement_ids": ["REQ-001"],
                        "blocking": True,
                        "recommended_answer": "Use PostgreSQL.",
                        "answer_options": ["Use PostgreSQL", "Use SQLite"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (docs_dir / "clarification-needed.md").write_text("blocking_count: 1\n## C1 - Need storage decision\n", encoding="utf-8")

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["planning_blocker_code"] == "clarification_blocking"
    assert payload["must_continue"] is False
    assert payload["control_status"] == "blocked"
    assert payload["next_step"]["action"] == "collect_clarification_answers"
    assert payload["next_step"]["requires_user_input"] is True
    assert payload["next_step"]["clarifications"][0]["recommended_answer"] == "Use PostgreSQL."
    assert payload["next_step"]["clarifications"][0]["answer_options"] == ["Use PostgreSQL", "Use SQLite"]

def test_cmd_control_status_routes_blocked_final_verify_to_repair_task(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T003", "title": "最终修复包", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    (project_root / ".app-delivery-runtime" / "task-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
        json.dumps({"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_task_id": "T003", "repair_report_artifact": "docs/reviews/final-repair-report.md"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["must_continue"] is True
    assert payload["next_step"]["action"] == "run_resume"
    assert payload["next_step"]["task_id"] == "T003"

def test_cmd_control_status_does_not_resume_exception_final_repair_task(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T003", "title": "最终修复包", "status": "exception", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "blocked_reason": "repair budget exhausted"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    (project_root / ".app-delivery-runtime" / "task-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
        json.dumps({"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_task_id": "T003", "repair_report_artifact": "docs/reviews/final-repair-report.md"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["control_status"] == "blocked"
    assert payload["must_continue"] is False
    assert payload["next_step"] is None
    assert payload["final_verify"]["repair_task_status"] == "exception"
    assert payload["final_verify"]["will_continue"] is False

def test_status_does_not_spawn_second_gate_repair_task_when_verified_gate_repair_exists(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status
    from delivery.state import save_gates

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Validation Gate Repair Bundle (GATE-auth)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "review_status": "pass", "git_commit": "def456", "blocked_reason": "validation gate repair bundle"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": tmp_path.name,
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {"id": "GATE-auth", "kind": "module", "title": "Auth gate", "status": "blocked", "scope_tasks": ["T002"], "scope_requirements": ["REQ-001"], "required_test_types": ["browser"], "source": "task-decompose", "repair_candidates": ["T002", "T003"], "report_artifact": "docs/reviews/gate-report-GATE-auth.md"}
            ],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["counts"]["verified"] == 3
    assert payload["next_task"]["id"] == "T-FINAL"

def test_status_does_not_create_gate_repair_task_for_missing_types(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status
    from delivery.state import save_gates

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/src/tests/test_auth/test_login.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": tmp_path.name,
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {"id": "GATE-auth", "kind": "module", "title": "Auth gate", "status": "blocked", "scope_tasks": ["T002"], "scope_requirements": ["REQ-001"], "required_test_types": ["lint", "typecheck", "build", "integration"], "missing_test_types": ["lint", "typecheck", "build", "integration"], "source": "task-decompose", "repair_candidates": ["T002"], "report_artifact": "docs/reviews/gate-report-GATE-auth.md"}
            ],
        },
    )

    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert not any(item.get("task_kind") == "repair" for item in payload["items"])

    status_payload = loop_status(tmp_path)
    gate = status_payload["gates"]["gates"][0]
    assert gate["status"] == "blocked"
    assert gate["missing_test_types"] == ["lint", "typecheck", "build", "integration"]

def test_status_prunes_obsolete_pending_gate_repair_when_gates_verified(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status
    from delivery.state import save_gates

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T013", "title": "Validation Gate Repair Bundle (GATE-auth)", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "blocked_reason": "validation gate repair bundle", "attempts": 0},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": tmp_path.name,
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {"id": "GATE-auth", "kind": "module", "title": "Auth gate", "status": "verified", "scope_tasks": ["T002"], "scope_requirements": ["REQ-001"], "required_test_types": [], "source": "task-decompose", "repair_candidates": [], "report_artifact": "docs/reviews/gate-report-GATE-auth.md"}
            ],
        },
    )

    status_payload = loop_status(tmp_path)
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))

    assert status_payload["gates"]["gates"][0]["status"] == "verified"
    assert not any(item["id"] == "T013" for item in payload["items"])

def test_cmd_control_status_reruns_final_verify_after_repair_verified(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    project_root.joinpath("AGENTS.md").write_text("agents\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "review_status": "pass", "git_commit": "def456", "verified_at": "2999-06-24T00:10:00Z"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "repair task T003 completed but final verification is still failing"},
            ],
        },
    )
    save_task_runtime_state(
        project_root,
        "T-FINAL",
        {
            "repair_candidates": ["T002"],
            "repair_task_id": "T003",
            "repair_report_artifact": "docs/reviews/final-repair-report.md",
            "final_verify_status": "repair_required",
            "updated_at": "2026-06-24T00:05:00Z",
        },
    )

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"]["action"] == "run_final_verify"
    assert payload["next_step"]["owner"] == "framework"
    assert payload["next_step"]["repair_task_id"] == "T003"

def test_status_prunes_stale_pending_repair_task_when_verified_repair_with_same_title_exists(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Validation Gate Repair Bundle (GATE-auth)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T004", "title": "Validation Gate Repair Bundle (GATE-auth)", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "blocked_reason": "validation gate repair bundle"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )

    payload = loop_status(tmp_path)

    remaining_ids = [row["id"] for row in json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"]]
    assert "T004" not in remaining_ids
    assert payload["counts"]["verified"] == 3

def test_status_repoints_final_runtime_when_pruning_stale_final_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "blocked_reason": "synthesized repair bundle for final verification failures"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification created repair task T004; preserve verified tasks and repair through that bundle"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "repair_candidates": ["T002", "T003"],
            "repair_task_id": "T004",
            "final_verify_status": "repair_required",
        },
    )

    payload = loop_status(tmp_path)

    final_runtime = json.loads((tmp_path / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").read_text(encoding="utf-8"))
    remaining_ids = [row["id"] for row in json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"]]
    assert "T004" not in remaining_ids
    assert final_runtime["repair_task_id"] == "T003"
    assert payload["final_verify"]["repair_task_id"] == "T003"
    assert payload["final_verify"]["repair_task_status"] == "verified"

def test_status_repoints_stale_missing_final_repair_runtime_to_existing_final_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification created repair task T004; preserve verified tasks and repair through that bundle"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "repair_candidates": ["T002", "T003"],
            "repair_task_id": "T004",
            "final_verify_status": "repair_required",
        },
    )

    payload = loop_status(tmp_path)

    final_runtime = json.loads((tmp_path / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").read_text(encoding="utf-8"))
    assert final_runtime["repair_task_id"] == "T003"
    assert payload["final_verify"]["repair_task_id"] == "T003"
    assert payload["final_verify"]["repair_task_status"] == "verified"

def test_status_keeps_current_pending_final_repair_task_even_when_older_verified_duplicate_exists(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_reporting import status as loop_status

    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"], "blocked_reason": "synthesized repair bundle for final verification failures"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003", "T004"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification created repair task T004; preserve verified tasks and repair through that bundle"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "repair_candidates": ["T002", "T003"],
            "repair_task_id": "T004",
            "final_verify_status": "repair_required",
        },
    )

    payload = loop_status(tmp_path)

    remaining_ids = [row["id"] for row in json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"]]
    final_runtime = json.loads((tmp_path / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").read_text(encoding="utf-8"))
    assert "T004" in remaining_ids
    assert final_runtime["repair_task_id"] == "T004"
    assert payload["final_verify"]["repair_task_id"] == "T004"
    assert payload["next_task"]["id"] == "T004"

def test_cmd_control_status_prefers_review_pending_task_over_blocked_final_repair(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": None},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    (project_root / ".app-delivery-runtime" / "task-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
        json.dumps({"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_report_artifact": "docs/reviews/final-repair-report.md"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["must_continue"] is True
    assert payload["next_step"]["action"] == "run_code_review"
    assert payload["next_step"]["task_id"] == "T002"


def test_review_host_step_prompt_tells_host_to_import_artifact(tmp_path: Path) -> None:
    step = build_review_host_step(project_root=tmp_path, task_id="T002", title="Feature")

    assert "Do not run the app-delivery import command yourself" not in step["prompt"]
    assert "After writing the artifact, run this import command" in step["prompt"]
    assert '${OPC_HOME:-$HOME/opc}/bin/app-delivery code-review --project' in step["prompt"]
    assert step["import_command"] in step["prompt"]


def test_cmd_control_status_marks_ready_host_response_as_unimported(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": None},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    input_path = project_root / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "pass", "summary": "ready", "findings": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["next_step"]["task_id"] == "T002"
    assert payload["next_step"]["host_response_ready"] is True
    assert payload["next_step"]["host_response_status"] == "ready_unimported"
    assert "run control --goal auto to import" in payload["next_step"]["message"]


def test_cmd_control_status_does_not_mark_stale_host_response_ready(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": None},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    input_path = project_root / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "pass", "summary": "old", "findings": []}), encoding="utf-8")
    request_path = code_review_request_path(project_root, "T002")
    request_path.write_text("new request\n", encoding="utf-8")
    old_time = dt.datetime(2026, 6, 27, 7, 0, tzinfo=dt.timezone.utc).timestamp()
    request_time = dt.datetime(2026, 6, 27, 7, 5, tzinfo=dt.timezone.utc).timestamp()
    os.utime(input_path, (old_time, old_time))
    os.utime(request_path, (request_time, request_time))
    save_task_runtime_state(project_root, "T002", {"task_id": "T002", "completed_at": "2026-06-27T07:04:00Z"})
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["next_step"]["task_id"] == "T002"
    assert "host_response_ready" not in payload["next_step"]
    assert "ready_unimported" not in json.dumps(payload["next_step"])

def test_cmd_control_status_blocks_when_final_repair_task_is_missing(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    (project_root / ".app-delivery-runtime" / "task-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
        json.dumps({"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_report_artifact": "docs/reviews/final-repair-report.md"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["control_status"] == "blocked"
    assert payload["must_continue"] is False


def test_cmd_control_status_spawns_watchdog_for_importable_host_step(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T007.json"
    snapshot = {
        "control_status": "in_progress",
        "must_continue": True,
        "counts": {"verified": 6, "review_pending": 1, "pending": 18},
        "next_step": {
            "kind": "host_skill",
            "owner": "host",
            "action": "run_code_review",
            "skill": "code-review",
            "task_id": "T007",
            "expected_input_path": str(input_path),
        },
    }
    spawned: list[str] = []

    monkeypatch.setattr(cli, "run_control", lambda project_root, *, goal, requirements_path=None, repair_task_id=None: dict(snapshot))
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: True)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: spawned.append(str(project_root)) or 123)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert spawned == [str(tmp_path)]
    assert payload["watchdog_pid"] == 123
    assert payload["next_step"]["task_id"] == "T007"

def test_cmd_control_auto_stops_when_final_repair_task_is_missing(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    (project_root / ".app-delivery-runtime" / "task-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
        json.dumps({"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_report_artifact": "docs/reviews/final-repair-report.md"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="step",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["must_continue"] is False
    assert payload["next_step"] is None

def test_cmd_control_auto_executes_repair_task_for_blocked_final_verify(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    (project_root / ".app-delivery-runtime" / "task-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
        json.dumps({"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_report_artifact": "docs/reviews/final-repair-report.md"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)

    called: list[str] = []

    def fake_run_fix_once(project_root, task_id, runtime, no_start=False, spawn_watchdog_after=True):
        called.append(task_id)
        (Path(project_root) / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").write_text(
            json.dumps({"task_id": "T-FINAL", "repair_candidates": []}),
            encoding="utf-8",
        )
        return {"status": "repair_started", "task_id": task_id}

    monkeypatch.setattr(cli, "_run_fix_once", fake_run_fix_once)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)
    import delivery.control_plane as control_plane
    monkeypatch.setattr(control_plane, "requirements_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr(control_plane, "architecture_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "ui_required", lambda project_root: False)
    monkeypatch.setattr(control_plane, "context_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_items_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_item_contract_errors", lambda project_root: {})
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert called == []
    assert payload["control_status"] == "blocked"
    assert payload["next_step"] is None
    assert payload["must_continue"] is False



def test_cmd_control_auto_executes_recover_stalled_step(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "kind": "framework",
                "owner": "framework",
                "action": "recover_stalled",
                "task_id": "T002",
                "attention_kind": "silent_stall",
            },
        },
        {"control_status": "running", "must_continue": False, "next_step": None},
    ]
    recovered: list[str] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_recover(project_root, *, task_id, runtime, runtime_attention, spawn_watchdog_after=True):
        recovered.append(task_id)
        return {"status": "recovered", "task_id": task_id, "attention_kind": runtime_attention.get("attention_kind")}

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "_run_stalled_recovery_once", fake_recover)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert recovered == ["T002"]
    assert payload["executed_steps"][0]["action"] == "recover_stalled"
    assert payload["executed_steps"][0]["result"]["status"] == "recovered"


def test_cmd_control_auto_defers_host_review_step_to_outer_host(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "prompt": "review prompt",
            },
        },
    ]

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="step",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert len(payload["executed_steps"]) == 1
    assert payload["executed_steps"][0]["owner"] == "host"
    assert payload["executed_steps"][0]["action"] == "run_code_review"
    assert payload["executed_steps"][0]["deferred_to_host"] is True
    assert payload["next_step"]["owner"] == "host"


def test_cmd_control_auto_spawns_watchdog_for_waiting_host_step_and_exits_successfully(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "prompt": "review prompt",
                "expected_input_path": str(input_path),
            },
        },
    ]
    spawned: list[str] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: True)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: spawned.append(str(project_root)) or 123)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    assert result == 0
    assert payload["executed_steps"][0]["deferred_to_host"] is True
    assert spawned == [str(tmp_path)]
    assert payload["host_handoff"]["status"] == "waiting_for_host"
    assert payload["host_handoff"]["retry_attempts"] == 0
    assert handoff["status"] == "waiting_for_host"
    assert handoff["kind"] == ""
    assert handoff["skill"] == "code-review"
    assert handoff["task_id"] == "T002"
    assert handoff["expected_input_path"] == str(input_path)
    assert handoff["prompt"] == "review prompt"
    assert handoff["retry_attempts"] == 0
    assert handoff["next_step"]["action"] == "run_code_review"


def test_save_host_handoff_requests_one_retry_for_stale_waiting_handoff(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "HOST_HANDOFF_RETRY_AFTER_SECONDS", 0)
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    next_step = {
        "owner": "host",
        "action": "run_code_review",
        "skill": "code-review",
        "task_id": "T002",
        "expected_input_path": str(input_path),
    }

    first = cli._save_host_handoff(tmp_path, next_step)
    second = cli._save_host_handoff(tmp_path, next_step)
    third = cli._save_host_handoff(tmp_path, next_step)

    assert first["retry_attempts"] == 0
    assert second["requested_at"] == first["requested_at"]
    assert second["retry_attempts"] == 1
    assert second["retry_requested_at"]
    assert third["retry_attempts"] == 1
    assert third["retry_requested_at"] == second["retry_requested_at"]


def test_save_host_handoff_uses_new_id_for_new_review_request_round(tmp_path: Path) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    request_path = code_review_request_path(tmp_path, "T002")
    request_path.write_text("first request\n", encoding="utf-8")
    next_step = {
        "owner": "host",
        "action": "run_code_review",
        "skill": "code-review",
        "task_id": "T002",
        "expected_input_path": str(input_path),
    }

    first = cli._save_host_handoff(tmp_path, next_step)
    old_time = dt.datetime(2026, 6, 24, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    new_time = dt.datetime(2026, 6, 24, 0, 5, tzinfo=dt.timezone.utc).timestamp()
    os.utime(request_path, (old_time, old_time))
    first = cli._save_host_handoff(tmp_path, next_step)
    os.utime(request_path, (new_time, new_time))
    second = cli._save_host_handoff(tmp_path, next_step)

    assert first["handoff_id"] != second["handoff_id"]
    assert second["retry_attempts"] == 0


def test_cmd_control_auto_persists_host_handoff_even_without_watchdog(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "prompt": "review prompt",
                "expected_input_path": str(input_path),
            },
        },
    ]

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="step",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    assert result == 1
    assert payload["executed_steps"][0]["deferred_to_host"] is True
    assert handoff["status"] == "waiting_for_host"
    assert handoff["skill"] == "code-review"
    assert handoff["task_id"] == "T002"
    assert handoff["expected_input_path"] == str(input_path)
    assert handoff["prompt"] == "review prompt"


def test_cmd_control_auto_does_not_persist_host_notice_as_handoff(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
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

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    spawned: list[str] = []
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: True)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: spawned.append(str(project_root)) or 123)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    watchdog_state = json.loads((tmp_path / ".app-delivery-runtime" / "watchdog-state.json").read_text(encoding="utf-8"))
    assert result == 1
    assert payload["executed_steps"][0]["deferred_to_host"] is True
    assert payload["host_handoff"]["status"] == "notice"
    assert handoff["status"] == "notice"
    assert handoff["action"] == "review_gate_blocker"
    assert handoff["skill"] == ""
    assert handoff["expected_input_path"] == ""
    assert handoff["message"] == "Review gate report and decide whether to repair."
    assert watchdog_state["pid"] == 0
    assert watchdog_state["status"] == "host_notice"
    assert watchdog_state["last_action"] == "review_gate_blocker"
    assert spawned == []


def test_cmd_control_step_executes_one_framework_step(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {"owner": "framework", "action": "run_scaffold"},
        },
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {"owner": "framework", "action": "run_resume"},
        },
    ]
    calls: list[str] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_execute(step, args):
        calls.append(str(step.get("action") or ""))
        return {"status": "ok"}

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "_execute_framework_control_step", fake_execute)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="step",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert calls == ["run_scaffold"]
    assert len(payload["executed_steps"]) == 1
    assert payload["executed_steps"][0]["action"] == "run_scaffold"
    assert payload["next_step"]["action"] == "run_resume"


def test_cmd_control_auto_repairs_exception_blocking_all_pending_tasks_once(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    from delivery.state import save_work_items

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "Foundation", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/shared.py"], "attempts": 3},
                {"id": "T002", "title": "Feature A", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": [], "output_paths": ["backend/a.py"]},
                {"id": "T003", "title": "Feature B", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["backend/b.py"]},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T003"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    calls: list[str] = []

    def fake_run_fix_once(project_root, task_id, runtime, no_start=False, spawn_watchdog_after=True):
        from delivery.task import all_tasks, reset_task, save_tasks

        calls.append(task_id)
        save_tasks(project_root, reset_task(all_tasks(project_root), task_id))
        return {"status": "reset", "task_id": task_id, "no_start": no_start}

    monkeypatch.setattr(cli, "_run_fix_once", fake_run_fix_once)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)
    import delivery.control_plane as control_plane
    monkeypatch.setattr(control_plane, "requirements_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr(control_plane, "architecture_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "ui_required", lambda project_root: False)
    monkeypatch.setattr(control_plane, "context_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_items_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_item_contract_errors", lambda project_root: {})

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="step",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    state = json.loads((tmp_path / ".app-delivery-runtime" / "auto-exception-repair.json").read_text(encoding="utf-8"))
    assert result == 0
    assert calls == ["T001"]
    assert payload["executed_steps"][0]["action"] == "auto_repair_exception"
    assert payload["executed_steps"][0]["result"] == {"status": "reset", "task_id": "T001", "no_start": True}
    assert "T001" in state["attempts"]

    from delivery.task import all_tasks, mark_task, save_tasks
    save_tasks(tmp_path, mark_task(all_tasks(tmp_path), "T001", "exception", attempts=3))

    payload_again = control_plane.routed_status(tmp_path)
    assert calls == ["T001"]
    assert payload_again["control_status"] == "blocked"
    assert payload_again["next_step"] is None

def test_cmd_control_auto_imports_ready_host_review_artifact(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "pass", "summary": "ok", "findings": []}), encoding="utf-8")

    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "expected_input_path": str(input_path),
            },
        },
        {
            "control_status": "in_progress",
            "must_continue": False,
            "next_step": None,
        },
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_import_task_review(project_root, task_id, payload, input_path_arg):
        imported.append((task_id, dict(payload), input_path_arg))
        return 0

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "import_task_review", fake_import_task_review)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert imported == [("T002", {"status": "pass", "summary": "ok", "findings": []}, input_path.resolve())]
    assert payload["executed_steps"][0]["owner"] == "host"
    assert payload["executed_steps"][0]["action"] == "run_code_review"
    assert payload["executed_steps"][0]["result"]["status"] == "imported"
    assert payload["executed_steps"][0]["result"]["task_id"] == "T002"

def test_cmd_control_auto_executes_host_skill_and_imports_generated_artifact(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "kind": "host_skill",
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "prompt": "review prompt",
                "expected_input_path": str(input_path),
            },
        },
        {
            "control_status": "in_progress",
            "must_continue": False,
            "next_step": None,
        },
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []
    executed: list[str] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_import_task_review(project_root, task_id, payload, input_path_arg):
        imported.append((task_id, dict(payload), input_path_arg))
        return 0

    def fake_run_host_skill(step, project_root):
        executed.append(str(step["task_id"]))
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(json.dumps({"status": "pass", "summary": "ok", "findings": []}), encoding="utf-8")
        return {"status": "executed", "skill": "code-review"}

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "import_task_review", fake_import_task_review)
    monkeypatch.setattr(cli, "_run_host_skill_for_control_step", fake_run_host_skill)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert executed == ["T002"]
    assert imported == [("T002", {"status": "pass", "summary": "ok", "findings": []}, input_path.resolve())]
    assert payload["executed_steps"][0]["owner"] == "host"
    assert payload["executed_steps"][0]["action"] == "run_code_review"
    assert payload["executed_steps"][0]["result"]["status"] == "imported"
    assert payload["executed_steps"][0]["result"]["host_execution"]["status"] == "executed"
    handoff = json.loads((tmp_path / ".app-delivery-runtime" / "host-handoff.json").read_text(encoding="utf-8"))
    assert handoff["status"] == "imported"
    assert handoff["skill"] == "code-review"
    assert handoff["task_id"] == "T002"
    assert handoff["input_path"] == str(input_path.resolve())

def test_cmd_control_auto_falls_back_to_existing_review_input_when_host_skill_fails(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "kind": "host_skill",
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "prompt": "review prompt",
                "expected_input_path": str(input_path),
            },
        },
        {"control_status": "in_progress", "must_continue": False, "next_step": None},
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_import_task_review(project_root, task_id, payload, input_path_arg):
        imported.append((task_id, dict(payload), input_path_arg))
        return 0

    def fake_run_host_skill(step, project_root):
        input_path.write_text(json.dumps({"status": "pass", "summary": "ok", "findings": []}), encoding="utf-8")
        raise DeliveryError(code="host_skill_execution_failed", message="Hermes unavailable", exit_code=2)

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "import_task_review", fake_import_task_review)
    monkeypatch.setattr(cli, "_run_host_skill_for_control_step", fake_run_host_skill)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert imported == [("T002", {"status": "pass", "summary": "ok", "findings": []}, input_path.resolve())]
    assert payload["executed_steps"][0]["result"]["host_execution"]["status"] == "failed_but_existing_artifact_imported"

def test_cmd_control_auto_defers_stale_host_review_input_until_new_review_arrives(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "changes_requested", "summary": "old", "findings": [{"severity": "blocking", "message": "stale", "requirement_ids": [], "acceptance_ids": []}]}), encoding="utf-8")
    request_path = code_review_request_path(tmp_path, "T002")
    request_path.write_text("fresh review request\n", encoding="utf-8")

    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "expected_input_path": str(input_path),
            },
        },
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_import_task_review(project_root, task_id, payload, input_path_arg):
        imported.append((task_id, dict(payload), input_path_arg))
        return 0

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "import_task_review", fake_import_task_review)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert imported == []
    assert payload["executed_steps"][0]["owner"] == "host"
    assert payload["executed_steps"][0]["action"] == "run_code_review"
    assert payload["executed_steps"][0]["deferred_to_host"] is True

def test_cmd_control_auto_defers_review_input_older_than_latest_runtime_completion_even_if_newer_than_request(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "changes_requested", "summary": "old", "findings": []}), encoding="utf-8")
    request_path = code_review_request_path(tmp_path, "T002")
    request_path.write_text("same review request\n", encoding="utf-8")
    os.utime(request_path, (100.0, 100.0))
    os.utime(input_path, (200.0, 200.0))
    save_task_runtime_state(tmp_path, "T002", {"task_id": "T002", "completed_at": "1970-01-01T00:05:00Z"})

    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "expected_input_path": str(input_path),
            },
        },
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []

    monkeypatch.setattr(cli, "run_control", lambda project_root, *, goal, requirements_path=None, repair_task_id=None: dict(snapshots.pop(0)))
    monkeypatch.setattr(cli, "import_task_review", lambda project_root, task_id, payload, input_path_arg: imported.append((task_id, dict(payload), input_path_arg)) or 0)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert imported == []
    assert payload["executed_steps"][0]["deferred_to_host"] is True

def test_cmd_control_auto_imports_review_when_input_is_newer_than_latest_runtime_completion_even_if_request_mtime_is_newer(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "pass", "summary": "ok", "findings": []}), encoding="utf-8")
    request_path = code_review_request_path(tmp_path, "T002")
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("fresh review request\n", encoding="utf-8")
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "task_id": "T002",
            "completed_at": "2026-06-27T07:00:00Z",
        },
    )

    original_stat = Path.stat

    class FakeStat:
        def __init__(self, result, *, mtime: float | None = None):
            self.st_mode = result.st_mode
            self.st_ino = result.st_ino
            self.st_dev = result.st_dev
            self.st_nlink = result.st_nlink
            self.st_uid = result.st_uid
            self.st_gid = result.st_gid
            self.st_size = result.st_size
            self.st_atime = result.st_atime
            self.st_mtime = mtime if mtime is not None else result.st_mtime
            self.st_mtime_ns = int(self.st_mtime * 1_000_000_000)
            self.st_ctime = result.st_ctime

    def fake_stat(self: Path):
        result = original_stat(self)
        if self == input_path:
            return FakeStat(result, mtime=dt.datetime(2026, 6, 27, 7, 5, tzinfo=dt.timezone.utc).timestamp())
        if self == request_path:
            return FakeStat(result, mtime=dt.datetime(2026, 6, 27, 7, 6, tzinfo=dt.timezone.utc).timestamp())
        return result

    snapshots = [
        {
            "control_status": "in_progress",
            "must_continue": True,
            "next_step": {
                "owner": "host",
                "action": "run_code_review",
                "skill": "code-review",
                "task_id": "T002",
                "expected_input_path": str(input_path),
            },
        },
        {
            "control_status": "in_progress",
            "must_continue": False,
            "next_step": None,
        },
    ]
    imported: list[tuple[str, dict[str, object], Path]] = []

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    def fake_import_task_review(project_root, task_id, payload, input_path_arg):
        imported.append((task_id, dict(payload), input_path_arg))
        return 0

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "import_task_review", fake_import_task_review)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)
    monkeypatch.setattr(Path, "stat", fake_stat)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert imported == [("T002", {"status": "pass", "summary": "ok", "findings": []}, input_path.resolve())]
    assert payload["executed_steps"][0]["result"]["status"] == "imported"

def test_cmd_control_status_routes_blocked_validation_gate_to_repair_before_final_verify(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (project_root / "backend" / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "backend" / "src" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    (docs_dir / "reviews").mkdir(parents=True, exist_ok=True)
    (docs_dir / "reviews" / "code-review-T000.md").write_text("status: pass\nreview_type: code\nwork_item: T000\n", encoding="utf-8")
    (docs_dir / "reviews" / "code-review-T002.md").write_text("status: pass\nreview_type: code\nwork_item: T002\n", encoding="utf-8")
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    ensure_git_repo(project_root)
    git(["add", "--", "."], cwd=project_root)
    git(["commit", "-m", "feat(T002): seed evidence"], cwd=project_root)
    head_sha = git_head_sha(project_root)
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "git_commit": head_sha, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T000.md", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "git_commit": head_sha, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T002.md", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr(
        "delivery.control_plane.runtime_status",
        lambda project_root: {
            "project": str(project_root),
            "counts": {"verified": 2, "pending": 1},
            "next_task": {
                "id": "T-FINAL",
                "title": "最终验证",
                "status": "pending",
                "task_kind": "feature",
                "dependencies": ["T000", "T002"],
                "requirements": [],
                "acceptance_scenarios": [],
                "output_tests": [],
                "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"],
            },
            "review_pending_task": None,
            "active_task": None,
            "stale_active_tasks": [],
            "paused": False,
            "final_verify_ready": False,
            "invalid_verified_tasks": {},
            "requirements_source_archived": True,
            "gates": {
                "schema_version": "1",
                "project": project_root.name,
                "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
                "gates": [
                    {
                        "id": "GATE-auth",
                        "kind": "module",
                        "title": "Auth gate",
                        "status": "blocked",
                        "scope_tasks": ["T002"],
                        "scope_requirements": ["REQ-001"],
                        "required_test_types": ["browser"],
                        "source": "task-decompose",
                        "repair_candidates": ["T002"],
                        "report_artifact": "docs/reviews/gate-report-GATE-auth.md",
                    }
                ],
            },
        },
    )

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"]["action"] == "review_gate_blocker"
    assert payload["next_step"]["owner"] == "host"
    assert payload["next_step"]["repair_candidates"] == ["T002"]

def test_cmd_control_status_auto_repairs_blocked_gate_exception_candidate_once(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T018", "title": "Gate scope task", "status": "exception", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["frontend/src/feature.tsx"], "attempts": 3},
                {"id": "T019", "title": "Blocked downstream task", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T018"], "output_tests": [], "output_paths": ["frontend/src/downstream.tsx"]},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T019"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    import delivery.control_plane as control_plane

    monkeypatch.setattr(control_plane, "requirements_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr(control_plane, "architecture_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "ui_required", lambda project_root: False)
    monkeypatch.setattr(control_plane, "context_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_items_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_item_contract_errors", lambda project_root: {})
    monkeypatch.setattr(control_plane, "needs_scaffold", lambda project_root: False)
    monkeypatch.setattr(
        control_plane,
        "runtime_status",
        lambda project_root: {
            "project": str(project_root),
            "counts": {"verified": 1, "exception": 1, "pending": 2},
            "next_task": {"id": "T-FINAL", "title": "Final", "status": "pending", "task_kind": "feature"},
            "review_pending_task": None,
            "active_task": None,
            "stale_active_tasks": [],
            "paused": False,
            "final_verify_ready": False,
            "invalid_verified_tasks": {},
            "requirements_source_archived": True,
            "gates": {
                "schema_version": "1",
                "project": project_root.name,
                "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
                "gates": [
                    {
                        "id": "GATE-frontend",
                        "kind": "module",
                        "title": "Frontend gate",
                        "status": "blocked",
                        "scope_tasks": ["T018"],
                        "scope_requirements": ["REQ-001"],
                        "required_test_types": ["browser"],
                        "source": "task-decompose",
                        "repair_candidates": ["T018", "T019"],
                        "blocked_reason": "scope tasks are not verified: T018",
                        "report_artifact": "docs/reviews/gate-report-GATE-frontend.md",
                    }
                ],
            },
        },
    )

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"]["action"] == "auto_repair_exception"
    assert payload["next_step"]["owner"] == "framework"
    assert payload["next_step"]["task_id"] == "T018"
    assert payload["next_step"]["gate_id"] == "GATE-frontend"
    assert payload["next_step"]["repair_candidates"] == ["T018", "T019"]

    control_plane.mark_auto_exception_repair_attempted(project_root, "T018")
    payload_again = control_plane.routed_status(project_root)
    assert payload_again["control_status"] == "in_progress"
    assert payload_again["next_step"]["action"] == "review_gate_blocker"
    assert payload_again["next_step"]["owner"] == "host"
    assert payload_again["next_step"]["task_id"] == "T018"


def test_cmd_control_status_auto_repairs_exception_blocking_gate_even_when_not_repair_candidate(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T004", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": [], "review_status": "pass"},
                {"id": "T-FRONTEND-API-AUDIT", "title": "Frontend audit", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T004"], "output_tests": [], "output_paths": [], "attempts": 3},
                {"id": "T900", "title": "Production gate", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T-FRONTEND-API-AUDIT"], "output_tests": [], "output_paths": []},
                {"id": "T901", "title": "Security gate", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T900"], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T901"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    import delivery.control_plane as control_plane

    monkeypatch.setattr(control_plane, "requirements_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr(control_plane, "architecture_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "ui_required", lambda project_root: False)
    monkeypatch.setattr(control_plane, "context_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_items_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_item_contract_errors", lambda project_root: {})
    monkeypatch.setattr(control_plane, "needs_scaffold", lambda project_root: False)
    monkeypatch.setattr(
        control_plane,
        "runtime_status",
        lambda project_root: {
            "project": str(project_root),
            "counts": {"verified": 1, "exception": 1, "pending": 3},
            "next_task": None,
            "review_pending_task": None,
            "active_task": None,
            "stale_active_tasks": [],
            "paused": False,
            "final_verify_ready": False,
            "invalid_verified_tasks": {},
            "requirements_source_archived": True,
            "gates": {
                "schema_version": "1",
                "project": project_root.name,
                "complexity": {},
                "gates": [
                    {
                        "id": "GATE-frontend",
                        "status": "blocked",
                        "repair_candidates": ["T004", "T900"],
                        "blocked_reason": "missing required test types: accessibility",
                        "report_artifact": "docs/reviews/gate-report-GATE-frontend.md",
                    }
                ],
            },
        },
    )

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["next_step"]["action"] == "auto_repair_exception"
    assert payload["next_step"]["task_id"] == "T-FRONTEND-API-AUDIT"
    assert payload["next_step"]["gate_id"] == "GATE-frontend"
    assert payload["next_step"]["blocked_pending_task_ids"] == ["T900", "T901"]


def test_cmd_control_status_auto_repairs_one_of_multiple_exception_blockers(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "Foundation", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T008", "title": "Agent graph", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": [], "output_paths": []},
                {"id": "T011", "title": "Control tower", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": [], "output_paths": []},
                {"id": "T009", "title": "Plan flow", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T008"], "output_tests": [], "output_paths": []},
                {"id": "T012", "title": "RCA", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T008"], "output_tests": [], "output_paths": []},
                {"id": "T900", "title": "Prod gate", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T009", "T011"], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T900"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    import delivery.control_plane as control_plane

    monkeypatch.setattr(control_plane, "requirements_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr(control_plane, "architecture_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "ui_required", lambda project_root: False)
    monkeypatch.setattr(control_plane, "context_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_items_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_item_contract_errors", lambda project_root: {})
    monkeypatch.setattr(control_plane, "needs_scaffold", lambda project_root: False)
    monkeypatch.setattr(
        control_plane,
        "runtime_status",
        lambda project_root: {
            "project": str(project_root),
            "counts": {"verified": 1, "exception": 2, "pending": 4},
            "next_task": None,
            "review_pending_task": None,
            "active_task": None,
            "stale_active_tasks": [],
            "paused": False,
            "final_verify_ready": False,
            "invalid_verified_tasks": {},
            "requirements_source_archived": True,
            "gates": {"schema_version": "1", "project": tmp_path.name, "complexity": {}, "gates": []},
        },
    )

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["next_step"]["action"] == "auto_repair_exception"
    assert payload["next_step"]["task_id"] == "T008"
    assert payload["next_step"]["blocked_pending_task_ids"] == ["T009", "T012", "T900"]


def test_cmd_control_status_auto_repairs_exception_when_blocked_gate_has_no_candidates(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "Foundation", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T008", "title": "Agent graph", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": [], "output_paths": []},
                {"id": "T009", "title": "Plan flow", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T008"], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T009"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    import delivery.control_plane as control_plane

    monkeypatch.setattr(control_plane, "requirements_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr(control_plane, "architecture_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "ui_required", lambda project_root: False)
    monkeypatch.setattr(control_plane, "context_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_items_ready", lambda project_root: True)
    monkeypatch.setattr(control_plane, "work_item_contract_errors", lambda project_root: {})
    monkeypatch.setattr(control_plane, "needs_scaffold", lambda project_root: False)
    monkeypatch.setattr(
        control_plane,
        "runtime_status",
        lambda project_root: {
            "project": str(project_root),
            "counts": {"verified": 1, "exception": 1, "pending": 2},
            "next_task": None,
            "review_pending_task": None,
            "active_task": None,
            "stale_active_tasks": [],
            "paused": False,
            "final_verify_ready": False,
            "invalid_verified_tasks": {},
            "requirements_source_archived": True,
            "gates": {
                "schema_version": "1",
                "project": tmp_path.name,
                "complexity": {},
                "gates": [
                    {
                        "id": "GATE-agent-decision",
                        "status": "blocked",
                        "repair_candidates": [],
                        "blocked_reason": "scope tasks are not verified: T008",
                        "report_artifact": "docs/reviews/gate-report-GATE-agent-decision.md",
                    }
                ],
            },
        },
    )

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["next_step"]["action"] == "auto_repair_exception"
    assert payload["next_step"]["task_id"] == "T008"
    assert payload["next_step"]["gate_id"] == "GATE-agent-decision"
    assert payload["next_step"]["blocked_pending_task_ids"] == ["T009"]

def test_cmd_control_status_prefers_runnable_next_task_over_blocked_future_gate(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    from delivery.state import save_gates

    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Agent Foundation", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_gates(
        project_root,
        {
            "schema_version": "1",
            "project": project_root.name,
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-agent-foundation",
                    "kind": "module",
                    "title": "Agent foundation gate",
                    "status": "blocked",
                    "scope_tasks": [],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["unit", "integration"],
                    "source": "task-decompose",
                    "observed_test_types": [],
                    "missing_test_types": ["unit", "integration"],
                    "repair_candidates": ["T002"],
                    "blocked_reason": "missing required test types: unit, integration",
                    "report_artifact": "docs/reviews/gate-report-GATE-agent-foundation.md",
                }
            ],
        },
    )
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(project_root),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"]["action"] == "run_resume"
    assert payload["next_step"]["task_id"] == "T000"

def test_cmd_control_status_prefers_review_pending_task_over_runnable_next_task(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        "delivery.control_plane.runtime_status",
        lambda project_root: {
            "project": str(project_root),
            "counts": {"verified": 1, "review_pending": 1, "pending": 1},
            "next_task": {
                "id": "T007",
                "title": "Gateway",
                "status": "pending",
                "task_kind": "feature",
                "runtime_state": {},
            },
            "review_pending_task": {
                "id": "T005",
                "title": "Domain Metric Calculation Engine & Risk Scoring",
                "status": "review_pending",
                "task_kind": "feature",
            },
            "active_task": None,
            "stale_active_tasks": [],
            "paused": False,
            "final_verify_ready": False,
            "invalid_verified_tasks": {},
            "requirements_source_archived": True,
            "gates": {"schema_version": "1", "project": "demo", "complexity": {}, "gates": []},
        },
    )
    monkeypatch.setattr("delivery.control_plane._planning_blocker_code", lambda project_root: None)
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr("delivery.control_plane.all_tasks", lambda project_root: [type("TaskRow", (), {"id": "T-FINAL", "status": "pending"})()])

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["control_status"] == "in_progress"
    assert payload["next_step"]["action"] == "run_code_review"
    assert payload["next_step"]["task_id"] == "T005"

def test_cmd_start_spawns_watchdog_only_when_project_enables_it(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"runtime": "claude", "watchdog_enabled": True}), encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    (project_root / "backend").mkdir(parents=True, exist_ok=True)
    calls: list[str] = []

    monkeypatch.setattr(cli, "cmd_loop", lambda args: calls.append("loop") or 0)
    monkeypatch.setattr(cli, "spawn_watchdog", lambda project_root: calls.append("watchdog") or 123)

    result = cli.cmd_start(argparse.Namespace(project=str(project_root), requirements=None, runtime="claude"))

    assert result == 0
    assert calls == ["loop", "watchdog"]

def test_cmd_start_requires_hermes_decompose_when_work_items_empty(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [],
        },
    )
    (project_root / "backend").mkdir(parents=True, exist_ok=True)

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(project_root), requirements=None, runtime="claude", max_auto_tasks=None))

    assert exc_info.value.code == "work_items_missing"
    assert exc_info.value.details["required_skill"] == "task-decompose"

def test_status_reports_pause_and_final_ready(tmp_path: Path) -> None:
    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "feature").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "feature" / "impl.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "reviews" / "code-review-T000.md").write_text("status: pass\nreview_type: code\nwork_item: T000\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews" / "code-review-T002.md").write_text("status: pass\nreview_type: code\nwork_item: T002\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "feat(T002): seed evidence"], cwd=tmp_path)
    head_sha = git_head_sha(tmp_path)

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "git_commit": head_sha, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T000.md", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "git_commit": head_sha, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T002.md", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/feature/impl.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    (tmp_path / ".app-delivery-pause").write_text("paused\n", encoding="utf-8")

    payload = loop_status(tmp_path)

    assert payload["paused"] is True
    assert payload["final_verify_ready"] is True
    assert payload["next_task"]["id"] == "T-FINAL"

def test_status_does_not_report_final_ready_when_final_task_is_blocked(tmp_path: Path) -> None:
    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "feature").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "feature" / "impl.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "reviews" / "code-review-T000.md").write_text("status: pass\nreview_type: code\nwork_item: T000\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews" / "code-review-T002.md").write_text("status: pass\nreview_type: code\nwork_item: T002\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "feat(T002): seed evidence"], cwd=tmp_path)
    head_sha = git_head_sha(tmp_path)

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "git_commit": head_sha, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T000.md", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "git_commit": head_sha, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T002.md", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/feature/impl.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["final_verify_ready"] is False
    assert payload["next_task"] is None

def test_status_surfaces_repair_required_final_verify_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/feature/impl.py"]},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "blocked_reason": "synthesized repair bundle for final verification failures"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification found repairable issues; framework will continue with repair tasks"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "final_verify_status": "blocked",
            "repair_candidates": ["T002"],
            "repair_task_id": "T003",
            "repair_report_artifact": "docs/reviews/final-repair-report.md",
            "final_verify_missing_test_types": [["REQ-001", "browser"]],
            "final_verify_gate_statuses": [{"id": "GATE-release", "status": "pending", "report_artifact": "docs/reviews/gate-report-GATE-release.md"}],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["final_verify"]["status"] == "repair_required"
    assert payload["final_verify"]["will_continue"] is True
    assert payload["final_verify"]["repair_candidates"] == ["T002"]
    assert payload["final_verify"]["repair_task_id"] == "T003"
    assert payload["final_verify"]["repair_task_status"] == "pending"

def test_status_keeps_final_verify_blocked_after_repair_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/feature/impl.py"]},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass"},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003", "T004"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003", "T004", "T005"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification reached maximum repair iterations (3); remaining failures require manual escalation"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "final_verify_status": "blocked",
            "final_repair_limit_reached": True,
            "repair_candidates": ["T002"],
            "repair_task_id": "T005",
            "repair_report_artifact": "docs/reviews/final-repair-report.md",
            "final_verify_missing_test_types": [["REQ-001", "browser"]],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["final_verify"]["status"] == "blocked"
    assert payload["final_verify"]["will_continue"] is False
    assert payload["final_verify"]["repair_task_id"] == "T005"
    assert payload["final_verify"]["repair_task_status"] == "verified"


def test_cmd_control_status_blocks_active_final_repair_after_limit(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("delivery.control_plane.requirements_ready", lambda project_root: True)
    monkeypatch.setattr("delivery.control_plane.blocking_clarifications_present", lambda project_root: False)
    monkeypatch.setattr("delivery.control_plane.architecture_ready", lambda project_root: True)
    monkeypatch.setattr("delivery.control_plane.ui_required", lambda project_root: False)
    monkeypatch.setattr("delivery.control_plane.context_ready", lambda project_root: True)
    monkeypatch.setattr("delivery.control_plane.work_items_ready", lambda project_root: True)
    monkeypatch.setattr("delivery.control_plane.work_item_contract_errors", lambda project_root: {})
    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T003", "title": "Final Verification Repair Bundle (T009)", "status": "active", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T004", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003", "T004"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "Final verification", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004", "T005"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification reached maximum repair iterations (3); remaining failures require manual escalation"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "final_verify_status": "blocked",
            "final_repair_limit_reached": True,
            "repair_candidates": ["T002"],
            "repair_task_id": "T005",
        },
    )
    save_task_runtime_state(tmp_path, "T003", {"task_id": "T003", "status": "interrupted"})

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="status",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["control_status"] == "blocked"
    assert payload["must_continue"] is False
    assert payload["next_step"] is None
    assert payload["final_verify"]["will_continue"] is False


def test_status_defers_final_verify_repair_context_until_non_final_tasks_are_complete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "功能", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/feature/impl.py"]},
                {"id": "T003", "title": "Another feature", "status": "active", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/src/tests/test_more.py"], "output_paths": ["backend/src/more/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification found repairable issues; framework will continue with repair tasks"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "final_verify_status": "repair_required",
            "repair_candidates": ["T002"],
            "repair_task_id": "T010",
            "repair_report_artifact": "docs/reviews/final-repair-report.md",
        },
    )

    payload = loop_status(tmp_path)

    assert payload["final_verify"]["status"] == "deferred"
    assert payload["final_verify"]["will_continue"] is False
    assert "deferred until all non-final tasks are verified" in str(payload["final_verify"]["blocked_reason"])

def test_routed_status_ignores_stale_final_blocker_until_regular_tasks_finish(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.control_plane._planning_blocker_code", lambda project_root: None)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}, {"id": "REQ-002", "title": "Two", "summary": "Two"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(tmp_path, {"schema_version": "1", "coverage": []})
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature A", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/feature_a/"]},
                {"id": "T003", "title": "Feature B", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/src/tests/test_feature_b.py"], "output_paths": ["backend/src/feature_b/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002", "T003"], "output_tests": [], "output_paths": [], "blocked_reason": "final verification found repairable issues; framework will continue with repair tasks"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "final_verify_status": "repair_required",
            "repair_candidates": ["T002"],
            "repair_task_id": "T010",
            "repair_report_artifact": "docs/reviews/final-repair-report.md",
        },
    )

    payload = cli.routed_status(tmp_path)

    assert payload["control_status"] == "in_progress"
    assert payload["must_continue"] is True
    assert payload["next_step"]["action"] == "run_resume"
    assert payload["next_step"]["task_id"] == "T003"

def test_routed_status_resumes_interrupted_active_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.control_plane._planning_blocker_code", lambda project_root: None)
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T006", "title": "Case workbench", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/incidents.spec.ts"], "output_paths": ["frontend/src/routes/incidents.tsx"]},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T006", {"status": "running", "runtime_pid": 0, "wrapper_pid": 0})

    payload = cli.routed_status(tmp_path)

    assert payload["active_task"]["runtime_state"]["status"] == "interrupted"
    assert payload["control_status"] == "in_progress"
    assert payload["must_continue"] is True
    assert payload["next_step"]["action"] == "run_resume"
    assert payload["next_step"]["task_id"] == "T006"
    assert payload["next_step"]["runtime_status"] == "interrupted"

def test_cmd_control_auto_stops_when_final_task_is_blocked(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(tmp_path, {"schema_version": "1", "coverage": []})
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": tmp_path.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T001"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"], "blocked_reason": "final verification requirements are not yet satisfied"},
            ],
        },
    )
    monkeypatch.setattr("delivery.loop_reporting.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["control_status"] == "blocked"
    assert payload["must_continue"] is False
    assert payload["next_step"] is None

def test_status_prefers_live_task_ledger_over_stale_session_pointer(tmp_path: Path) -> None:
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T003", "title": "Task 3", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_3.py"], "output_paths": ["backend/src/t3.py"]},
                {"id": "T004", "title": "Task 4", "status": "active", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T003"], "output_tests": ["backend/tests/test_4.py"], "output_paths": ["backend/src/t4.py"]},
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "opencode",
                "task_count": 2,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Task 3",
                "current_task_id": "T003",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )
    (active_dir / "opencode-T004-live.json").write_text(
        json.dumps(
            {
                "pid": __import__("os").getpid(),
                "runtime": "opencode",
                "session_id": "session-live",
                "task_id": "T004",
                "task_title": "Task 4",
                "phase": "implementation",
                "project_root": str(tmp_path),
                "status": "running",
                "runtime_pid": __import__("os").getpid(),
                "updated_at": "2026-06-24T00:06:00Z",
                "created_at": "2026-06-24T00:06:00Z",
            }
        ),
        encoding="utf-8",
    )

    payload = loop_status(tmp_path)

    assert payload["active_task"]["id"] == "T004"

def test_status_treats_active_task_without_live_runtime_as_pending_display_state(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T004", "title": "Task 4", "status": "active", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_4.py"], "output_paths": ["backend/src/t4.py"]},
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "",
                "runtime": "opencode",
                "task_count": 1,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Task 4",
                "current_task_id": "T004",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["counts"].get("active", 0) == 0
    assert payload["counts"]["pending"] == 1
    assert payload["active_task"] is None
    assert payload["next_task"]["id"] == "T004"

def test_status_normalizes_stale_running_runtime_state_on_next_task(tmp_path: Path) -> None:
    from delivery.state import save_task_runtime_state

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Task 2", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_2.py"], "output_paths": ["backend/src/t2.py"]},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T002", {"status": "running", "runtime_pid": 0, "wrapper_pid": 0})

    payload = loop_status(tmp_path)

    assert payload["next_task"]["runtime_state"]["status"] == "interrupted"

def test_status_reports_verified_task_without_evidence_without_reopening(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "git_commit": None, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T000.md", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "verified", "git_commit": None, "review_status": "pass", "review_artifact": "docs/reviews/code-review-T001.md", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T001"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["final_verify_ready"] is False
    assert payload["counts"]["verified"] == 3
    assert payload["invalid_verified_tasks"] == {
        "T000": "missing task commit and pass code review evidence",
        "T001": "missing task commit and pass code review evidence",
        "T-FINAL": "missing pass final review evidence",
    }
    persisted = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert all(item["status"] == "verified" for item in persisted["items"])

def test_status_reports_verified_task_with_mock_only_browser_e2e_as_invalid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_gitops.git_commit_timestamp", lambda project_root, commit: "2026-06-24T00:00:00Z")
    docs_dir = tmp_path / "docs" / "reviews"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "code-review-T002.md").write_text("status: pass\nreview_type: code\n", encoding="utf-8")
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
                {"id": "T002", "title": "Cases", "status": "verified", "git_commit": "abc123", "review_status": "pass", "review_artifact": "docs/reviews/code-review-T002.md", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/case.spec.ts"], "output_paths": ["frontend/src/routes/cases.tsx"]},
            ],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["invalid_verified_tasks"] == {
        "T002": "frontend/e2e/case.spec.ts:3 page.route for project-owned API calls uses route.fulfill without route.fetch/route.continue/route.fallback passthrough; mocked browser proof is not real backend E2E evidence"
    }

def test_status_reports_verified_task_with_latest_failed_validation_as_invalid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_gitops.git_commit_timestamp", lambda project_root, commit: "2026-06-24T00:00:00Z")
    docs_dir = tmp_path / "docs" / "reviews"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "exception-report-T020.md").write_text("status: exception\n", encoding="utf-8")
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [
                {
                    "task_id": "T020",
                    "timestamp": "2026-07-07T15:57:21Z",
                    "test_files": ["frontend/e2e/ai-interaction.spec.ts"],
                    "test_types": ["browser", "e2e", "integration"],
                    "requirement_ids": ["REQ-056"],
                    "passed": False,
                    "passed_count": 0,
                    "failed_count": 1,
                    "failures": [{"test": "frontend/e2e/ai-interaction.spec.ts", "message": "Docker daemon unavailable"}],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {},
        },
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
                    "id": "T020",
                    "title": "UI Integration and AI Interaction Validation",
                    "status": "verified",
                    "git_commit": "abc123",
                    "review_status": None,
                    "review_artifact": "docs/reviews/exception-report-T020.md",
                    "blocked_reason": "[stalled_runtime] runtime liveness stalled",
                    "requirements": ["REQ-056"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/e2e/ai-interaction.spec.ts"],
                    "output_paths": ["docs/reviews/ui-ai-workbench-validation.md"],
                },
            ],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["invalid_verified_tasks"] == {
        "T020": "latest task validation failed at 2026-07-07T15:57:21Z; report=docs/reviews/test-report-T020.md"
    }

def test_status_reports_verified_task_with_mismatched_task_commit_as_invalid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_gitops.git_commit_timestamp", lambda project_root, commit: "2026-06-24T00:00:00Z")
    monkeypatch.setattr("delivery.loop_gitops.git_commit_subject", lambda project_root, commit: "feat(T019): Agent Integration Validation")
    docs_dir = tmp_path / "docs" / "reviews"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "code-review-T020.md").write_text("status: pass\n", encoding="utf-8")
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [
                {
                    "task_id": "T020",
                    "timestamp": "2026-07-07T15:46:12Z",
                    "test_files": ["frontend/e2e/ai-interaction.spec.ts"],
                    "test_types": ["browser", "e2e", "integration"],
                    "requirement_ids": ["REQ-056"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {},
        },
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
                    "id": "T020",
                    "title": "UI Integration and AI Interaction Validation",
                    "status": "verified",
                    "git_commit": "ec3c6f18f7d069eaa69b57661909b534c06c887e",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T020.md",
                    "requirements": ["REQ-056"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/e2e/ai-interaction.spec.ts"],
                    "output_paths": ["docs/reviews/ui-ai-workbench-validation.md"],
                },
            ],
        },
    )

    payload = loop_status(tmp_path)

    assert payload["invalid_verified_tasks"] == {
        "T020": "task commit subject does not match task id: ec3c6f18f7d069eaa69b57661909b534c06c887e has subject 'feat(T019): Agent Integration Validation'"
    }

def test_status_does_not_allow_final_ready_or_claim_with_invalid_verified_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_gitops.git_commit_timestamp", lambda project_root, commit: "2026-06-24T00:00:00Z")
    docs_dir = tmp_path / "docs" / "reviews"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "exception-report-T020.md").write_text("status: exception\n", encoding="utf-8")
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [
                {
                    "task_id": "T020",
                    "timestamp": "2026-07-07T15:57:21Z",
                    "test_files": ["frontend/e2e/ai-interaction.spec.ts"],
                    "test_types": ["browser", "e2e", "integration"],
                    "requirement_ids": ["REQ-056"],
                    "passed": False,
                    "passed_count": 0,
                    "failed_count": 1,
                    "failures": [{"test": "frontend/e2e/ai-interaction.spec.ts", "message": "Docker daemon unavailable"}],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {"passed": True},
        },
    )
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T020", "title": "UI Validation", "status": "verified", "git_commit": "abc123", "review_artifact": "docs/reviews/exception-report-T020.md", "requirements": ["REQ-056"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/ai-interaction.spec.ts"], "output_paths": ["docs/reviews/ui-ai-workbench-validation.md"]},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T020"], "output_tests": [], "output_paths": ["docs/release-evidence.md"]},
            ],
        },
    )

    pending_final_payload = loop_status(tmp_path)

    assert pending_final_payload["final_verify_ready"] is False
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T020", "title": "UI Validation", "status": "verified", "git_commit": "abc123", "review_artifact": "docs/reviews/exception-report-T020.md", "requirements": ["REQ-056"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/ai-interaction.spec.ts"], "output_paths": ["docs/reviews/ui-ai-workbench-validation.md"]},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T020"], "output_tests": [], "output_paths": ["docs/release-evidence.md"]},
            ],
        },
    )

    verified_final_payload = cli.project_summary(tmp_path)

    assert verified_final_payload["delivery_claim_allowed"] is False

def test_routed_status_reports_invalid_verified_task_without_blocking_next_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop_gitops.git_commit_timestamp", lambda project_root, commit: "2026-06-24T00:00:00Z")
    monkeypatch.setattr("delivery.control_plane._planning_blocker_code", lambda project_root: None)
    docs_dir = tmp_path / "docs" / "reviews"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "code-review-T002.md").write_text("status: pass\nreview_type: code\n", encoding="utf-8")
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
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": [], "git_commit": "base"},
                {"id": "T002", "title": "Cases", "status": "verified", "git_commit": "abc123", "review_status": "pass", "review_artifact": "docs/reviews/code-review-T002.md", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/case.spec.ts"], "output_paths": ["frontend/src/routes/cases.tsx"]},
                {"id": "T003", "title": "Downstream", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_api/test_downstream.py"], "output_paths": ["backend/src/downstream.py"]},
            ],
        },
    )

    payload = cli.routed_status(tmp_path)

    assert payload["next_task"]["id"] == "T003"
    assert payload["invalid_verified_tasks"]
    assert payload["control_status"] == "in_progress"
    assert payload["must_continue"] is True
    assert payload["next_step"]["action"] == "run_resume"
    assert payload["next_step"]["task_id"] == "T003"

def test_cmd_start_stops_on_blocking_clarification(tmp_path: Path, monkeypatch) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "clarification-needed.md").write_text("blocking_count: 1\n## C1 - Need decision\n", encoding="utf-8")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(tmp_path), requirements=None, runtime="claude"))

    assert exc_info.value.code == "clarification_blocking"
    assert exc_info.value.details["expected_input_path"].endswith("/.app-delivery-runtime/stage-inputs/spec-review.json")

def test_cmd_start_does_not_autoresolve_blocking_clarifications(tmp_path: Path, monkeypatch) -> None:
    requirements = tmp_path / "req.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    (docs_dir / "clarification-needed.md").write_text("blocking_count: 1\n## C1 - Need decision\n", encoding="utf-8")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(tmp_path), requirements=str(requirements), runtime="claude"))

    assert exc_info.value.code == "clarification_blocking"

def test_build_planning_host_step_clarification_blocking_requests_user_answers(tmp_path: Path) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
                "clarifications": [
                    {
                        "severity": "C1",
                        "question": "Need storage decision",
                        "rationale": "This changes persistence architecture.",
                        "affected_requirement_ids": ["REQ-001"],
                        "blocking": True,
                        "recommended_answer": "Use PostgreSQL.",
                        "answer_options": ["Use PostgreSQL", "Use SQLite"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    step = build_planning_host_step(
        code="clarification_blocking",
        project_root=tmp_path,
        requirements_path=str(tmp_path / "req.md"),
    )

    assert step is not None
    assert step["kind"] == "host_interaction"
    assert step["action"] == "collect_clarification_answers"
    assert step["requires_user_input"] is True
    assert step["answers_path"].endswith("/docs/clarification-answers.md")
    assert step["clarifications"][0]["question"] == "Need storage decision"
    assert step["clarifications"][0]["recommended_answer"] == "Use PostgreSQL."
    assert "Answer: " in step["answers_markdown_template"]
    assert "docs/clarification-answers.md" in step["resume_prompt"]

def test_build_planning_host_step_clarification_blocking_uses_answers_to_resume_spec_review(tmp_path: Path) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
                "clarifications": [
                    {
                        "severity": "C1",
                        "question": "Need storage decision",
                        "rationale": "This changes persistence architecture.",
                        "affected_requirement_ids": ["REQ-001"],
                        "blocking": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "clarification-answers.md").write_text("# Clarification Answers\n\n## Need storage decision\n\nAnswer: Use PostgreSQL.\n", encoding="utf-8")

    step = build_planning_host_step(
        code="clarification_blocking",
        project_root=tmp_path,
        requirements_path=str(tmp_path / "req.md"),
    )

    assert step is not None
    assert step["kind"] == "host_skill"
    assert step["skill"] == "spec-review"
    assert step["action"] == "repair_spec_review"
    assert "docs/clarification-answers.md" in step["prompt"]
    assert "fold the answered clarifications back into the normalized requirements" in step["prompt"]

def test_cmd_control_auto_defers_clarification_questions_to_outer_host(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    snapshots = [
        {
            "control_status": "blocked",
            "must_continue": False,
            "next_step": {
                "kind": "host_interaction",
                "owner": "host",
                "action": "collect_clarification_answers",
                "requires_user_input": True,
                "clarifications": [{"question": "Need storage decision"}],
            },
        },
    ]

    def fake_run_control(project_root, *, goal, requirements_path=None, repair_task_id=None):
        return dict(snapshots.pop(0))

    monkeypatch.setattr(cli, "run_control", fake_run_control)
    monkeypatch.setattr(cli, "_watchdog_enabled_for", lambda project_root: False)

    result = cli.cmd_control(
        argparse.Namespace(
            project=str(tmp_path),
            goal="auto",
            requirements=None,
            task_id=None,
            runtime=None,
            max_auto_tasks=None,
            max_control_steps=16,
            _locked=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["control_status"] == "blocked"
    assert payload["next_step"]["action"] == "collect_clarification_answers"
    assert "executed_steps" not in payload

def test_status_does_not_report_running_from_stale_session_pointer_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"]},
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "opencode",
                "task_count": 3,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )
    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["active_task"] is None
    assert payload["counts"].get("pending") == 1

def test_status_does_not_treat_released_execution_lock_metadata_as_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"]},
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-1",
                "runtime": "opencode",
                "task_count": 3,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )
    lock_dir = tmp_path / ".app-delivery-runtime" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "execution.lock").write_text(json.dumps({"pid": os.getpid(), "heartbeat_at": "2026-06-24T00:00:00Z"}) + "\n", encoding="utf-8")

    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["active_task"] is None
    assert payload["counts"].get("pending") == 1
    assert payload["stale_active_ledger_tasks"][0]["id"] == "T002"
    assert payload["stale_active_ledger_tasks"][0]["display_status"] == "pending"

def test_status_does_not_report_verified_task_from_stale_running_runtime_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "Foundation", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": [], "completed_at": "2026-06-24T00:10:00Z"},
                {"id": "T002", "title": "Feature", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": [], "output_paths": ["backend/src/feature/"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T001",
        {
            "task_id": "T001",
            "status": "running",
            "runtime_pid": 0,
            "wrapper_pid": 0,
            "updated_at": "2026-06-24T00:10:00Z",
        },
    )

    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["active_task"] is None
    assert payload["next_task"]["id"] == "T002"
    assert payload["counts"].get("verified") == 1
    assert payload["counts"].get("pending") == 1

def test_cmd_start_rejects_concurrent_execution(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    lock_dir = project_root / ".app-delivery-runtime" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "execution.lock"
    monkeypatch.setenv("APP_DELIVERY_EXECUTION_LOCK_TIMEOUT_SECONDS", "0")

    with lock_path.open("a+", encoding="utf-8") as handle:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write('{"pid":123,"heartbeat_at":"2026-06-24T00:00:00Z"}\n')
        handle.flush()
        with pytest.raises(DeliveryError) as exc_info:
            cli.cmd_start(argparse.Namespace(project=str(project_root), requirements=None, runtime="claude", max_auto_tasks=None))

    assert exc_info.value.code == "project_busy"
    assert exc_info.value.details["lock_owner"]["pid"] == 123

def test_cmd_start_rejects_invalid_task_contracts(tmp_path: Path) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T010", "title": "Broken", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(project_root), requirements=None, runtime="claude", max_auto_tasks=None))

    assert exc_info.value.code == "work_items_contract_invalid"
    assert exc_info.value.details["expected_input_path"].endswith("/.app-delivery-runtime/stage-inputs/task-decompose.json")

def test_cmd_start_does_not_autorepair_invalid_task_contracts(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(project_root, {"schema_version": "1", "coverage": []})
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": project_root.name,
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T010", "title": "Broken", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": ["T000"], "output_tests": [], "output_paths": ["src/bad.py"]},
            ],
        },
    )
    (project_root / "backend").mkdir(parents=True, exist_ok=True)

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(project_root), requirements=None, runtime="claude", max_auto_tasks=None))

    assert exc_info.value.code == "work_items_contract_invalid"

