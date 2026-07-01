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

    assert "minimum validation floor" in prompt
    assert "Passing tests are necessary but not sufficient" in prompt
    assert "Prefer the declared output paths and output tests" in prompt
    assert "cd backend && uv run pytest" in prompt
    assert "frontend/package.json" in prompt


def test_task_and_review_prompts_inline_relevant_technology_constraints(tmp_path: Path) -> None:
    from delivery.loop_review import build_code_review_request
    from delivery.loop_task_prompt import build_task_prompt

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "dependency_hints": [
                    {
                        "ecosystem": "backend",
                        "name": "LangGraph",
                        "source": "requirements-analysis",
                        "reason": "Agent orchestration framework with checkpointing and human-in-the-loop resume.",
                        "evidence": "ADR-001",
                    },
                    {
                        "ecosystem": "backend",
                        "name": "PostgreSQL",
                        "source": "requirements-analysis",
                        "reason": "Primary database.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    task = Task.from_dict(
        {
            "id": "T015",
            "title": "Orchestrator Agent Graph: LangGraph State Machine",
            "status": "pending",
            "requirements": ["REQ-017"],
            "acceptance_scenarios": [],
            "dependencies": ["T013"],
            "output_tests": ["backend/tests/agents/test_orchestrator_graph.py"],
            "output_paths": ["backend/src/agents/graph.py", "backend/src/agents/orchestrator.py"],
        }
    )

    task_prompt = build_task_prompt(tmp_path, task)
    review_prompt = build_code_review_request(tmp_path, task)

    assert "Project technology constraints for this task:" in task_prompt
    assert "LangGraph (backend): Agent orchestration framework" in task_prompt
    assert "ADR-001" in task_prompt
    assert "do not replace them with custom implementations silently" in task_prompt
    assert "PostgreSQL" not in task_prompt
    assert "Project technology constraints to verify:" in review_prompt
    assert "LangGraph (backend): Agent orchestration framework" in review_prompt
    assert "dependency entries, imports/usages, adapters, configuration" in review_prompt
    assert "status=changes_requested" in review_prompt
    assert "PostgreSQL" not in review_prompt


def test_shared_foundation_prompt_has_intent_and_manifest_boundary(tmp_path: Path) -> None:
    from delivery.loop_task_prompt import build_task_prompt
    from delivery.task import Task

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "dependency_hints": [
                    {
                        "ecosystem": "backend",
                        "name": "LangGraph",
                        "source": "requirements-analysis",
                        "reason": "Agent orchestration framework.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    task = Task.from_dict(
        {
            "id": "T001",
            "title": "Shared foundation",
            "status": "pending",
            "requirements": [],
            "acceptance_scenarios": [],
            "dependencies": ["T000"],
            "output_tests": ["backend/src/tests/test_health.py"],
            "output_paths": ["backend/pyproject.toml", "backend/uv.lock", "backend/src/runtime/"],
            "intent": {
                "objective": "Create the minimal shared foundation.",
                "done_when": ["Foundation smoke tests pass"],
                "non_goals": ["Do not implement agent graphs"],
            },
        }
    )

    prompt = build_task_prompt(tmp_path, task)

    assert "Task intent:" in prompt
    assert "Foundation smoke tests pass" in prompt
    assert "For shared foundation, install only dependencies directly used" in prompt
    assert "LangGraph (backend)" not in prompt


def test_build_prefinal_audit_prompt_is_self_contained(tmp_path: Path) -> None:
    from delivery.loop_task_prompt import build_task_prompt

    task = Task(
        id=PREFINAL_AUDIT_TASK_ID,
        title="Pre-final full-system audit",
        status="pending",
        requirements=[],
        acceptance_scenarios=[],
        dependencies=["T002"],
        output_tests=list(PREFINAL_AUDIT_OUTPUT_TESTS),
        output_paths=list(PREFINAL_AUDIT_OUTPUT_PATHS),
        task_kind="audit",
    )

    prompt = build_task_prompt(tmp_path, task)

    assert "new runtime session" in prompt
    assert "do not rely on hidden conversation history" in prompt
    assert PREFINAL_AUDIT_REPORT_PATH in prompt
    assert "## Requirement Gap Matrix" in prompt
    assert "scaffolding, stubs, shared wiring, or placeholders" in prompt
    assert "Use other project docs" in prompt
    assert "do not assume Python, React" in prompt
    assert "The framework will run exhaustive final verification" in prompt
    assert "T-FINAL" not in prompt
    assert "Run browser/end-to-end checks only" in prompt
    assert "Do not modify the app-delivery framework repository" not in prompt


def test_build_frontend_api_audit_prompt_requires_real_backend_e2e_assessment(tmp_path: Path) -> None:
    from delivery.loop_task_prompt import build_task_prompt

    task = Task(
        id=FRONTEND_API_AUDIT_TASK_ID,
        title="Frontend API integration audit",
        status="pending",
        requirements=[],
        acceptance_scenarios=[],
        dependencies=["T002"],
        output_tests=list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
        output_paths=list(FRONTEND_API_AUDIT_OUTPUT_PATHS),
        task_kind="audit",
    )

    prompt = build_task_prompt(tmp_path, task)

    assert FRONTEND_API_AUDIT_REPORT_PATH in prompt
    assert "page.route" in prompt
    assert "route.fulfill" in prompt
    assert "real backend E2E" in prompt
    assert "placeholder specs" in prompt
    assert "smoke-only" in prompt
    assert "mocked-browser" in prompt
    assert "## API Surface Mapping" in prompt
    assert "## Mocked Browser Test Assessment" in prompt
    assert "## Real Backend E2E Readiness" in prompt
    assert "test data" in prompt.casefold()


def test_stage_input_path_uses_runtime_stage_inputs_dir(tmp_path: Path) -> None:
    path = stage_input_path(tmp_path, "task-decompose")
    assert path == tmp_path / ".app-delivery-runtime" / "stage-inputs" / "task-decompose.json"


def test_resolve_project_root_uses_opc_projects_for_bare_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPC_HOME", str(tmp_path))

    path = resolve_project_root("scms-01")

    assert path == tmp_path / "projects" / "scms-01"


def test_resolve_project_root_rejects_absolute_path_outside_opc_projects_when_strict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPC_HOME", str(tmp_path))
    monkeypatch.setenv("APP_DELIVERY_ENFORCE_OPC_PROJECT_ROOT", "1")

    with pytest.raises(DeliveryError) as exc_info:
        resolve_project_root("/Users/hzg/apps/agents/m-opc")

    assert exc_info.value.code == "project_root_outside_opc_projects"


def test_resolve_project_root_allows_absolute_path_inside_opc_projects_when_strict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPC_HOME", str(tmp_path))
    monkeypatch.setenv("APP_DELIVERY_ENFORCE_OPC_PROJECT_ROOT", "1")
    project_root = tmp_path / "projects" / "ai-platform"
    project_root.mkdir(parents=True)

    path = resolve_project_root(str(project_root))

    assert path == project_root.resolve()


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
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}, {"id": "REQ-002", "title": "Two", "summary": "Two"}], "acceptance_scenarios": []}), encoding="utf-8")
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
                        "requirements": ["REQ-001", "REQ-002"],
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
    by_id = {item["id"]: item for item in payload["items"]}
    assert PREFINAL_AUDIT_TASK_ID in by_id
    assert by_id[PREFINAL_AUDIT_TASK_ID]["task_kind"] == "audit"
    assert by_id[PREFINAL_AUDIT_TASK_ID]["status"] == "pending"
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002"]
    assert by_id[PREFINAL_AUDIT_TASK_ID]["output_paths"][-1] == PREFINAL_AUDIT_REPORT_PATH
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == [PREFINAL_AUDIT_TASK_ID]
    persisted = json.loads((tmp_path / ".app-delivery-runtime" / "stage-inputs" / "task-decompose.json").read_text(encoding="utf-8"))
    assert isinstance(persisted, dict)
    assert persisted["items"][0]["title"] == "Feature"
    gates = load_gates(tmp_path)
    assert any(gate["id"] == "GATE-RELEASE" for gate in gates["gates"])


