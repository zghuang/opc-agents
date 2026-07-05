from __future__ import annotations

import json
from pathlib import Path

import pytest

from delivery.builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from delivery.state import save_gates, save_test_plan, save_test_results, save_work_items
from delivery.task import Task, check_requirements_coverage, check_test_type_coverage, decompose_tasks, lint_task_contract, mark_task, pick_next_task, referenced_req_ids, reset_task
from delivery.task import _validate_task_shape
from delivery.gates import normalize_complexity_override, normalize_stage_gates, refresh_gates, validate_validation_tasks, validate_gate_references


def test_task_decompose_prompt_flags_mixed_capability_surfaces() -> None:
    from delivery.skill_prompts import render_skill_prompt

    prompt = render_skill_prompt(
        "decompose.md",
        requirements_json="{}",
        architecture_md="",
        shared_components_md="",
        architecture_meta_json="{}",
        test_plan_json="{}",
        clarification_answers_md="",
    )

    assert "multiple independently testable capability surfaces" in prompt
    assert "Coherence is semantic" in prompt
    assert "Every `done_when` item should map to task-owned `output_tests`" in prompt
    assert "ingestion + normalization" not in prompt
    assert "numeric limits" not in prompt


def test_decompose_tasks_inserts_builtin_foundations(tmp_path: Path) -> None:
    payload = decompose_tasks(
        tmp_path,
        [{"title": "用户注册", "requirements": ["REQ-001"], "output_tests": ["tests/auth/test_register.py"], "output_paths": ["backend/src/auth/"]}],
        include_shared_foundation=True,
    )
    ids = [item["id"] for item in payload["items"]]
    assert ids[:3] == ["T000", "T001", "T002"]
    assert payload["items"][0]["title"] == "Scaffold"
    assert payload["items"][1]["title"] == "Shared foundation"
    assert ids[-2:] == [PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID]
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"][:3] == ["T000", "T001", "T002"]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == [PREFINAL_AUDIT_TASK_ID]
    assert payload["items"][1]["output_paths"] == [
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/tests/",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/pnpm-lock.yaml",
        "frontend/yarn.lock",
        "frontend/src/lib/",
        "frontend/src/styles/",
        "frontend/src/App.tsx",
        "frontend/src/App.test.tsx",
        "frontend/e2e/",
    ]
    assert payload["items"][1]["intent"]["objective"].startswith("Create the minimal shared")
    assert "Do not preinstall project-wide technology packages" in payload["items"][1]["intent"]["non_goals"][-1]

def test_decompose_tasks_preserves_normalized_task_intent(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Case workspace",
                "requirements": ["REQ-001"],
                "output_tests": ["backend/tests/test_case.py"],
                "output_paths": ["backend/src/case/"],
                "intent": {
                    "objective": "  Enable case review.  ",
                    "journey": "User opens a case and sees current state.",
                    "done_when": ["Case API works", "Case API works", "Case test passes"],
                    "non_goals": ["Do not build unrelated dashboards", ""],
                    "ignored": "value",
                },
            }
        ],
        include_shared_foundation=True,
    )

    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["intent"] == {
        "objective": "Enable case review.",
        "journey": "User opens a case and sees current state.",
        "done_when": ["Case API works", "Case test passes"],
        "non_goals": ["Do not build unrelated dashboards"],
    }

