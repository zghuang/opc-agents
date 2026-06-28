from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from importlib import import_module
from pathlib import Path

import pytest

from delivery.errors import DeliveryError
from delivery.loop_gitops import ensure_git_repo, git, git_head_sha
from delivery.loop_review import code_review_request_path
from delivery.loop import status as loop_status
from delivery.runtime_config import resolve_project_root
from delivery.skill_prompts import render_skill_prompt
from delivery.stage_harness import stage_import_command, stage_input_path
from delivery.state import load_gates, load_session_state, load_task_runtime_state, save_architecture_meta, save_session_state, save_task_runtime_state, save_test_plan, save_work_items


cli = import_module("delivery.__main__")


def test_stage_contract_reference_exists() -> None:
    path = Path(__file__).resolve().parents[2] / "skills" / "spec-review" / "references" / "stage-contract.md"
    assert path.exists()
    assert "structured JSON" in path.read_text(encoding="utf-8")


def test_render_skill_prompt_reads_stage_contract_reference() -> None:
    prompt = render_skill_prompt("spec-review.md", source_document="# Raw requirements\n")

    assert "Return JSON with top-level fields" in prompt
    assert "# Raw requirements" in prompt


def test_build_task_prompt_explains_project_relative_test_paths(tmp_path: Path) -> None:
    from delivery.loop_task_prompt import build_task_prompt
    from delivery.task import Task

    task = Task.from_dict(
        {
            "id": "T002",
            "title": "Auth",
            "status": "pending",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": [],
            "dependencies": ["T001"],
            "output_tests": ["backend/src/tests/test_auth.py::test_login_success", "frontend/e2e/login.spec.ts::test_login_success_navigation"],
            "output_paths": ["backend/src/core/auth.py", "frontend/src/lib/auth-context.tsx"],
        }
    )

    prompt = build_task_prompt(tmp_path, task)

    assert "The harness normalizes common backend/frontend path prefixes" in prompt
    assert "cd backend && uv run pytest" in prompt
    assert "frontend/package.json" in prompt


def test_stage_input_path_uses_runtime_stage_inputs_dir(tmp_path: Path) -> None:
    path = stage_input_path(tmp_path, "task-decompose")
    assert path == tmp_path / ".app-delivery-runtime" / "stage-inputs" / "task-decompose.json"


def test_resolve_project_root_uses_opc_projects_for_bare_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPC_HOME", str(tmp_path))

    path = resolve_project_root("scms-01")

    assert path == tmp_path / "projects" / "scms-01"


def test_stage_import_command_uses_cli_alias_for_context_sync(tmp_path: Path) -> None:
    command = stage_import_command(tmp_path, "project-context-sync")
    assert "app-delivery context-sync --project" in command
    assert "project-context-sync --project" not in command


def test_stage_import_command_uses_cli_alias_for_decompose(tmp_path: Path) -> None:
    command = stage_import_command(tmp_path, "task-decompose")
    assert "app-delivery decompose --project" in command
    assert "task-decompose --project" not in command