def test_cmd_decompose_rejects_backend_root_package_paths(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {"tier": "S", "rationale": "Small", "signals": {}},
                "validation_gates": [],
                "items": [
                    {"title": "Feature", "task_kind": "feature", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/api/feature.py"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "stage_output_invalid"
    assert "backend-root package paths" in exc_info.value.message


def test_cmd_decompose_rejects_top_level_mcp_server_paths(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {"tier": "S", "rationale": "Small", "signals": {}},
                "validation_gates": [],
                "items": [
                    {
                        "title": "MCP tools",
                        "task_kind": "feature",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": [],
                        "output_tests": ["mcp-server/tests/test_tools.py"],
                        "output_paths": ["mcp-server/server.py", "mcp-server/tools/rules.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "stage_output_invalid"
    assert "top-level mcp-server paths" in exc_info.value.message


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
                    "requirements": ["REQ-001", "REQ-002"],
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
    input_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "The declared feature behavior is complete."},
                    {"id": "REQ-002", "status": "pass", "notes": "The related requirement behavior is complete."},
                ],
                "acceptance_assessment": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"
    assert payload["items"][0]["git_commit"] == "abc123"
    assert payload["items"][0]["review_status"] == "pass"
    assert payload["items"][0]["review_artifact"] == "docs/reviews/code-review-T002.md"


def test_cmd_code_review_task_contract_repair_moves_acceptance_to_later_task(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": [{"id": "AS-001", "title": "Scenario", "summary": "Needs a later workflow.", "source_requirement_ids": ["REQ-001"]}]}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(tmp_path, {"schema_version": "1", "coverage": []})
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {
                    "id": "T002",
                    "title": "Feature",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "status_session_id": "ses-1",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                },
                {"id": "T003", "title": "Later Workflow", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_workflow.py"], "output_paths": ["backend/src/workflow.py"]},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Declared acceptance scenario belongs to a later workflow task.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": [],
                        "acceptance_ids": ["AS-001"],
                        "message": "AS-001 cannot be completed inside this task contract.",
                    }
                ],
                "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "Task-local requirement behavior is complete."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "changes_requested", "notes": "Scenario requires a later workflow task."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [
                        {
                            "op": "move_acceptance_scenario",
                            "id": "AS-001",
                            "from_task": "T002",
                            "to_task_id": "T003",
                            "rationale": "AS-001 belongs to the later workflow task.",
                        }
                    ],
                    "affected_requirement_ids": [],
                    "affected_acceptance_ids": ["AS-001"],
                    "notes": "Move AS-001 to the task that owns the workflow implementation.",
                },
            }
        ),
        encoding="utf-8",
    )
    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["status"] == "pending"
    assert by_id["T002"]["review_status"] is None
    assert by_id["T002"]["acceptance_scenarios"] == []
    assert by_id["T003"]["acceptance_scenarios"] == ["AS-001"]
    assert by_id["T003"]["requirements"] == ["REQ-001"]
    assert by_id["T003"]["status"] == "pending"
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_repair"]["applied_operations"][0]["mode"] == "moved_to_existing_task"