def test_decompose_tasks_ignores_replayed_builtin_items(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    payload = decompose_tasks(
        tmp_path,
        [
            {"id": "T000", "title": "脚手架", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["docs/project-structure.md"]},
            {"id": "T001", "title": "共享基础设施", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/main.py"]},
            {"id": "T002", "title": "Feature", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
            {"id": "T-FINAL", "title": "最终验证", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md"]},
        ],
        include_shared_foundation=True,
    )

    ids = [item["id"] for item in payload["items"]]
    assert ids == ["T000", "T001", "T002", PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID]
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["title"] == "Feature"
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002"]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == [PREFINAL_AUDIT_TASK_ID]

def test_decompose_tasks_does_not_insert_frontend_api_audit_for_frontend_only_scope(tmp_path: Path) -> None:
    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Static marketing page",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": ["AS-001"],
                "output_tests": ["frontend/e2e/static-page.spec.ts"],
                "output_paths": ["frontend/src/pages/StaticPage.tsx", "frontend/src/api/client.ts"],
            }
        ],
        include_shared_foundation=True,
    )

    ids = [item["id"] for item in payload["items"]]

    assert FRONTEND_API_AUDIT_TASK_ID not in ids
    assert ids[-2:] == [PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID]

def test_decompose_tasks_inserts_frontend_api_audit_for_python_react_stack(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"stack": "python-react"}), encoding="utf-8")

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Incident workspace UI",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": ["AS-001"],
                "output_tests": ["frontend/e2e/incident.spec.ts"],
                "output_paths": ["frontend/src/pages/IncidentWorkspace.tsx"],
            }
        ],
        include_shared_foundation=True,
    )

    by_id = {item["id"]: item for item in payload["items"]}

    assert FRONTEND_API_AUDIT_TASK_ID in by_id
    assert by_id[FRONTEND_API_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002"]
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002", FRONTEND_API_AUDIT_TASK_ID]

def test_decompose_tasks_inserts_frontend_api_audit_for_frontend_with_backend_api_scope(tmp_path: Path) -> None:
    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Incident workspace",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": ["AS-001"],
                "output_tests": ["frontend/e2e/incident-flow.spec.ts", "backend/tests/test_api/test_incidents.py"],
                "output_paths": ["frontend/src/pages/IncidentWorkspace.tsx", "backend/src/api/incidents.py"],
            }
        ],
        include_shared_foundation=True,
    )

    by_id = {item["id"]: item for item in payload["items"]}

    assert FRONTEND_API_AUDIT_TASK_ID in by_id
    assert by_id[FRONTEND_API_AUDIT_TASK_ID]["task_kind"] == "audit"
    assert by_id[FRONTEND_API_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002"]
    assert by_id[FRONTEND_API_AUDIT_TASK_ID]["output_paths"][-1] == FRONTEND_API_AUDIT_REPORT_PATH
    production_gate_ids = [item["id"] for item in payload["items"] if str(item["title"]).startswith("Production Gate:")]
    assert production_gate_ids
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002", FRONTEND_API_AUDIT_TASK_ID, *production_gate_ids]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == [PREFINAL_AUDIT_TASK_ID]

def test_decompose_tasks_adds_dynamic_production_gates_for_matching_project(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "RBAC", "summary": "Users require role permissions and site data isolation."},
                    {"id": "REQ-002", "title": "Agent", "summary": "AI agent orchestrator must call model tools."},
                    {"id": "REQ-003", "title": "Execution", "summary": "Approved actions dispatch to external systems with rollback."},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "frontend").mkdir(parents=True)
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True)

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Full-stack workflow",
                "requirements": ["REQ-001", "REQ-002", "REQ-003"],
                "acceptance_scenarios": [],
                "output_tests": ["backend/tests/test_workflow.py", "frontend/e2e/workflow.spec.ts"],
                "output_paths": ["backend/src/api/workflow.py", "backend/src/agents/workflow.py", "frontend/src/pages/Workflow.tsx"],
            }
        ],
        include_shared_foundation=True,
    )

    titles = [item["title"] for item in payload["items"]]
    assert "Production Gate: Real backend E2E validation" in titles
    assert "Production Gate: Security and access control enforcement" in titles
    assert "Production Gate: Real agent integration" in titles
    assert "Production Gate: Approval and execution loop" in titles
    assert payload["items"][-2]["id"] == PREFINAL_AUDIT_TASK_ID
    assert payload["items"][-1]["id"] == FINAL_VERIFY_TASK_ID

def test_decompose_tasks_does_not_add_agent_gate_without_agent_requirements(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "Orders", "summary": "Users manage orders."}], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True)

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Order API",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "output_tests": ["backend/tests/test_orders.py"],
                "output_paths": ["backend/src/api/orders.py"],
            }
        ],
        include_shared_foundation=True,
    )

    titles = [item["title"] for item in payload["items"]]
    assert "Production Gate: Real agent integration" not in titles

def test_decompose_tasks_preserves_architecture_selected_backend_app_contract_paths(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Backend core",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": ["T001"],
                "output_tests": ["backend/tests/unit/core/test_config.py"],
                "output_paths": [
                    "backend/app/core/config.py",
                    "backend/app/domain/models.py",
                    "backend/app/main.py",
                ],
            }
        ],
        include_shared_foundation=True,
    )

    task = payload["items"][2]
    assert task["output_paths"] == [
        "backend/app/core/config.py",
        "backend/app/domain/models.py",
        "backend/app/main.py",
    ]

