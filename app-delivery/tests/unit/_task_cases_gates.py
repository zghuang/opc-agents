from __future__ import annotations

import json
from pathlib import Path

from delivery.builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from delivery.state import save_gates, save_test_plan, save_test_results, save_work_items
from delivery.task import Task, check_requirements_coverage, check_test_type_coverage, decompose_tasks, lint_task_contract, mark_task, pick_next_task, referenced_req_ids, reset_task
from delivery.gates import normalize_complexity_override, normalize_stage_gates, refresh_gates, validate_validation_tasks, validate_gate_references


def test_refresh_gates_marks_gate_verified_when_scope_and_validation_evidence_pass(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_auth/test_login.py"], "output_paths": ["backend/src/auth/service.py"], "review_status": "pass"},
                {"id": "T003", "title": "Auth gate validation", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/login.spec.ts"], "output_paths": ["docs/reviews/test-report-auth.md"], "review_status": "pass", "task_kind": "validation"},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-auth",
                    "kind": "module",
                    "title": "Auth gate",
                    "status": "pending",
                    "scope_tasks": ["T002"],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["api", "browser"],
                    "source": "task-decompose",
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
                    "test_files": ["backend/tests/test_auth/test_login.py"],
                    "test_types": ["api"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                },
                {
                    "task_id": "T003",
                    "timestamp": "2026-06-24T00:10:00Z",
                    "test_files": ["frontend/e2e/login.spec.ts"],
                    "test_types": ["browser"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                },
            ],
            "full_suite_results": {"passed": True, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "verified"
    assert gate["missing_test_types"] == []
    assert gate["repair_candidates"] == []
    assert gate["report_artifact"] == "docs/reviews/gate-report-GATE-auth.md"
    assert (tmp_path / "docs" / "reviews" / "gate-report-GATE-auth.md").exists()

def test_refresh_gates_marks_gate_blocked_and_suggests_repairs_when_evidence_is_missing(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_auth/test_login.py"], "output_paths": ["backend/src/auth/service.py"], "review_status": "pass"},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-auth",
                    "kind": "module",
                    "title": "Auth gate",
                    "status": "pending",
                    "scope_tasks": ["T002"],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["api", "browser"],
                    "source": "task-decompose",
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
                    "test_files": ["backend/tests/test_auth/test_login.py"],
                    "test_types": ["api"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                },
            ],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "blocked"
    assert gate["missing_test_types"] == ["browser"]
    assert gate["repair_candidates"] == ["T002"]
    assert gate["report_artifact"] == "docs/reviews/gate-report-GATE-auth.md"
    text = (tmp_path / "docs" / "reviews" / "gate-report-GATE-auth.md").read_text(encoding="utf-8")
    assert "Missing test types: browser" in text

def test_refresh_gates_waits_for_pending_validation_task_before_reopening_verified_scope_task(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Ingestion", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_ingestion/test_pipeline.py"], "output_paths": ["backend/src/ingestion/"], "review_status": "pass"},
                {"id": "T020", "title": "Validation", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["mock-server/tests/test_routers/test_erp_orders.py"], "output_paths": ["docs/reviews/test-report-validation.md"], "task_kind": "validation"},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-ingestion",
                    "kind": "module",
                    "title": "Ingestion gate",
                    "status": "pending",
                    "scope_tasks": ["T002"],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["api", "contract"],
                    "source": "task-decompose",
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
                    "test_files": ["backend/tests/test_ingestion/test_pipeline.py"],
                    "test_types": ["api", "integration"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                },
            ],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "pending"
    assert gate["missing_test_types"] == ["contract"]
    assert gate["blocked_reason"] == "waiting for related validation tasks to satisfy missing test types"
    assert gate["repair_candidates"] == ["T020"]

def test_refresh_gates_waits_for_pending_validation_task_with_contract_named_mock_suite(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "Scaffold", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/"]},
                {"id": "T004", "title": "Ingestion", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_ingestion/test_pipeline.py"], "output_paths": ["backend/src/ingestion.py"]},
                {"id": "T020", "title": "Validation", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T004"], "output_tests": ["mock-server/tests/test_all_mocks_contract.py"], "output_paths": ["docs/reviews/test-report-validation.md"], "task_kind": "validation"},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-ingestion",
                    "kind": "module",
                    "title": "Ingestion gate",
                    "status": "pending",
                    "scope_tasks": ["T004"],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["api", "contract"],
                    "source": "task-decompose",
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
                    "task_id": "T004",
                    "timestamp": "2026-06-24T00:00:00Z",
                    "test_files": ["backend/tests/test_ingestion/test_pipeline.py"],
                    "test_types": ["api", "integration"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                },
            ],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "pending"
    assert gate["missing_test_types"] == ["contract"]
    assert gate["blocked_reason"] == "waiting for related validation tasks to satisfy missing test types"
    assert gate["repair_candidates"] == ["T020"]

def test_refresh_gates_keeps_requirement_only_gate_pending_until_mapped_tasks_finish(tmp_path: Path) -> None:
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
                    "title": "Auth feature",
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                    "review_status": None,
                },
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-auth",
                    "kind": "module",
                    "title": "Auth gate",
                    "status": "pending",
                    "scope_tasks": [],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["api", "browser"],
                    "source": "task-decompose",
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "active"
    assert gate["blocked_reason"] == "scope tasks are still running"
    assert gate["missing_test_types"] == ["api", "browser"]
    assert gate["repair_candidates"] == ["T002"]

def test_refresh_gates_marks_legacy_unscoped_gate_pending_with_mapping_reason(tmp_path: Path) -> None:
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
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature.py"],
                },
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-x",
                    "kind": "module",
                    "title": "Legacy unscoped gate",
                    "status": "pending",
                    "scope_tasks": [],
                    "scope_requirements": [],
                    "required_test_types": ["integration"],
                    "source": "task-decompose",
                }
            ],
        },
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "results": [],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "pending"
    assert gate["blocked_reason"] == "gate scope mapping is missing from task-decompose output"
    assert gate["missing_test_types"] == ["integration"]