def test_cmd_code_review_task_contract_repair_creates_followup_when_no_target_exists(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": [{"id": "AS-001", "title": "Scenario", "summary": "Needs a later workflow.", "source_requirement_ids": ["REQ-001"]}]}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "status_session_id": "ses-1", "completed_at": "2026-06-24T01:00:00Z", "attempts": 1},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Declared acceptance scenario needs its own follow-up task.",
                "findings": [{"severity": "blocking", "requirement_ids": [], "acceptance_ids": ["AS-001"], "message": "AS-001 cannot be completed inside this task contract."}],
                "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "Task-local requirement behavior is complete."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "changes_requested", "notes": "Scenario requires a separate follow-up task."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [
                        {
                            "op": "move_acceptance_scenario",
                            "id": "AS-001",
                            "from_task": "T002",
                            "to_task_hint": "workflow implementation",
                            "followup_task": {
                                "title": "Acceptance Scenario Follow-up: AS-001 workflow",
                                "requirements": ["REQ-001"],
                                "output_tests": ["backend/tests/test_as_001_workflow.py"],
                                "output_paths": ["backend/src/workflow/as_001.py"],
                            },
                        }
                    ],
                    "affected_requirement_ids": [],
                    "affected_acceptance_ids": ["AS-001"],
                    "notes": "Create a follow-up task for AS-001.",
                },
            }
        ),
        encoding="utf-8",
    )
    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    followup = next(item for item in payload["items"] if item["title"] == "Acceptance Scenario Follow-up: AS-001 workflow")
    assert by_id["T002"]["acceptance_scenarios"] == []
    assert followup["acceptance_scenarios"] == ["AS-001"]
    assert followup["dependencies"] == ["T002"]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == ["T002", followup["id"]]
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_repair"]["applied_operations"][0]["mode"] == "created_followup_task"