def test_decompose_tasks_rejects_companion_mock_server_contract_paths(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    try:
        decompose_tasks(
            tmp_path,
            [
                {
                    "title": "Mock contracts",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": ["companion/mock-server/tests/all_mock_contracts.py"],
                    "output_paths": ["companion/mock-server/routers/erp.py"],
                }
            ],
            include_shared_foundation=True,
        )
    except ValueError as exc:
        assert "non-canonical mock-server paths" in str(exc)
        assert "use project-root mock-server/..." in str(exc)
    else:
        raise AssertionError("expected companion/mock-server paths to be rejected")

def test_decompose_tasks_allows_architecture_selected_top_level_mcp_server_contract_paths(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "MCP tools",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": ["T001"],
                "output_tests": ["mcp-server/tests/test_tools.py"],
                "output_paths": ["mcp-server/server.py", "mcp-server/tools/rules.py"],
            }
        ],
        include_shared_foundation=True,
    )

    task = next(item for item in payload["items"] if item["title"] == "MCP tools")
    assert task["output_paths"] == ["mcp-server/server.py", "mcp-server/tools/rules.py"]

def test_normalize_complexity_override_requires_task_decompose_metadata() -> None:
    payload = normalize_complexity_override(
        {
            "tier": "L",
            "rationale": "Multi-domain project with milestone validation needs.",
            "signals": {"estimated_loc": 80000, "estimated_tasks": 18},
        },
    )

    assert payload["tier"] == "L"
    assert payload["source"] == "task-decompose"
    assert payload["signals"]["estimated_tasks"] == 18


def test_normalize_complexity_override_rejects_understated_tier() -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_complexity_override(
            {
                "tier": "S",
                "rationale": "Incorrectly marked small.",
                "signals": {"estimated_loc": 120000, "estimated_modules": 14, "estimated_tasks": 16},
            }
        )

    assert "minimum expected tier is L" in str(exc_info.value)

def test_normalize_stage_gates_keeps_simple_task_decompose_gate_shape() -> None:
    gates = normalize_stage_gates(
        [
            {
                "id": "GATE-domain-auth",
                "kind": "module",
                "title": "Module gate: auth",
                "scope_tasks": ["T002", "T003"],
                "scope_requirements": ["REQ-001"],
                "required_test_types": ["api", "browser"],
            }
        ]
    )

    assert gates == [
        {
            "id": "GATE-domain-auth",
            "kind": "module",
            "title": "Module gate: auth",
            "status": "pending",
            "scope_tasks": ["T002", "T003"],
            "scope_requirements": ["REQ-001"],
            "required_test_types": ["api", "browser"],
            "source": "task-decompose",
        }
    ]

def test_validate_validation_tasks_requires_validation_task_for_m_plus() -> None:
    tasks = [
        Task("T002", "Feature", "pending", ["REQ-001"], [], ["T001"], ["backend/tests/test_feature.py"], ["backend/src/feature.py"], task_kind="feature"),
    ]

    try:
        validate_validation_tasks(tasks, {"tier": "M"})
    except ValueError as exc:
        assert "validation task" in str(exc)
    else:
        raise AssertionError("expected M+ projects to require a validation task")

def test_validate_gate_references_rejects_unknown_scope_task() -> None:
    tasks = [
        Task("T002", "Feature", "pending", ["REQ-001"], [], ["T001"], ["backend/tests/test_feature.py"], ["backend/src/feature.py"], task_kind="feature"),
        Task("T003", "Validation", "pending", ["REQ-001"], [], ["T002"], ["backend/tests/test_feature.py"], ["docs/reviews/test-report-feature.md"], task_kind="validation"),
    ]

    try:
        validate_gate_references(tasks, [{"id": "GATE-x", "scope_tasks": ["T999"]}])
    except ValueError as exc:
        assert "unknown task id" in str(exc)
    else:
        raise AssertionError("expected invalid gate scope task reference to fail")

def test_validate_gate_references_rejects_unscoped_gate() -> None:
    tasks = [
        Task("T002", "Feature", "pending", ["REQ-001"], [], ["T001"], ["backend/tests/test_feature.py"], ["backend/src/feature.py"], task_kind="feature"),
    ]

    try:
        validate_gate_references(tasks, [{"id": "GATE-x", "scope_tasks": [], "scope_requirements": []}])
    except ValueError as exc:
        assert "scope_tasks or scope_requirements" in str(exc)
    else:
        raise AssertionError("expected unscoped gate reference to fail")

