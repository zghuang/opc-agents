from __future__ import annotations

import json
from pathlib import Path

from delivery.builtin_tasks import FINAL_VERIFY_TASK_ID, FRONTEND_API_AUDIT_REPORT_PATH, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID
from delivery.state import save_gates, save_test_plan, save_test_results, save_work_items
from delivery.task import Task, check_requirements_coverage, check_test_type_coverage, decompose_tasks, lint_task_contract, mark_task, pick_next_task, referenced_req_ids, reset_task
from delivery.gates import normalize_complexity_override, normalize_stage_gates, refresh_gates, validate_validation_tasks, validate_gate_references


def test_referenced_req_ids_expands_ranges() -> None:
    text = "REQ-001 to REQ-003 and NFR-001"
    assert referenced_req_ids(text) == ["REQ-001", "REQ-002", "REQ-003", "NFR-001"]


def test_pick_next_task_honors_verified_dependencies() -> None:
    tasks = [
        Task("T000", "脚手架", "verified", [], [], [], [], [], task_kind="feature"),
        Task("T001", "共享基础设施", "pending", [], [], ["T000"], [], [], task_kind="feature"),
        Task("T002", "功能", "pending", ["REQ-001"], [], ["T001"], [], [], task_kind="feature"),
    ]
    assert pick_next_task(tasks).id == "T001"


def test_pick_next_task_returns_none_when_dependencies_not_verified() -> None:
    tasks = [
        Task("T000", "脚手架", "verified", [], [], [], [], [], task_kind="feature"),
        Task("T001", "共享基础设施", "blocked", [], [], ["T000"], [], [], task_kind="feature"),
        Task("T002", "功能", "pending", ["REQ-001"], [], ["T001"], [], [], task_kind="feature"),
        Task("T003", "功能二", "pending", ["REQ-002"], [], ["T002"], [], [], task_kind="feature"),
    ]
    assert pick_next_task(tasks) is None


def test_pick_next_task_skips_dependency_exception_and_returns_independent_ready_task() -> None:
    tasks = [
        Task("T000", "脚手架", "verified", [], [], [], [], [], task_kind="feature"),
        Task("T001", "共享基础设施", "exception", [], [], ["T000"], [], [], task_kind="feature"),
        Task("T002", "功能", "pending", ["REQ-001"], [], ["T001"], [], [], task_kind="feature"),
        Task("T003", "功能二", "pending", ["REQ-002"], [], ["T000"], [], [], task_kind="feature"),
    ]
    assert pick_next_task(tasks).id == "T003"


def test_mark_task_sets_verified_timestamp() -> None:
    tasks = [Task("T001", "功能", "pending", [], [], [], [], [], task_kind="feature")]
    updated = mark_task(tasks, "T001", "verified")
    assert updated[0].status == "verified"
    assert updated[0].verified_at is not None


def test_mark_task_accumulates_session_history() -> None:
    tasks = [Task("T001", "功能", "pending", [], [], [], [], [], task_kind="feature")]
    updated = mark_task(tasks, "T001", "active", status_session_id="session-1")
    updated = mark_task(updated, "T001", "pending", status_session_id=None)
    updated = mark_task(updated, "T001", "active", status_session_id="session-2")
    updated = mark_task(updated, "T001", "review_pending", status_session_id="session-2")

    assert updated[0].status_session_id == "session-2"
    assert updated[0].session_ids == ["session-1", "session-2"]


def test_reset_task_clears_review_and_block_state() -> None:
    tasks = [
        Task(
            "T002",
            "功能",
            "blocked",
            "feature",
            ["REQ-001"],
            [],
            ["T001"],
            ["tests/test_feature.py"],
            ["backend/src/feature.py"],
            git_commit="abc",
            status_session_id="session-1",
            started_at="2026-06-24T00:00:00Z",
            completed_at="2026-06-24T00:10:00Z",
            review_status="changes_requested",
            review_artifact="docs/reviews/code-review-T002.md",
            reviewed_at="2026-06-24T00:11:00Z",
            verified_at="2026-06-24T00:12:00Z",
            blocked_reason="failed",
            attempts=3,
        )
    ]
    updated = reset_task(tasks, "T002")
    assert updated[0].status == "pending"
    assert updated[0].git_commit is None
    assert updated[0].review_status is None
    assert updated[0].blocked_reason is None
    assert updated[0].attempts == 0


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
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002"]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == [PREFINAL_AUDIT_TASK_ID]
    assert payload["items"][1]["output_paths"] == [
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/src/main.py",
        "backend/src/runtime/",
        "backend/src/tests/",
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
    assert by_id[PREFINAL_AUDIT_TASK_ID]["dependencies"] == ["T000", "T001", "T002", FRONTEND_API_AUDIT_TASK_ID]
    assert by_id[FINAL_VERIFY_TASK_ID]["dependencies"] == [PREFINAL_AUDIT_TASK_ID]


def test_decompose_tasks_normalizes_backend_app_contract_paths(tmp_path: Path) -> None:
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
        "backend/src/core/config.py",
        "backend/src/domain/models.py",
        "backend/src/main.py",
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


def test_normalize_complexity_override_requires_task_decompose_metadata() -> None:
    payload = normalize_complexity_override(
        {
            "tier": "M",
            "rationale": "Multi-domain project with milestone validation needs.",
            "signals": {"estimated_loc": 80000, "estimated_tasks": 18},
        },
    )

    assert payload["tier"] == "M"
    assert payload["source"] == "task-decompose"
    assert payload["signals"]["estimated_tasks"] == 18


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
    assert "backend/src/runtime/" in tasks["T001"].output_paths
    assert "frontend/src/App.tsx" in tasks["T001"].output_paths
    assert "frontend/package-lock.json" in tasks["T001"].output_paths
    assert "frontend/pnpm-lock.yaml" in tasks["T001"].output_paths
    assert "frontend/e2e/" in tasks["T001"].output_paths
    assert "backend/tests/core/" not in tasks["T001"].output_tests
    assert "backend/src/tests/test_health.py" in tasks["T001"].output_tests
    assert "backend/src/tests/test_database.py" in tasks["T001"].output_tests
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