def test_cmd_code_review_task_contract_repair_defers_acceptance_when_local_repair_is_unsafe(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": [{"id": "AS-001", "title": "Scenario", "summary": "Needs a later workflow."}]}), encoding="utf-8")
    (docs_dir / "architecture.md").write_text("architecture\n", encoding="utf-8")
    (docs_dir / "shared-components.md").write_text("shared\n", encoding="utf-8")
    save_architecture_meta(tmp_path, {"schema_version": "1", "ui_required": False})
    tmp_path.joinpath("CLAUDE.md").write_text("claude\n", encoding="utf-8")
    save_test_plan(tmp_path, {"schema_version": "1", "coverage": []})
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "status_session_id": "ses-1", "completed_at": "2026-06-24T01:00:00Z", "attempts": 1},
                {"id": FINAL_VERIFY_TASK_ID, "title": "Final", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Declared acceptance scenario belongs elsewhere but no safe target was identified.",
                "findings": [{"severity": "blocking", "requirement_ids": [], "acceptance_ids": ["AS-001"], "message": "AS-001 cannot be completed inside this task contract."}],
                "requirement_assessment": [{"id": "REQ-001", "status": "pass", "notes": "Task-local requirement behavior is complete."}],
                "acceptance_assessment": [{"id": "AS-001", "status": "changes_requested", "notes": "Scenario requires a later workflow task."}],
                "task_contract_assessment": {
                    "status": "changes_requested",
                    "issue_type": "task_contract",
                    "recommended_action": "task_contract_repair",
                    "operations": [{"op": "move_acceptance_scenario", "id": "AS-001", "from_task": "T002", "to_task_hint": "unknown workflow"}],
                    "affected_requirement_ids": [],
                    "affected_acceptance_ids": ["AS-001"],
                    "notes": "Move AS-001 when a safe target is available.",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.loop_review.git_commit_explicit_paths", lambda project_root, paths, message: "abc123")

    result = cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["status"] == "verified"
    assert by_id["T002"]["review_status"] == "pass"
    assert by_id["T002"]["acceptance_scenarios"] == []
    assert "verified with deferred acceptance" in by_id["T002"]["blocked_reason"]
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["task_contract_deferred_acceptance"]["deferred_acceptance_scenarios"][0]["id"] == "AS-001"
    assert runtime_state["task_contract_blocker"] is None
    deferrals = json.loads((tmp_path / "docs" / "reviews" / "task-contract-deferrals.json").read_text(encoding="utf-8"))
    assert deferrals["deferred_acceptance_scenarios"][0]["policy"] == "delivery_continue"
    review_md = (tmp_path / "docs" / "reviews" / "code-review-T002.md").read_text(encoding="utf-8")
    assert "Deferred acceptance scenario assignment" in review_md

    monkeypatch.setattr("delivery.control_plane.needs_scaffold", lambda project_root: False)
    control_result = cli.cmd_control(
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

    routed = json.loads(capsys.readouterr().out)
    assert control_result == 0
    assert routed["planning_blocker_code"] is None
    assert routed["next_step"]["action"] == "run_final_verify"


def test_cmd_code_review_rejects_pass_when_latest_task_tests_failed(tmp_path: Path, monkeypatch) -> None:
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
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": "T002",
                    "timestamp": "2026-06-24T01:01:00Z",
                    "test_files": ["backend/tests/test_feature.py"],
                    "test_types": ["unit"],
                    "requirement_ids": ["REQ-001"],
                    "passed": False,
                    "passed_count": 0,
                    "failed_count": 1,
                    "failures": [{"test": "backend/tests/test_feature.py", "message": "assertion failed", "traceback": "..."}],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {},
        },
    )
    input_path = tmp_path / "code-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "Looks complete."},
                ],
                "acceptance_assessment": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id="T002", input=str(input_path)))

    assert exc_info.value.code == "review_pass_preconditions_failed"
    assert "latest task validation failed" in exc_info.value.details["precondition_errors"][0]
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "review_pending"
    assert not (tmp_path / "docs" / "reviews" / "code-review-T002.md").exists()