def test_lint_task_contract_reports_missing_outputs() -> None:
    lint = lint_task_contract(
        Task(
            "T010",
            "Broken",
            "pending",
            ["REQ-001"],
            ["AS-001"],
            [],
            [],
            [],
        )
    )

    assert any("output_paths" in message for message in lint["errors"])
    assert any("output_tests" in message for message in lint["errors"])

def test_lint_task_contract_warns_when_frontend_paths_lack_frontend_tests() -> None:
    lint = lint_task_contract(
        Task(
            "T011",
            "Frontend",
            "pending",
            ["REQ-001"],
            [],
            [],
            ["backend/tests/test_backend.py"],
            ["frontend/src/features/demo/pages/DemoPage.tsx"],
        )
    )

    assert lint["errors"] == []
    assert any("frontend paths" in message for message in lint["warnings"])

def test_lint_task_contract_rejects_backend_root_relative_shortcuts() -> None:
    lint = lint_task_contract(
        Task(
            "T012",
            "Bad paths",
            "pending",
            ["REQ-001"],
            [],
            [],
            ["../tests/auth/test_login.py"],
            ["../src/auth/routes.py"],
        )
    )

    assert any("project-root-relative" in message for message in lint["errors"])

def test_lint_task_contract_allows_safe_project_root_relative_paths_outside_fixed_roots() -> None:
    lint = lint_task_contract(
        Task(
            "T013",
            "Observability",
            "pending",
            ["REQ-001"],
            [],
            [],
            ["backend/tests/test_governance/test_monitoring_infra.py"],
            ["docker/observability/prometheus/prometheus.yml"],
        )
    )

    assert lint["errors"] == []

def test_all_tasks_normalizes_existing_t001_contract(tmp_path: Path) -> None:
    from delivery.task import all_tasks
    from delivery.state import save_work_items

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/"]},
                {"id": "T001", "title": "共享基础设施", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/main.py"]},
            ],
        },
    )

    tasks = {task.id: task for task in all_tasks(tmp_path)}

    assert "backend/src/main.py" in tasks["T001"].output_paths
    assert "backend/pyproject.toml" in tasks["T001"].output_paths
    assert "backend/tests/" in tasks["T001"].output_paths
    assert "frontend/src/App.tsx" in tasks["T001"].output_paths
    assert "frontend/package-lock.json" in tasks["T001"].output_paths
    assert "frontend/pnpm-lock.yaml" in tasks["T001"].output_paths
    assert "frontend/e2e/" in tasks["T001"].output_paths
    assert "backend/tests/core/" not in tasks["T001"].output_tests
    assert "backend/tests/test_health.py" in tasks["T001"].output_tests
    assert "backend/tests/test_database.py" in tasks["T001"].output_tests
    assert "frontend/src/App.test.tsx" in tasks["T001"].output_tests