def test_refresh_gates_normalizes_mock_server_results_to_contract_and_integration(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/"]},
                {"id": "T002", "title": "Mocks", "status": "verified", "requirements": ["REQ-066"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["mock-server/tests/test_routers/test_erp_orders.py"], "output_paths": ["mock-server/"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "gate-mocks",
                    "kind": "module",
                    "title": "Mock gate",
                    "scope_tasks": ["T002"],
                    "scope_requirements": ["REQ-066"],
                    "required_test_types": ["integration", "contract"],
                    "source": "task-decompose",
                }
            ],
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
                    "test_files": ["mock-server/tests/test_routers/test_erp_orders.py"],
                    "test_types": ["unit"],
                    "requirement_ids": ["REQ-066"],
                    "passed": True,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {"passed": True, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["status"] == "verified"
    assert gate["observed_test_types"] == ["contract", "integration", "unit"]
    assert gate["missing_test_types"] == []
    assert gate["repair_candidates"] == []

def test_refresh_gates_normalizes_supplychain_auth_results_to_api_and_browser(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth", "status": "review_pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/src/tests/test_auth/test_login.py", "frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/", "frontend/src/routes/login.tsx"]},
            ],
        },
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "M", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "GATE-auth",
                    "kind": "module",
                    "title": "Auth gate",
                    "scope_tasks": ["T002"],
                    "scope_requirements": ["REQ-001"],
                    "required_test_types": ["api", "browser"],
                    "source": "task-decompose",
                }
            ],
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
                    "test_files": ["backend/src/tests/test_auth/test_login.py", "frontend/e2e/auth.spec.ts"],
                    "test_types": ["unit", "e2e"],
                    "requirement_ids": ["REQ-001"],
                    "passed": True,
                    "passed_count": 3,
                    "failed_count": 0,
                    "failures": [],
                    "attempt": 1,
                }
            ],
            "full_suite_results": {"passed": True, "scores": {}},
        },
    )

    payload = refresh_gates(tmp_path)

    gate = payload["gates"][0]
    assert gate["observed_test_types"] == ["api", "browser", "e2e", "unit"]
    assert gate["missing_test_types"] == []