def test_cmd_code_review_rejects_prefinal_audit_pass_when_report_missing(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": PREFINAL_AUDIT_TASK_ID,
                    "title": "Pre-final full-system audit",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS),
                    "status_session_id": "ses-audit",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": PREFINAL_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:01:00Z",
                    "test_files": list(PREFINAL_AUDIT_OUTPUT_TESTS),
                    "test_types": ["unit"],
                    "requirement_ids": [],
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
    input_path = tmp_path / "audit-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Audit ready.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id=PREFINAL_AUDIT_TASK_ID, input=str(input_path)))

    assert exc_info.value.code == "review_pass_preconditions_failed"
    assert "required audit report is missing" in exc_info.value.details["precondition_errors"][0]
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "review_pending"
    assert not (tmp_path / "docs" / "reviews" / "code-review-T-SYSTEM-AUDIT.md").exists()


def test_cmd_code_review_rejects_frontend_api_audit_pass_when_report_missing(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": FRONTEND_API_AUDIT_TASK_ID,
                    "title": "Frontend API integration audit",
                    "status": "review_pending",
                    "task_kind": "audit",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
                    "output_paths": list(FRONTEND_API_AUDIT_OUTPUT_PATHS),
                    "status_session_id": "ses-frontend-audit",
                    "completed_at": "2026-06-24T01:00:00Z",
                    "attempts": 1,
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T01:01:00Z",
            "results": [
                {
                    "task_id": FRONTEND_API_AUDIT_TASK_ID,
                    "timestamp": "2026-06-24T01:01:00Z",
                    "test_files": list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
                    "test_types": ["unit"],
                    "requirement_ids": [],
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
    input_path = tmp_path / "frontend-audit-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Audit ready.", "findings": [], "requirement_assessment": [], "acceptance_assessment": []}), encoding="utf-8")
    monkeypatch.setattr("delivery.loop_review.git_commit_task", lambda project_root, task, message, extra_paths=None: "abc123")

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_code_review(argparse.Namespace(project=str(tmp_path), task_id=FRONTEND_API_AUDIT_TASK_ID, input=str(input_path)))

    assert exc_info.value.code == "review_pass_preconditions_failed"
    assert "required frontend/API audit report is missing" in exc_info.value.details["precondition_errors"][0]
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "review_pending"
    assert not (tmp_path / "docs" / "reviews" / f"code-review-{FRONTEND_API_AUDIT_TASK_ID}.md").exists()


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