def test_check_test_type_coverage_flags_verified_frontend_acceptance_without_browser_evidence(tmp_path: Path) -> None:
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "coverage": [
                {"requirement_id": "REQ-001", "test_types": ["api"]},
            ],
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
                    "id": "T002",
                    "title": "Dashboard",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["frontend/src/pages/Dashboard.test.tsx"],
                    "output_paths": ["frontend/src/pages/dashboard.tsx"],
                    "review_status": "pass",
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [
                {
                    "task_id": "T002",
                    "timestamp": "2026-06-24T00:00:00Z",
                    "test_files": ["frontend/src/pages/Dashboard.test.tsx"],
                    "test_types": ["unit"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
        },
    )

    missing = check_test_type_coverage(tmp_path)

    assert ("REQ-001", "browser") in missing

def test_check_test_type_coverage_allows_explicit_downstream_browser_owner(tmp_path: Path) -> None:
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "coverage": [
                {"requirement_id": "REQ-001", "test_types": ["api"]},
            ],
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
                    "id": "T002",
                    "title": "Dashboard",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T000"],
                    "output_tests": ["frontend/src/pages/Dashboard.test.tsx"],
                    "output_paths": ["frontend/src/pages/dashboard.tsx"],
                    "review_status": "pass",
                },
                {
                    "id": "T020",
                    "title": "Dashboard validation",
                    "status": "pending",
                    "task_kind": "validation",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/dashboard.spec.ts"],
                    "output_paths": ["docs/reviews/test-report-dashboard.md"],
                },
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [
                {
                    "task_id": "T002",
                    "timestamp": "2026-06-24T00:00:00Z",
                    "test_files": ["frontend/src/pages/Dashboard.test.tsx"],
                    "test_types": ["unit"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
        },
    )

    missing = check_test_type_coverage(tmp_path)

    assert ("REQ-001", "browser") not in missing

def test_decompose_tasks_merges_foundation_like_generated_task_into_t001(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    payload = decompose_tasks(
        tmp_path,
        [
            {
                "id": "T002",
                "title": "基础设施与共享组件",
                "requirements": [],
                "acceptance_scenarios": [],
                "dependencies": [],
                "output_tests": ["backend/tests/core/test_database.py"],
                "output_paths": ["backend/src/core/database/", "frontend/src/lib/auth/"],
            },
            {
                "title": "Feature",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": ["T002"],
                "output_tests": ["backend/tests/feature/test_feature.py"],
                "output_paths": ["backend/src/feature/"],
            },
        ],
        include_shared_foundation=True,
    )
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T001"]["output_tests"] == ["backend/tests/core/test_database.py"]
    assert by_id["T001"]["requirements"] == []
    assert by_id["T003"]["requirements"] == ["REQ-001"]
    assert by_id["T003"]["dependencies"] == ["T001"]
    assert all(item["title"] != "基础设施与共享组件" for item in payload["items"])

def test_decompose_tasks_migrates_foundation_requirements_to_first_real_task(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "IC-001", "title": "Infra", "summary": "Infra"},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    payload = decompose_tasks(
        tmp_path,
        [
            {
                "id": "T002",
                "title": "基础设施与共享组件",
                "requirements": ["IC-001"],
                "acceptance_scenarios": [],
                "dependencies": [],
                "output_tests": ["backend/tests/core/test_database.py"],
                "output_paths": ["backend/src/core/database/", "frontend/src/lib/auth/"],
            },
            {
                "title": "Feature",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": ["T002"],
                "output_tests": ["backend/tests/feature/test_feature.py"],
                "output_paths": ["backend/src/feature/"],
            },
        ],
        include_shared_foundation=True,
    )
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T001"]["requirements"] == ["IC-001"]
    assert by_id["T003"]["requirements"] == ["IC-001", "REQ-001"]

def test_decompose_tasks_normalizes_title_dependencies_to_ids(tmp_path: Path) -> None:
    payload = decompose_tasks(
        tmp_path,
        [
            {"title": "Infrastructure Foundation & Shared Services", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_foundation.py"], "output_paths": ["backend/src/foundation/"]},
            {"title": "Mock HTTP Services", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["Infrastructure Foundation & Shared Services"], "output_tests": ["tests/test_mock.py"], "output_paths": ["mock-server/app/"]},
        ],
        include_shared_foundation=False,
    )
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["dependencies"] == ["T000"]
    assert by_id["T003"]["dependencies"] == ["T002"]

def test_decompose_tasks_normalizes_shortened_title_dependencies(tmp_path: Path) -> None:
    payload = decompose_tasks(
        tmp_path,
        [
            {"title": "T005 — Data Import Pipeline (Orders, Production, Inventory, Logistics)", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_import.py"], "output_paths": ["backend/src/import/"]},
            {"title": "T006 — Event Platform & OTIF Risk Engine", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T005 — Data Import Pipeline"], "output_tests": ["tests/test_event.py"], "output_paths": ["backend/src/events/"]},
        ],
        include_shared_foundation=False,
    )
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["dependencies"] == ["T000"]
    assert by_id["T003"]["dependencies"] == ["T002"]

def test_decompose_tasks_default_to_scaffold_not_previous_task(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    payload = decompose_tasks(
        tmp_path,
        [
            {"title": "Task A", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_a.py"], "output_paths": ["backend/src/a/"]},
            {"title": "Task B", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_b.py"], "output_paths": ["backend/src/b/"]},
        ],
        include_shared_foundation=False,
    )
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T002"]["dependencies"] == ["T000"]
    assert by_id["T003"]["dependencies"] == ["T000"]


def test_decompose_tasks_rejects_flat_dependency_graph_for_shared_foundation_projects(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    requirements = [{"id": f"REQ-{idx:03d}", "title": f"Req {idx}", "summary": "One"} for idx in range(1, 9)]
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": requirements, "acceptance_scenarios": []}), encoding="utf-8")
    items = [
        {"title": f"Feature {idx}", "requirements": [f"REQ-{idx:03d}"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [f"backend/tests/test_{idx}.py"], "output_paths": [f"backend/feature_{idx}/"]}
        for idx in range(1, 9)
    ]

    try:
        decompose_tasks(tmp_path, items, include_shared_foundation=True)
    except ValueError as exc:
        assert "flat dependency graph" in str(exc)
        assert "Add real prerequisite edges" in str(exc)
    else:
        raise AssertionError("expected flat graph guard to reject all-empty dependencies")


def test_decompose_tasks_rejects_validation_task_without_validated_feature_dependency(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")

    try:
        decompose_tasks(
            tmp_path,
            [
                {"id": "T010", "title": "Feature", "task_kind": "feature", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/feature/"]},
                {"id": "T020", "title": "Feature validation", "task_kind": "validation", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature_validation.py"], "output_paths": ["docs/reviews/feature-validation.md"]},
            ],
            include_shared_foundation=True,
        )
    except ValueError as exc:
        assert "must depend on the feature task(s) it validates" in str(exc)
        assert "Feature" in str(exc)
    else:
        raise AssertionError("expected validation task dependency guard")


def test_decompose_tasks_allows_validation_task_with_validated_feature_dependency(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}), encoding="utf-8")

    payload = decompose_tasks(
        tmp_path,
        [
            {"id": "T010", "title": "Feature", "task_kind": "feature", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/feature/"]},
            {"id": "T020", "title": "Feature validation", "task_kind": "validation", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["Feature"], "output_tests": ["backend/tests/test_feature_validation.py"], "output_paths": ["docs/reviews/feature-validation.md"]},
        ],
        include_shared_foundation=True,
    )

    by_title = {item["title"]: item for item in payload["items"]}
    assert by_title["Feature validation"]["dependencies"] == [by_title["Feature"]["id"]]

def test_decompose_tasks_rejects_oversized_task_shape(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    try:
        decompose_tasks(
            tmp_path,
            [{
                "title": "Too big",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": [],
                "output_tests": [f"tests/test_{idx}.py" for idx in range(11)],
                "output_paths": [f"backend/src/path_{idx}/" for idx in range(21)],
            }],
            include_shared_foundation=False,
        )
    except ValueError as exc:
        assert "oversized" in str(exc)
    else:
        raise AssertionError("expected oversized task validation error")

def test_decompose_tasks_rejects_acceptance_without_tests(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    try:
        decompose_tasks(
            tmp_path,
            [{"title": "No tests", "requirements": ["REQ-001"], "acceptance_scenarios": ["AS-001"], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/x/"]}],
            include_shared_foundation=False,
        )
    except ValueError as exc:
        assert "acceptance scenarios" in str(exc)
    else:
        raise AssertionError("expected acceptance/test alignment error")

def test_decompose_tasks_requests_reconsideration_for_large_task_without_split_justification(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    requirements = [{"id": f"REQ-{idx:03d}", "title": f"Req {idx}", "summary": "One"} for idx in range(1, 14)]
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": requirements, "acceptance_scenarios": []}), encoding="utf-8")
    try:
        decompose_tasks(
            tmp_path,
            [
                {
                    "title": "Large coherent capability",
                    "requirements": [row["id"] for row in requirements],
                    "acceptance_scenarios": [f"AS-{idx:03d}" for idx in range(1, 7)],
                    "dependencies": [],
                    "output_tests": [f"backend/tests/test_{idx}.py" for idx in range(1, 10)],
                    "output_paths": ["backend/src/large/"],
                }
            ],
            include_shared_foundation=False,
        )
    except ValueError as exc:
        message = str(exc)
        assert "needs boundary review" in message
        assert "intent.split_justification" in message
    else:
        raise AssertionError("expected large task to require split justification")

def test_decompose_tasks_allows_large_task_with_split_justification(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    requirements = [{"id": f"REQ-{idx:03d}", "title": f"Req {idx}", "summary": "One"} for idx in range(1, 14)]
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": requirements, "acceptance_scenarios": []}), encoding="utf-8")

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Large but indivisible capability",
                "requirements": [row["id"] for row in requirements],
                "acceptance_scenarios": [f"AS-{idx:03d}" for idx in range(1, 7)],
                "dependencies": [],
                "output_tests": [f"backend/tests/test_{idx}.py" for idx in range(1, 10)],
                "output_paths": ["backend/src/large/"],
                "intent": {
                    "objective": "Deliver a single indivisible workflow",
                    "split_justification": "Splitting would create plumbing-only tasks without independently testable behavior.",
                },
            }
        ],
        include_shared_foundation=False,
    )

    by_title = {item["title"]: item for item in payload["items"]}
    assert by_title["Large but indivisible capability"]["intent"]["split_justification"].startswith("Splitting would")

def test_task_shape_size_guard_ignores_framework_production_gates() -> None:
    _validate_task_shape(
        [
            Task(
                "T900",
                "Production Gate: Real agent integration",
                "pending",
                [f"REQ-{idx:03d}" for idx in range(1, 31)],
                [],
                [],
                ["backend/tests/integration/agents/test_real_agent_inputs.py"],
                ["backend/tests/integration/agents/test_real_agent_inputs.py"],
                task_kind="validation",
            )
        ]
    )

def test_decompose_tasks_rejects_more_than_twenty_non_builtin_tasks(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    requirements = [{"id": f"REQ-{idx:03d}", "title": f"Req {idx}", "summary": "One"} for idx in range(1, 22)]
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": requirements, "acceptance_scenarios": []}), encoding="utf-8")
    items = [
        {
            "title": f"Feature {idx}",
            "requirements": [f"REQ-{idx:03d}"],
            "acceptance_scenarios": [],
            "dependencies": [],
            "output_tests": [f"backend/tests/test_feature_{idx}.py"],
            "output_paths": [f"backend/src/feature_{idx}/"],
        }
        for idx in range(1, 22)
    ]

    try:
        decompose_tasks(tmp_path, items, include_shared_foundation=False)
    except ValueError as exc:
        message = str(exc)
        assert "non-built-in tasks (>20)" in message
        assert "reassess whether this count is appropriate" in message
    else:
        raise AssertionError("expected task graph count guard to reject more than 20 non-built-in tasks")

def test_check_test_type_coverage_uses_test_plan_bindings(tmp_path: Path) -> None:
    from delivery.state import save_test_results, save_work_items

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [],
        },
    )
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T00:00:00Z",
            "coverage": [{"requirement_id": "REQ-001", "test_types": ["api", "browser"], "suite": "tests/auth/"}],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "results": [
                {
                    "task_id": "T002",
                    "timestamp": "2026-06-24T00:00:00Z",
                    "test_files": ["tests/auth/test_api_register.py"],
                    "test_types": ["api"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )
    assert check_test_type_coverage(tmp_path) == [("REQ-001", "browser")]

def test_check_test_type_coverage_accepts_verified_gate_observed_type(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [],
        },
    )
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T00:00:00Z",
            "coverage": [{"requirement_id": "REQ-001", "test_types": ["scenario"], "suite": "backend/scenarios"}],
        },
    )
    save_test_results(
        tmp_path,
        {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {}},
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "gates": [
                {
                    "id": "GATE-scenario",
                    "status": "verified",
                    "scope_requirements": ["REQ-001"],
                    "observed_test_types": ["scenario"],
                    "required_test_types": ["scenario"],
                }
            ],
        },
    )

    assert check_test_type_coverage(tmp_path) == []

def test_check_test_type_coverage_treats_req_performance_accessibility_as_advisory(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [],
        },
    )
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T00:00:00Z",
            "coverage": [{"requirement_id": "REQ-001", "test_types": ["performance", "accessibility"], "suite": "release advisory"}],
        },
    )
    save_test_results(
        tmp_path,
        {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {}},
    )

    assert check_test_type_coverage(tmp_path) == []

def test_check_test_type_coverage_counts_pass_review_as_review_evidence(tmp_path: Path) -> None:
    from delivery.state import save_work_items

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
                    "requirements": ["REQ-CONSTRAINT-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/feature.py"],
                    "review_status": "pass",
                }
            ],
        },
    )
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T00:00:00Z",
            "coverage": [{"requirement_id": "REQ-CONSTRAINT-001", "test_types": ["review"], "suite": "code review"}],
        },
    )

    assert check_test_type_coverage(tmp_path) == []

def test_check_requirements_coverage_treats_final_verify_requirements_as_precovered(tmp_path: Path) -> None:
    requirements_payload = {
        "requirements": [
            {"id": "REQ-001", "title": "One", "summary": "One"},
            {"id": "NFR-001", "title": "Frontend gates", "summary": "Quality gates"},
        ],
        "acceptance_scenarios": [],
    }
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md"]},
            ],
        },
    )

    coverage = check_requirements_coverage(tmp_path, requirements_payload)

    assert coverage["all_covered"] is True
    assert coverage["uncovered"] == []