def test_cmd_decompose_imports_from_input_file(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {
                    "tier": "S",
                    "rationale": "Single feature project.",
                    "signals": {"estimated_loc": 8000, "estimated_tasks": 1, "estimated_modules": 1},
                },
                "validation_gates": [
                    {
                        "id": "GATE-RELEASE",
                        "kind": "release",
                        "title": "Release gate",
                        "scope_tasks": ["T002"],
                        "scope_requirements": ["REQ-001"],
                        "required_test_types": ["unit"],
                    }
                ],
                "items": [
                    {
                        "title": "Feature",
                        "task_kind": "feature",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": [],
                        "output_tests": ["tests/test_feature.py"],
                        "output_paths": ["backend/src/feature.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    payload = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))
    assert any(item["title"] == "Feature" for item in payload["items"])
    persisted = json.loads((tmp_path / ".app-delivery-runtime" / "stage-inputs" / "task-decompose.json").read_text(encoding="utf-8"))
    assert isinstance(persisted, dict)
    assert persisted["items"][0]["title"] == "Feature"
    gates = load_gates(tmp_path)
    assert any(gate["id"] == "GATE-RELEASE" for gate in gates["gates"])


def test_cmd_decompose_imports_complexity_and_validation_gates_from_object_payload(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {
                    "tier": "M",
                    "rationale": "Estimated 12-18 tasks across multiple modules.",
                    "signals": {"estimated_loc": 60000, "estimated_tasks": 14, "estimated_modules": 5},
                },
                "validation_gates": [
                    {
                        "id": "GATE-auth-domain",
                        "kind": "module",
                        "title": "Module gate: auth domain",
                        "scope_tasks": ["T002"],
                        "scope_requirements": ["REQ-001"],
                        "required_test_types": ["api", "browser"],
                    }
                ],
                "items": [
                    {
                        "title": "Feature",
                        "task_kind": "feature",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": [],
                        "output_tests": ["tests/test_feature.py"],
                        "output_paths": ["backend/src/feature.py"],
                    },
                    {
                        "title": "Auth domain validation",
                        "task_kind": "validation",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": ["T002"],
                        "output_tests": ["backend/tests/test_feature.py"],
                        "output_paths": ["docs/reviews/test-report-auth-domain.md"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    gates = load_gates(tmp_path)
    assert gates["complexity"]["tier"] == "M"
    assert gates["complexity"]["source"] == "task-decompose"
    assert gates["gates"][0]["id"] == "GATE-auth-domain"


def test_cmd_decompose_rejects_invalid_task_contracts_during_import(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {
                    "tier": "S",
                    "rationale": "Single feature project.",
                    "signals": {"estimated_loc": 8000, "estimated_tasks": 1, "estimated_modules": 1},
                },
                "validation_gates": [],
                "items": [
                    {
                        "title": "Feature",
                        "task_kind": "feature",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": [],
                        "output_tests": ["../tests/test_feature.py"],
                        "output_paths": ["../src/feature.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "stage_output_invalid"
    assert exc_info.value.details["contract_errors"]["T002"]


def test_cmd_decompose_requires_full_object_payload_contract(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "title": "Feature",
                        "task_kind": "feature",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": [],
                        "output_tests": ["backend/tests/test_feature.py"],
                        "output_paths": ["backend/src/feature.py"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "input_missing_fields"
    assert "delivery_complexity" in exc_info.value.message
    assert exc_info.value.details["missing_fields"] == ["delivery_complexity", "validation_gates"]


def test_cmd_code_review_import_marks_task_verified(tmp_path: Path, monkeypatch) -> None:
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
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "status_session_id": "ses-1",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Looks good.", "findings": []}), encoding="utf-8")

    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    assert payload["items"][0]["git_commit"] == "abc123"
    assert payload["items"][0]["review_status"] == "pass"
    assert payload["items"][0]["review_artifact"] == "docs/reviews/code-review-T002.md"


def test_cmd_final_review_import_marks_t_final_verified(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "review_pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": [],
                    "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"],
                }
            ],
        },
    )
    input_path = tmp_path / "final-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Release ready.", "findings": []}), encoding="utf-8")

    result = cli.cmd_final_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    assert payload["items"][0]["review_status"] == "pass"
    assert payload["items"][0]["review_artifact"] == "docs/reviews/final-review.md"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["final_verify_status"] == "pass"


def test_cmd_start_requires_external_spec_review_from_requirements(tmp_path: Path) -> None:
    requirements = tmp_path / "req.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_start(argparse.Namespace(project=str(tmp_path), requirements=str(requirements), runtime="claude"))

    assert exc_info.value.code == "requirements_missing"
    assert exc_info.value.details["required_skill"] == "spec-review"
    assert exc_info.value.details["requirements_path"] == str(requirements.resolve())
    assert (tmp_path / "docs" / "requirements-source.md").read_text(encoding="utf-8") == "# Raw requirements\n"


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
    assert (docs_dir / "project-summary.json").exists()
    assert (docs_dir / "project-summary.md").exists()


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


def test_cmd_start_reuses_existing_bootstrap(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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


def test_cmd_control_status_routes_blocked_final_verify_to_repair_task(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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


def test_status_gate_repair_task_includes_missing_type_command_specs(tmp_path: Path, monkeypatch) -> None:
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

    loop_status(tmp_path)
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    repair_task = next(item for item in payload["items"] if item.get("task_kind") == "repair")
    assert "npm run lint" in repair_task["output_tests"]
    assert "npm run typecheck" in repair_task["output_tests"]
    assert "npm run build" in repair_task["output_tests"]
    assert "backend/tests/" in repair_task["output_tests"]


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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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


def test_cmd_control_status_blocks_when_final_repair_task_is_missing(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    assert payload["next_step"] is None


def test_cmd_control_auto_stops_when_final_repair_task_is_missing(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    assert called == ["T002"]
    assert payload["executed_steps"][0]["action"] == "run_repair_task"
    assert payload["must_continue"] is False


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
    assert len(payload["executed_steps"]) == 1
    assert payload["executed_steps"][0]["owner"] == "host"
    assert payload["executed_steps"][0]["action"] == "run_code_review"
    assert payload["executed_steps"][0]["deferred_to_host"] is True
    assert payload["next_step"]["owner"] == "host"


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


def test_cmd_control_auto_defers_stale_host_review_input_until_new_review_arrives(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / ".app-delivery-runtime" / "review-inputs" / "code-review-T002.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps({"status": "changes_requested", "summary": "old", "findings": ["stale"]}), encoding="utf-8")
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
    assert result == 0
    assert imported == []
    assert payload["executed_steps"][0]["owner"] == "host"
    assert payload["executed_steps"][0]["action"] == "run_code_review"
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    assert payload["next_step"]["action"] == "run_resume"
    assert payload["next_step"]["owner"] == "framework"
    assert payload["next_step"]["task_id"] == "T003"


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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
                "title": "OTIF Calculation Engine & Risk Scoring",
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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


def test_status_surfaces_repair_required_final_verify_context(tmp_path: Path) -> None:
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
    assert payload["final_verify"]["repair_task_status"] is None


def test_cmd_control_auto_stops_when_final_task_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements-source.md").write_text("# Raw requirements\n", encoding="utf-8")
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    tmp_path.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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


def test_status_repairs_verified_task_without_evidence(tmp_path: Path) -> None:
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
    assert payload["counts"]["pending"] == 3
    assert payload["invalid_verified_tasks"] == {
        "T000": "missing task commit and pass code review evidence",
        "T001": "missing task commit and pass code review evidence",
        "T-FINAL": "missing pass final review evidence",
    }
    persisted = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert all(item["status"] == "pending" for item in persisted["items"])


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


def test_cmd_watchdog_run_continues_autorunnable_host_step(tmp_path: Path, monkeypatch) -> None:
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
    assert calls == ["auto"]


def test_status_does_not_report_running_from_stale_session_pointer_only(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
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
    monkeypatch.setattr("delivery.loop_reporting.process_alive", lambda pid: False)

    result = cli.cmd_status(argparse.Namespace(project=str(tmp_path)))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["active_task"] is None
    assert payload["counts"].get("pending") == 1


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


def test_cmd_start_rejects_concurrent_execution(tmp_path: Path) -> None:
    project_root = tmp_path
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": False})
    project_root.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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
    project_root.joinpath("CODE_MAP.md").write_text("code map\n", encoding="utf-8")
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


def test_cmd_context_sync_accepts_runtime_specific_context_only(tmp_path: Path) -> None:
    input_path = tmp_path / "context.json"
    input_path.write_text(
        json.dumps(
            {
                "claude_md": "claude\n",
                "code_map_md": "# CODE_MAP.md — Demo\n\n## Project Root\n\nold tree\n\n## Where to Add New Code\n\n- add routes under backend/src/api/\n",
                "test_plan": {"schema_version": "1", "coverage": []},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "architecture.md").write_text(
        "# Architecture\n\n## 3. Module Architecture\n\n```text\ndemo/\n├── backend/\n└── frontend/\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "project-structure.md").write_text(
        "# Project Structure\n\n- backend/\n- frontend/\n- docs/\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "project-bootstrap.json").write_text(
        json.dumps({"runtime": "claude"}), encoding="utf-8"
    )

    result = cli.cmd_context_sync(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    assert (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    claude_text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    code_map_text = (tmp_path / "CODE_MAP.md").read_text(encoding="utf-8")
    assert "claude\n" in claude_text
    assert "## Runtime Baseline" in claude_text
    assert "docs/requirements-source.md is the raw requirements record" in claude_text
    assert "framework artifacts for planning and traceability" in claude_text
    assert "do not guess APIs from memory" in claude_text
    assert "Source of truth for the current filesystem layout: `docs/project-structure.md`" in code_map_text
    assert "Source of truth for the intended target structure: `docs/architecture.md` -> `Module Architecture`." in code_map_text
    assert "old tree" not in code_map_text
    assert "add routes under backend/src/api/" in code_map_text
    persisted = json.loads((tmp_path / ".app-delivery-runtime" / "stage-inputs" / "project-context-sync.json").read_text(encoding="utf-8"))
    assert persisted["code_map_md"].startswith("# CODE_MAP.md — Demo")


def test_cmd_arch_design_normalizes_design_paths(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## 3. Module Architecture\n\n```text\nproject/\n├── backend/\n└── frontend/\n```\n",
                "shared_components_md": "shared\n",
                "ui_required": True,
                "modules": [{"path": "docs/architecture/module-a.md", "content": "module\n"}],
                "adrs": [{"path": "docs/architecture/adr-001.md", "content": "adr\n"}],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    assert (docs_dir / "modules" / "module-a.md").exists()
    assert (docs_dir / "adr" / "adr-001.md").exists()
    assert not (docs_dir / "docs").exists()
    persisted = json.loads((tmp_path / ".app-delivery-runtime" / "stage-inputs" / "arch-design.json").read_text(encoding="utf-8"))
    assert persisted["ui_required"] is True


def test_cmd_arch_design_requires_module_architecture_section(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## 3. 分层架构\n\n```text\nlayered\n```\n",
                "shared_components_md": "shared\n",
                "ui_required": True,
                "modules": [],
                "adrs": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "input_invalid_shape"
    assert "Module Architecture" in exc_info.value.message