def test_cmd_final_review_import_changes_requested_creates_repair_bundle(tmp_path: Path) -> None:
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
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "review_status": "pass",
                    "git_commit": "abc123",
                },
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "review_pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": [],
                    "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"],
                },
            ],
        },
    )
    input_path = tmp_path / "final-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Release still misses the user-visible feature behavior.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": ["AS-001"],
                        "message": "Feature behavior is not complete enough for release.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_final_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    repair_id = next(item["id"] for item in payload["items"] if item.get("task_kind") == "repair")
    assert by_id[repair_id]["title"] == "Final Review Repair Bundle (T002)"
    assert by_id[repair_id]["requirements"] == ["REQ-001"]
    assert by_id[repair_id]["acceptance_scenarios"] == ["AS-001"]
    assert by_id[repair_id]["output_tests"] == ["backend/tests/test_feature.py"]
    assert by_id["T-FINAL"]["status"] == "blocked"
    assert by_id["T-FINAL"]["blocked_reason"] == f"final review created repair task {repair_id}; preserve verified tasks and repair through that bundle"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == repair_id
    assert runtime_state["repair_candidates"] == ["T002"]
    assert runtime_state["final_verify_status"] == "repair_required"
    review_md = (tmp_path / "docs" / "reviews" / "final-review.md").read_text(encoding="utf-8")
    assert "[blocking] Feature behavior is not complete enough for release. (REQ-001, AS-001)" in review_md


