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

def test_final_review_prompt_includes_deferred_semantic_risks(tmp_path: Path) -> None:
    from delivery.review_prompts import build_final_review_request

    risk_path = tmp_path / "docs" / "reviews" / "semantic-risk-register.json"
    risk_path.parent.mkdir(parents=True, exist_ok=True)
    risk_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "risks": [
                    {
                        "task_id": "T008",
                        "categories": ["production-stub"],
                        "repeat_count": 2,
                        "review_artifact": "docs/reviews/code-review-T008.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    prompt = build_final_review_request(
        tmp_path,
        results_summary="All tests passed.",
        requirement_coverage={"total": 1, "covered": 1, "uncovered": []},
        missing_test_types=[],
    )

    assert "Deferred semantic risks:" in prompt
    assert "T008: production-stub" in prompt
    assert "final release review must decide" in prompt

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
    assert "Selected stack contract:" in prompt
    assert "does not prescribe a backend package directory" in prompt
    assert "Backend implementation code lives under `backend/src" not in prompt


def test_render_stage_prompts_include_layout_neutral_python_react_guidance() -> None:
    for prompt_name in ["arch-design.md", "context-sync.md", "decompose.md"]:
        prompt = render_skill_prompt(prompt_name, project="")
        assert "Python React Stack Contract" in prompt
        assert "does not prescribe a backend package directory" in prompt
        assert "explicit FastAPI application lifecycle setup and cleanup" in prompt
        assert "backend/src" not in prompt

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

    task = Task.from_dict(
        {
            "id": "T015",
            "title": "Orchestrator incident workflow",
            "status": "pending",
            "requirements": ["REQ-017"],
            "acceptance_scenarios": [],
            "dependencies": ["T013"],
            "output_tests": ["backend/tests/agents/test_orchestrator_graph.py"],
            "output_paths": ["backend/src/agents/graph.py", "backend/src/agents/orchestrator.py"],
            "intent": {
                "objective": "Stateful incident orchestration workflow",
            },
            "technology_constraints": [
                {
                    "name": "LangGraph",
                    "ecosystem": "backend",
                    "requirement": "must_use",
                    "reason": "ADR 001 selects LangGraph for stateful multi-agent orchestration.",
                    "source": "docs/adr/001-agent-framework.md",
                    "expected_evidence": [
                        "backend dependency manifest includes langgraph",
                        "workflow implementation imports LangGraph primitives",
                    ],
                }
            ],
        }
    )

    task_prompt = build_task_prompt(tmp_path, task)
    review_prompt = build_code_review_request(tmp_path, task)

    assert "Required technology constraints for this task:" in task_prompt
    assert "LangGraph (backend, must_use): ADR 001 selects LangGraph" in task_prompt
    assert "backend dependency manifest includes langgraph" in task_prompt
    assert "docs/adr/001-agent-framework.md" in task_prompt
    assert "PostgreSQL" not in task_prompt
    assert "Technology constraints to verify:" in review_prompt
    assert "LangGraph (backend, must_use): ADR 001 selects LangGraph" in review_prompt
    assert "technology_assessment" in review_prompt
    assert "A `must_use` constraint without implementation evidence" in review_prompt
    assert "PostgreSQL" not in review_prompt

def test_task_prompt_surfaces_invalid_verified_upstream_evidence_without_reopening(tmp_path: Path) -> None:
    from delivery.loop_task_prompt import build_task_prompt

    e2e_dir = tmp_path / "frontend" / "e2e"
    e2e_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir = tmp_path / "docs" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "code-review-T005.md").write_text("status: pass\n", encoding="utf-8")
    (reviews_dir / "test-report-T005.md").write_text("status: pass\n", encoding="utf-8")
    (e2e_dir / "dashboard.spec.ts").write_text(
        """
import { test } from '@playwright/test'

test('dashboard', async ({ page }) => {
  await page.route('**/api/dashboard', route => route.fulfill({ status: 200, json: {} }))
})
""".strip()
        + "\n",
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
                {"id": "T005", "title": "Dashboard", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/dashboard.spec.ts"], "output_paths": ["frontend/src/routes/dashboard.tsx"], "review_status": "pass", "review_artifact": "docs/reviews/code-review-T005.md", "git_commit": "abc"},
                {"id": "T010", "title": "Approval workflow", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T005"], "output_tests": ["backend/tests/test_approvals.py"], "output_paths": ["backend/demo_domain/api/approvals.py"]},
            ],
        },
    )

    prompt = build_task_prompt(tmp_path, Task.from_dict(json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))["items"][1]))

    assert "Verified upstream evidence issues:" in prompt
    assert "T005:" in prompt
    assert ".app-delivery-runtime/prompts/T005.md" in prompt
    assert "docs/reviews/code-review-T005.md" in prompt
    assert "docs/reviews/test-report-T005.md" in prompt
    assert "Do not reopen, reset, or re-run verified feature tasks" in prompt
    assert "dedicated repair bundle" in prompt

    repair_task = Task.from_dict(
        {
            "id": "T010",
            "title": "Approval workflow",
            "status": "pending",
            "review_status": "changes_requested",
            "review_artifact": "docs/reviews/code-review-T010.md",
            "requirements": ["REQ-002"],
            "acceptance_scenarios": [],
            "dependencies": ["T005"],
            "output_tests": ["backend/tests/test_approvals.py"],
            "output_paths": ["backend/demo_domain/api/approvals.py"],
        }
    )
    repair_prompt = build_task_prompt(tmp_path, repair_task)

    assert "Review repair mode:" in repair_prompt
    assert "Verified upstream evidence issues:" in repair_prompt
    assert "Do not reopen, reset, or re-run verified feature tasks" in repair_prompt

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
    assert "production-semantic-scan.md" in prompt
    assert "production-path hardcoded/default responses" in prompt
    assert "do not recommend release readiness" in prompt
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
    assert "Do not make missing frontend endpoints pass by adding production-path stub" in prompt
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
        resolve_project_root("/path/to/legacy-opc-runtime")

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
                        "technology_constraints": [],
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

