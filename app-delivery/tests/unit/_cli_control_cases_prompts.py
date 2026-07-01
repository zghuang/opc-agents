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

def test_render_arch_design_prompt_includes_project_dependency_hints(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "dependency_hints": [
                    {"ecosystem": "backend", "name": "LangGraph", "reason": "Agent orchestration"},
                ]
            }
        ),
        encoding="utf-8",
    )

    prompt = render_skill_prompt("arch-design.md", project=str(tmp_path), requirements_json="{}", clarification_answers_md="")

    assert "Project technology constraints:" in prompt
    assert "LangGraph" in prompt

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

    assert "Use declared output paths/tests as the main contract" in prompt
    assert "Green tests are not enough" in prompt
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
    assert "Use these technologies unless an ADR or clarification supersedes them." in task_prompt
    assert "PostgreSQL" not in task_prompt
    assert "Project technology constraints to verify:" in review_prompt
    assert "LangGraph (backend): Agent orchestration framework" in review_prompt
    assert "Require implementation evidence or an explicit superseding ADR/clarification." in review_prompt
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
    assert "For shared foundation, install only dependencies used by this task" in prompt
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

def test_cmd_arch_design_rejects_missing_dependency_hints(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "dependency_hints": [
                    {"ecosystem": "backend", "name": "LangGraph", "reason": "Agent orchestration"},
                ]
            }
        ),
        encoding="utf-8",
    )
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Tech Design\n\nNo agent framework named.\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n└── frontend/\n```\n",
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
    assert "dependency_hints" in exc_info.value.message
    assert exc_info.value.details["missing_dependency_hints"] == ["LangGraph"]

def test_cmd_arch_design_accepts_dependency_hints_in_architecture_docs(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "dependency_hints": [
                    {"ecosystem": "backend", "name": "LangGraph", "reason": "Agent orchestration"},
                ]
            }
        ),
        encoding="utf-8",
    )
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Tech Design\n\nAgent orchestration uses LangGraph.\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n└── frontend/\n```\n",
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

def test_cmd_arch_design_rejects_adr_documents_in_modules(tmp_path: Path) -> None:
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n└── frontend/\n```\n",
                "shared_components_md": "shared\n",
                "ui_required": True,
                "modules": [
                    {
                        "path": "docs/modules/ADR-001-langgraph.md",
                        "content": "# ADR-001: Use LangGraph\n\n## Context\nNeed orchestration.\n\n## Decision\nUse LangGraph.\n\n## Consequences\nStateful graph runtime.\n",
                    }
                ],
                "adrs": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "input_invalid_shape"
    assert "modules[]" in exc_info.value.message
    assert exc_info.value.details["invalid_module_docs"] == ["docs/modules/ADR-001-langgraph.md"]

def test_cmd_arch_design_accepts_distinct_module_docs_and_adrs(tmp_path: Path) -> None:
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Module Architecture\n\n```text\nproject/\n├── backend/\n│   └── src/\n└── frontend/\n```\n",
                "shared_components_md": "shared\n",
                "ui_required": True,
                "modules": [
                    {
                        "path": "docs/modules/agent-orchestration.md",
                        "content": "# Agent Orchestration Module\n\n## Responsibilities\nCoordinate agent execution.\n\n## Public Interfaces\nOrchestrator service.\n",
                    }
                ],
                "adrs": [
                    {
                        "path": "docs/adr/ADR-001-langgraph.md",
                        "content": "# ADR-001: Use LangGraph\n\n## Context\nNeed orchestration.\n\n## Decision\nUse LangGraph.\n\n## Consequences\nStateful graph runtime.\n",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    assert (tmp_path / "docs" / "modules" / "agent-orchestration.md").exists()
    assert (tmp_path / "docs" / "adr" / "ADR-001-langgraph.md").exists()

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
    assert exc_info.value.details["max_implementation_file_entries"] == 20
    assert exc_info.value.details["implementation_file_entry_count"] == 25
    assert "generated_0.py" in exc_info.value.details["implementation_file_examples"]

def test_cmd_arch_design_allows_many_scaffold_config_files_in_module_tree(tmp_path: Path) -> None:
    file_rows = "\n".join(
        [
            "│   ├── pyproject.toml",
            "│   ├── uv.lock",
            "│   ├── package.json",
            "│   ├── package-lock.json",
            "│   ├── pnpm-lock.yaml",
            "│   ├── yarn.lock",
            "│   ├── tsconfig.json",
            "│   ├── vite.config.ts",
            "│   ├── playwright.config.ts",
            "│   ├── docker-compose.yml",
            "│   ├── Dockerfile",
            "│   ├── README.md",
            "│   ├── .env.example",
            "│   ├── go.mod",
            "│   ├── go.sum",
            "│   ├── Cargo.toml",
            "│   ├── Cargo.lock",
            "│   ├── pom.xml",
            "│   ├── build.gradle",
            "│   ├── settings.gradle",
            "│   ├── .gitkeep",
            "│   └── __init__.py",
        ]
    )
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

    result = cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