def test_cmd_final_review_import_changes_requested_stops_after_repair_iteration_limit(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T004", "title": "Final Review Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "ghi789"},
                {"id": "T005", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass", "git_commit": "jkl012"},
                {"id": "T-FINAL", "title": "最终验证", "status": "review_pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003", "T004", "T005"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    input_path = tmp_path / "final-review.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "changes_requested",
                "summary": "Release still misses the user-visible feature behavior.",
                "findings": [
                    {
                        "severity": "blocking",
                        "requirement_ids": ["REQ-001"],
                        "acceptance_ids": [],
                        "message": "Feature behavior is still not release-ready.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_final_review(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    repair_tasks = [item for item in payload["items"] if item.get("task_kind") == "repair"]
    final = next(item for item in payload["items"] if item["id"] == "T-FINAL")
    assert [item["id"] for item in repair_tasks] == ["T003", "T004", "T005"]
    assert "maximum repair iterations (3)" in final["blocked_reason"]
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] is None
    assert runtime_state["repair_candidates"] == ["T002"]
    assert runtime_state["final_repair_limit_reached"] is True
    assert runtime_state["final_verify_status"] == "blocked"


def test_final_repair_preserves_verified_prefinal_audit_and_updates_final_dependency() -> None:
    loop_module = import_module("delivery.loop")
    tasks = [
        Task.from_dict({"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []}),
        Task.from_dict({"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]}),
        Task.from_dict({"id": "T003", "title": "Final Verification Repair Bundle", "status": "pending", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000", "T002", PREFINAL_AUDIT_TASK_ID], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]}),
        Task.from_dict({"id": PREFINAL_AUDIT_TASK_ID, "title": "Pre-final full-system audit", "status": "verified", "task_kind": "audit", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS), "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS), "git_commit": "abc", "review_status": "pass"}),
        Task.from_dict({"id": FINAL_VERIFY_TASK_ID, "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": [PREFINAL_AUDIT_TASK_ID], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]}),
    ]

    updated = loop_module._invalidate_prefinal_audit_after_repair(tasks, "T003")
    audit = next(task for task in updated if task.id == PREFINAL_AUDIT_TASK_ID)
    final = next(task for task in updated if task.id == FINAL_VERIFY_TASK_ID)

    assert audit.status == "verified"
    assert audit.git_commit == "abc"
    assert audit.review_status == "pass"
    assert audit.blocked_reason is None
    assert final.dependencies == [PREFINAL_AUDIT_TASK_ID, "T003"]


def test_final_verify_creates_environment_repair_for_environment_failures(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop import DeliveryLoop
    from delivery.verify import TestFailure, TestResult

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["frontend/e2e/feature.spec.ts"], "output_paths": ["frontend/src/feature.tsx"]},
                {"id": PREFINAL_AUDIT_TASK_ID, "title": "Pre-final full-system audit", "status": "verified", "task_kind": "audit", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": list(PREFINAL_AUDIT_OUTPUT_TESTS), "output_paths": list(PREFINAL_AUDIT_OUTPUT_PATHS)},
                {"id": FINAL_VERIFY_TASK_ID, "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [PREFINAL_AUDIT_TASK_ID], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    result = TestResult(
        task_id="T002",
        timestamp="2026-06-24T01:00:00Z",
        test_files=["frontend/e2e/feature.spec.ts"],
        test_types=["browser", "e2e"],
        requirement_ids=["REQ-001"],
        passed=False,
        passed_count=0,
        failed_count=1,
        failures=[TestFailure("frontend/e2e/feature.spec.ts", "test environment not ready (browser_e2e): shared test services did not become healthy", "{}", failure_kind="environment_not_ready")],
        attempt=1,
    )
    monkeypatch.setattr("delivery.loop.run_full_suite", lambda project_root, mode="all": [result])
    monkeypatch.setattr("delivery.loop.refresh_gates", lambda project_root: {"gates": []})
    monkeypatch.setattr("delivery.loop.check_requirements_coverage", lambda project_root, requirements_payload: {"total": 1, "covered": 1, "uncovered": [], "all_covered": True})
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])
    monkeypatch.setattr("delivery.loop.render_release_evidence", lambda project_root, summary, requirement_coverage, missing_test_types: docs_dir / "release-evidence.md")
    monkeypatch.setattr("delivery.loop.write_final_repair_report", lambda project_root, results, requirement_coverage, missing_test_types, repair_candidates: "docs/reviews/final-repair-report.md")

    payload = DeliveryLoop(tmp_path, runtime="opencode").final_verify()

    assert payload["status"] == "environment_blocked"
    items = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))["items"]
    repair = next(item for item in items if item["title"] == "Validation Environment Repair Bundle")
    assert payload["repair_candidates"] == [repair["id"]]
    assert repair["task_kind"] == "repair"
    assert repair["output_tests"] == ["frontend/e2e/feature.spec.ts"]
    assert "docker-compose.yml" in repair["output_paths"] or "docs/reviews/final-repair-report.md" in repair["output_paths"]
    final_task = next(item for item in items if item["id"] == FINAL_VERIFY_TASK_ID)
    assert final_task["status"] == "blocked"
    assert "environment repair task" in final_task["blocked_reason"]


def test_default_python_react_environment_repair_scope_is_explicit_stack_heuristic(tmp_path: Path) -> None:
    from delivery.environment_repair import default_python_react_environment_repair_scope_paths

    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "playwright.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core").mkdir(parents=True)
    (tmp_path / "backend" / ".env").write_text("DATABASE_URL=sqlite://\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    paths = default_python_react_environment_repair_scope_paths(tmp_path)

    assert "docker-compose.yml" in paths
    assert "frontend/playwright.config.ts" in paths
    assert "backend/.env" in paths


def test_cmd_verify_rejects_full_verify_when_non_final_tasks_are_incomplete(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T001",
                    "title": "Shared infra",
                    "status": "active",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/src/tests/test_health.py"],
                    "output_paths": ["backend/src/core/"],
                },
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": [],
                    "output_paths": [],
                },
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_verify(argparse.Namespace(project=str(tmp_path), runtime=None, mode="all", _locked=False))

    assert exc_info.value.code == "verify_project_incomplete"


def test_cmd_verify_non_verified_mode_allows_diagnostic_run_on_incomplete_project(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T001",
                    "title": "Shared infra",
                    "status": "active",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/src/tests/test_health.py"],
                    "output_paths": ["backend/src/core/"],
                },
                {
                    "id": "T-FINAL",
                    "title": "最终验证",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": [],
                    "output_paths": [],
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.__main__.DeliveryLoop.final_verify",
        lambda self, suite_mode="all": {"passed": False, "status": "blocked", "suite_mode": suite_mode},
    )

    result = cli.cmd_verify(argparse.Namespace(project=str(tmp_path), runtime=None, mode="non-verified", _locked=False))

    assert result == 1


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
    assert summary_json["code_lines"]["total"] == 5
    assert summary_json["code_lines"]["backend"] == 2
    assert summary_json["code_lines"]["frontend"] == 3
    summary_md = (docs_dir / "project-summary.md").read_text(encoding="utf-8")
    assert "- Effective Code Lines: 5" in summary_md
    assert "- Backend Code Lines: 2" in summary_md
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature/"]},
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


def test_project_execution_guard_prunes_dead_pid_lock(tmp_path: Path, monkeypatch) -> None:
    lock_dir = tmp_path / ".app-delivery-runtime" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "execution.lock"
    lock_path.write_text('{"pid":999999,"heartbeat_at":"2026-06-24T00:00:00Z"}\n', encoding="utf-8")

    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False if pid == 999999 else True)

    removed = cli._prune_stale_execution_lock(tmp_path)

    assert removed is True
    assert not lock_path.exists()


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


def test_cmd_context_sync_accepts_runtime_specific_context_only(tmp_path: Path) -> None:
    input_path = tmp_path / "context.json"
    input_path.write_text(
        json.dumps(
            {
                "claude_md": "claude\n",
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
    assert "claude\n" in claude_text
    assert "## Runtime Baseline" in claude_text
    assert "docs/requirements-source.md is the raw requirements record" in claude_text
    assert "framework artifacts for planning and traceability" in claude_text
    assert "do not guess APIs from memory" in claude_text
    assert "Backend Python commands must use the project environment" in claude_text
    assert "Do not run project code with shell-default `python`, `python3`, or `pip`" in claude_text
    assert not (tmp_path / "CODE_MAP.md").exists()
    assert (tmp_path / "docs" / "test-plan.json").exists()
    persisted = json.loads((tmp_path / ".app-delivery-runtime" / "stage-inputs" / "project-context-sync.json").read_text(encoding="utf-8"))
    assert persisted["test_plan"] == {"schema_version": "1", "coverage": []}


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


def test_cmd_arch_design_rejects_backend_root_package_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   ├── api/\n│   └── agents/\n└── frontend/\n```\n",
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
    assert "backend/src" in exc_info.value.message


def test_cmd_arch_design_rejects_top_level_mcp_server_paths(tmp_path: Path) -> None:
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n├── mcp-server/\n│   └── tools/\n└── frontend/\n```\n",
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
    assert "unsupported extra service root" in exc_info.value.message


def test_cmd_arch_design_ignores_backend_root_examples_outside_module_tree(tmp_path: Path) -> None:
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\nThis prose mentions backend/api as legacy input text only.\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n│       └── api/\n└── frontend/\n```\n",
                "shared_components_md": "shared\n",
                "ui_required": True,
                "modules": [],
                "adrs": [],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0


def test_cmd_arch_design_rejects_implementation_file_inventory(tmp_path: Path) -> None:
    file_rows = "\n".join(f"│       ├── generated_{idx}.py" for idx in range(25))
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": f"# Architecture\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n{file_rows}\n└── frontend/\n```\n",
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
    assert "scaffold skeleton" in exc_info.value.message