def test_cmd_decompose_allows_architecture_selected_backend_root_package_paths(tmp_path: Path) -> None:
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
                    {"title": "Feature", "task_kind": "feature", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "technology_constraints": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/api/feature.py"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    payload = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))
    assert any("backend/api/feature.py" in item.get("output_paths", []) for item in payload["items"])


def test_cmd_decompose_requires_ui_route_mapping_coverage(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-055", "title": "Control Tower", "summary": "Control tower home"}], "acceptance_scenarios": []}), encoding="utf-8")
    ui_dir = docs_dir / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "page-archetypes.md").write_text(
        "## Route Mapping\n\n"
        "| Page / Route | Source Requirements | Primary Roles | Required Regions / Components | States | Suggested Output Paths | Suggested Browser Tests |\n"
        "|--------------|---------------------|---------------|--------------------------------|--------|------------------------|-------------------------|\n"
        "| Control Tower | REQ-055 | Domain Metric Commander | KPI cards, heatmap | loading, empty, error | frontend/src/pages/ControlTower/ | frontend/e2e/control-tower.spec.ts |\n",
        encoding="utf-8",
    )
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {"tier": "S", "rationale": "Small", "signals": {}},
                "validation_gates": [],
                "items": [
                    {"title": "Control Tower", "task_kind": "feature", "requirements": ["REQ-055"], "acceptance_scenarios": [], "dependencies": [], "technology_constraints": [], "output_tests": ["frontend/e2e/control-tower.spec.ts"], "output_paths": ["frontend/src/pages/ControlTower/"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0


def test_cmd_decompose_rejects_uncovered_ui_route_mapping(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-055", "title": "Control Tower", "summary": "Control tower home"}], "acceptance_scenarios": []}), encoding="utf-8")
    ui_dir = docs_dir / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "page-archetypes.md").write_text(
        "## Route Mapping\n\n"
        "| Page / Route | Source Requirements | Primary Roles | Required Regions / Components | States | Suggested Output Paths | Suggested Browser Tests |\n"
        "|--------------|---------------------|---------------|--------------------------------|--------|------------------------|-------------------------|\n"
        "| Control Tower | REQ-055 | Domain Metric Commander | KPI cards, heatmap | loading, empty, error | frontend/src/pages/ControlTower/ | frontend/e2e/control-tower.spec.ts |\n",
        encoding="utf-8",
    )
    input_path = tmp_path / "decompose.json"
    input_path.write_text(
        json.dumps(
            {
                "delivery_complexity": {"tier": "S", "rationale": "Small", "signals": {}},
                "validation_gates": [],
                "items": [
                    {"title": "Backend only", "task_kind": "feature", "requirements": ["REQ-055"], "acceptance_scenarios": [], "dependencies": [], "technology_constraints": [], "output_tests": ["backend/tests/test_control_tower.py"], "output_paths": ["backend/api/control_tower.py"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryError) as exc_info:
        cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert exc_info.value.code == "stage_output_invalid"
    assert "UI route mappings" in exc_info.value.message

def test_cmd_decompose_rejects_items_missing_technology_constraints(tmp_path: Path) -> None:
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

    assert exc_info.value.code == "input_invalid_shape"
    assert "technology_constraints" in exc_info.value.message

def test_cmd_decompose_allows_architecture_selected_top_level_mcp_server_paths(tmp_path: Path) -> None:
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
                        "technology_constraints": [],
                        "output_tests": ["mcp-server/tests/test_tools.py"],
                        "output_paths": ["mcp-server/server.py", "mcp-server/tools/rules.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.cmd_decompose(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    payload = json.loads((docs_dir / "work-items.json").read_text(encoding="utf-8"))
    assert any("mcp-server/server.py" in item.get("output_paths", []) for item in payload["items"])

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
                        "technology_constraints": [],
                        "output_tests": ["tests/test_feature.py"],
                        "output_paths": ["backend/src/feature.py"],
                    },
                    {
                        "title": "Auth domain validation",
                        "task_kind": "validation",
                        "requirements": ["REQ-001"],
                        "acceptance_scenarios": [],
                        "dependencies": ["T002"],
                        "technology_constraints": [],
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
                        "technology_constraints": [],
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
    assert "read the matching section of `docs/requirements-source.md` before coding or reviewing" in claude_text
    assert "follow `docs/requirements-source.md` for endpoints, methods, request/response fields, data models, protocols" in claude_text
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


def test_cmd_arch_design_accepts_dynamic_module_architecture_tree_formats(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Module Architecture\n\n```tree\nbackend/\n  app/\n    models/\nfrontend/\n  src/\n    pages/\n```\n",
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
    assert (docs_dir / "architecture.md").exists()


def test_cmd_arch_design_accepts_unfenced_markdown_module_architecture_tree(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "arch.json"
    input_path.write_text(
        json.dumps(
            {
                "architecture_md": "# Architecture\n\n## Module Architecture\n\n- project/\n  - backend/\n    - app/\n  - frontend/\n    - src/\n\n## Data Model\n\nNo scaffold paths here.\n",
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
    assert (docs_dir / "architecture.md").exists()


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

def test_cmd_arch_design_allows_architecture_selected_backend_package_tree(tmp_path: Path) -> None:
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

    result = cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    assert (tmp_path / "docs" / "architecture.md").exists()

def test_cmd_arch_design_allows_explicit_top_level_mcp_server_paths(tmp_path: Path) -> None:
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

    result = cli.cmd_arch_design(argparse.Namespace(project=str(tmp_path), input=str(input_path)))

    assert result == 0
    assert (tmp_path / "docs" / "architecture.md").exists()

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

def test_cmd_arch_design_allows_limited_implementation_file_anchors(tmp_path: Path) -> None:
    file_rows = "\n".join(f"│       ├── anchor_{idx}.py" for idx in range(50))
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

def test_cmd_arch_design_rejects_implementation_file_inventory(tmp_path: Path) -> None:
    file_rows = "\n".join(f"│       ├── generated_{idx}.py" for idx in range(55))
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
    assert exc_info.value.details["max_implementation_file_entries"] == 50
    assert exc_info.value.details["implementation_file_entry_count"] == 55
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