def test_check_test_type_coverage_allows_nfr_to_reuse_project_wide_passed_type(tmp_path: Path) -> None:
    from delivery.state import save_test_results

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [],
        },
    )
    save_test_plan(
        tmp_path,
        {
            "schema_version": "1",
            "generated_at": "2026-06-24T00:00:00Z",
            "coverage": [{"requirement_id": "NFR-002", "test_types": ["api", "integration"], "suite": "critical backend tests"}],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "results": [
                {
                    "task_id": "T002",
                    "timestamp": "2026-06-24T00:00:00Z",
                    "test_files": ["backend/tests/test_auth/test_login.py"],
                    "test_types": ["api"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )

    assert check_test_type_coverage(tmp_path) == [("NFR-002", "integration")]

def test_decompose_tasks_raises_on_uncovered_requirement(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    try:
        decompose_tasks(
            tmp_path,
            [{"title": "Only first", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_one.py"], "output_paths": ["backend/src/one/"]}],
            include_shared_foundation=False,
        )
    except ValueError as exc:
        assert "REQ-002" in str(exc)
    else:
        raise AssertionError("expected uncovered requirement error")

def test_decompose_tasks_allows_final_verify_nfr_coverage(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "NFR-001", "title": "Frontend gates", "summary": "Frontend quality gates must pass."},
                    {"id": "NFR-002", "title": "Backend gates", "summary": "Backend quality gates must pass."},
                    {"id": "NFR-003", "title": "Release QA", "summary": "Release QA browser evidence is required."},
                    {"id": "NFR-004", "title": "Manual start record", "summary": "Release QA manual start record is required."},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    payload = decompose_tasks(
        tmp_path,
        [{"title": "Only feature", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_one.py"], "output_paths": ["backend/src/one/"]}],
        include_shared_foundation=False,
    )

    final_task = payload["items"][-1]
    assert final_task["id"] == "T-FINAL"
    assert final_task["requirements"] == ["NFR-001", "NFR-002", "NFR-003", "NFR-004"]

def test_decompose_tasks_collapses_foundation_scope_task_without_exact_title_match(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    payload = decompose_tasks(
        tmp_path,
        [
            {
                "title": "Shared Backend Infrastructure & Seed Data",
                "requirements": [],
                "acceptance_scenarios": [],
                "dependencies": [],
                "output_tests": [],
                "output_paths": [
                    "backend/src/shared/dependencies/auth.py",
                    "backend/src/shared/middleware/tenant_context.py",
                    "backend/src/shared/services/task_manager.py",
                ],
            },
            {
                "title": "Feature",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": ["Shared Backend Infrastructure & Seed Data"],
                "output_tests": ["backend/tests/feature/test_feature.py"],
                "output_paths": ["backend/src/feature/"],
            },
        ],
        include_shared_foundation=True,
    )

    by_id = {item["id"]: item for item in payload["items"]}
    assert "T002" not in by_id
    assert "backend/src/shared/services/task_manager.py" in by_id["T001"]["output_paths"]
    assert by_id["T003"]["dependencies"] == ["T001"]

def test_decompose_tasks_raises_on_dependency_cycle(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "REQ-002", "title": "Two", "summary": "Two"},
                ],
                "acceptance_scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    try:
        decompose_tasks(
            tmp_path,
            [
                {"title": "Task A", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["Task B"], "output_tests": ["tests/test_a.py"], "output_paths": ["backend/src/a/"]},
                {"title": "Task B", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["Task A"], "output_tests": ["tests/test_b.py"], "output_paths": ["backend/src/b/"]},
            ],
            include_shared_foundation=False,
        )
    except ValueError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("expected dependency cycle error")

