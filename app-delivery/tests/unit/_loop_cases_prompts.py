from __future__ import annotations

import json
import os
from pathlib import Path

from delivery.loop import DeliveryLoop
from delivery.loop import recover
from delivery.loop_gitops import git_commit_task, task_scope_delta
from delivery.loop_reporting import project_summary, status as runtime_status
from delivery.loop_review import _parse_review_payload, _validate_pass_review_matrix, build_code_review_request, code_review_request_path, write_code_review_request
from delivery.loop_task_prompt import build_fix_prompt, build_stalled_recovery_prompt, build_task_prompt
from delivery.session import RuntimeErrorResponse, RuntimeSession, save_current_session
from delivery.state import load_session_state, load_task_runtime_state, save_session_state, save_task_runtime_state, save_test_results, save_work_items


def test_build_code_review_prompt_includes_requirement_details(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "User login", "summary": "Users can sign in."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Happy path", "summary": "Successful login.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_code_review_request(tmp_path, Task("T002", "Login", "pending", ["REQ-001"], ["AS-001"], [], [], []))
    assert "User login" in prompt
    assert "Users can sign in." in prompt
    assert "Successful login." in prompt
    assert "For every declared requirement" in prompt
    assert "For every declared acceptance scenario" in prompt
    assert "Green tests are not enough" in prompt
    assert "user-visible frontend acceptance scenario" not in prompt
    assert "# Code Review Contract" in prompt
    assert "move_acceptance_scenario.id` must be one of: AS-001" in prompt

def test_review_payload_validation_reports_multiple_schema_errors() -> None:
    from delivery.task import Task

    task = Task("T002", "Login", "review_pending", ["REQ-001"], ["AS-001"], [], [], [])
    payload = {
        "status": "pass",
        "summary": "Looks good.",
        "findings": ["legacy string finding", {"severity": "critical", "message": ""}],
        "requirement_assessment": [{"id": "REQ-001", "status": "done", "notes": ""}],
        "acceptance_assessment": "missing array",
    }

    try:
        _validate_pass_review_matrix(task, payload)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected validation to fail")

    assert "findings[0] must be an object" in message
    assert "findings[1].severity must be one of" in message
    assert "requirement_assessment status for REQ-001 must be one of" in message
    assert "acceptance_assessment must be provided as an array" in message

def test_write_code_review_request_preserves_timestamp_when_content_is_unchanged(tmp_path: Path) -> None:
    from delivery.task import Task

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(json.dumps({"requirements": [], "acceptance_scenarios": []}), encoding="utf-8")
    task = Task("T002", "Login", "pending", [], [], [], [], [])

    first = write_code_review_request(tmp_path, task)
    request_path = tmp_path / first
    before = request_path.stat().st_mtime_ns
    second = write_code_review_request(tmp_path, task)
    after = request_path.stat().st_mtime_ns

    assert first == second
    assert before == after

def test_status_reports_runtime_attention_for_long_read_only_run(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "status_session_id": "ses-op-stall"},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "task_id": "T002",
            "status": "running",
            "started_at": "2026-06-24T00:00:00Z",
            "updated_at": "2026-06-24T00:20:00Z",
            "session_id": "ses-op-stall",
            "runtime_pid": os.getpid(),
            "wrapper_pid": os.getpid(),
            "last_tool_name": "read",
            "last_tool_at": "2026-06-24T00:19:00Z",
            "last_activity_kind": "read_only",
            "last_mutation_at": None,
            "read_only_streak": 25,
        },
    )
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "opencode-T002-ses-op-stall.json").write_text(
        json.dumps(
            {
                "task_id": "T002",
                "task_title": "Feature",
                "runtime": "opencode",
                "session_id": "ses-op-stall",
                "phase": "implementation",
                "status": "running",
                "pid": os.getpid(),
                "runtime_pid": os.getpid(),
                "updated_at": "2026-06-24T00:20:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = runtime_status(tmp_path)

    assert payload["runtime_attention"]["suspected"] is True
    assert payload["runtime_attention"]["kind"] == "read_only_stall"
    assert payload["runtime_attention"]["read_only_streak"] == 25
    assert payload["active_task"]["runtime_attention"]["last_tool_name"] == "read"

def test_task_scope_delta_ignores_machine_owned_structure_and_context_docs(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "feature.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "feature.py").write_text("print('base')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "project-structure.md").write_text("# structure\n", encoding="utf-8")
    tmp_path.joinpath("AGENTS.md").write_text("# agents\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "feature.py").write_text("print('task')\n", encoding="utf-8")
    (tmp_path / "docs" / "project-structure.md").write_text("# structure updated\n", encoding="utf-8")
    tmp_path.joinpath("AGENTS.md").write_text("# agents updated\n", encoding="utf-8")

    delta = task_scope_delta(
        tmp_path,
        Task("T004", "Feature", "pending", ["REQ-001"], [], [], ["backend/tests/test_feature.py"], ["backend/src/feature.py"]),
    )

    assert delta["out_of_scope"] == []

def test_build_code_review_prompt_includes_technology_hint_check_for_manifest_tasks(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "runtime": "claude",
                "dependency_hints": [
                    {"ecosystem": "backend", "name": "LangGraph", "reason": "Explicit orchestration framework"}
                ],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_code_review_request(
        tmp_path,
        Task("T001", "Shared infra", "pending", [], [], [], [], ["backend/pyproject.toml"]),
    )

    assert "Tech-design check:" in prompt
    assert "Manifest changes must align with `Tech Design`" in prompt
    assert "Fail silent contradictions" in prompt
    assert "demo constants, hardcoded responses, in-memory state" in prompt

def test_build_task_prompt_includes_requirement_and_acceptance_details(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "User login", "summary": "Users can sign in."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Happy path", "summary": "Successful login.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_task_prompt(tmp_path, Task("T002", "Login", "pending", ["REQ-001"], ["AS-001"], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert "Requirement details:" in prompt
    assert "User login" in prompt
    assert "Users can sign in." in prompt
    assert "Acceptance scenario details:" in prompt
    assert "Successful login." in prompt
    assert "Use declared output paths/tests as the main contract" in prompt
    assert "Green tests are not enough" in prompt
    assert "cd backend && uv run pytest" in prompt
    assert "frontend/package.json" not in prompt

def test_build_task_prompt_includes_task_intent(tmp_path: Path) -> None:
    from delivery.task import Task

    prompt = build_task_prompt(
        tmp_path,
        Task.from_dict(
            {
                "id": "T002",
                "title": "Case workspace",
                "status": "pending",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": [],
                "output_tests": ["backend/tests/test_case.py"],
                "output_paths": ["backend/src/case/"],
                "intent": {
                    "objective": "Enable users to review cases.",
                    "journey": "User opens a case and sees status, evidence, and next action.",
                    "done_when": ["Case state can be queried", "Task-local tests pass"],
                    "non_goals": ["Do not implement reporting dashboards"],
                },
            }
        ),
    )

    assert "Task intent:" in prompt
    assert "Objective: Enable users to review cases." in prompt
    assert "Journey: User opens a case" in prompt
    assert "Done when:" in prompt
    assert "Case state can be queried" in prompt
    assert "Non-goals:" in prompt
    assert "Do not implement reporting dashboards" in prompt

def test_build_task_prompt_frontend_acceptance_requires_browser_guidance(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "Dashboard", "summary": "Users can navigate the dashboard."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Dashboard flow", "summary": "User opens the dashboard and sees navigation.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_task_prompt(
        tmp_path,
        Task(
            "T010",
            "Dashboard",
            "pending",
            ["REQ-001"],
            ["AS-001"],
            [],
            ["frontend/src/Dashboard.test.tsx"],
            ["frontend/src/pages/dashboard.tsx"],
        ),
    )

    assert "Frontend acceptance behavior" in prompt
    assert "No browser/e2e test is currently declared" in prompt

def test_build_validation_task_prompt_explains_validation_role(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "Incident handling", "summary": "System handles incidents."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Happy path", "summary": "Low-risk case completes automatically.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    task = Task.from_dict(
        {
            "id": "T010",
            "title": "Acceptance validation",
            "status": "pending",
            "task_kind": "validation",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": ["AS-001"],
            "dependencies": ["T009"],
            "output_tests": ["backend/tests/test_acceptance/test_as001.py", "frontend/e2e/incident-happy-path.spec.ts"],
            "output_paths": ["backend/tests/test_acceptance/", "frontend/e2e/"],
            "intent": {
                "objective": "Prove the incident happy path.",
                "journey": "Incident moves through backend and browser validation.",
                "done_when": ["Acceptance test asserts the scenario"],
            },
        }
    )

    prompt = build_task_prompt(tmp_path, task)

    assert "Validation task mission:" in prompt
    assert "Task intent:" in prompt
    assert "Prove the incident happy path." in prompt
    assert "This is a validation task." in prompt
    assert "app-delivery control loop will deterministically execute the declared Tests" in prompt
    assert "Prefer improving or adding acceptance tests, end-to-end tests, fixtures, scenario data, and test helpers before changing product code." in prompt
    assert "Do not turn this task into broad new product implementation" in prompt
    assert "Each declared acceptance scenario should map to at least one concrete validation path" in prompt
    assert "This task owns frontend-facing validation coverage." in prompt

def test_build_repair_task_prompt_encourages_iterative_gap_repair(tmp_path: Path) -> None:
    from delivery.task import Task

    report_path = tmp_path / "docs" / "reviews" / "final-repair-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# Final Repair Report\n\n## Missing Test Types\n\n- REQ-001: missing scenario\n", encoding="utf-8")

    task = Task.from_dict(
        {
            "id": "T010",
            "title": "Final Verification Repair Bundle (T007, T008)",
            "status": "pending",
            "task_kind": "repair",
            "requirements": ["REQ-001", "REQ-002"],
            "acceptance_scenarios": [],
            "dependencies": ["T007", "T008"],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/src/feature.py"],
            "blocked_reason": "synthesized repair bundle for final verification failures",
        }
    )

    prompt = build_task_prompt(tmp_path, task)

    assert "Repair objective:" in prompt
    assert "docs/reviews/final-repair-report.md" in prompt
    assert "Requirement details:" not in prompt
    assert "Requirement IDs potentially affected: REQ-001, REQ-002" in prompt
    assert "If this bundle contains many gaps, enumerate them and repair in small verified batches" in prompt
    assert "do not stop after only the first easy fix while declared failures remain" in prompt
    assert "let the framework resume from the remaining evidence" in prompt

def test_build_validation_code_review_prompt_reviews_validation_assets(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "Incident handling", "summary": "System handles incidents."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Happy path", "summary": "Low-risk case completes automatically.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    task = Task.from_dict(
        {
            "id": "T010",
            "title": "Acceptance validation",
            "status": "review_pending",
            "task_kind": "validation",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": ["AS-001"],
            "dependencies": ["T009"],
            "output_tests": ["backend/tests/test_acceptance/test_as001.py", "frontend/e2e/incident-happy-path.spec.ts"],
            "output_paths": ["backend/tests/test_acceptance/", "frontend/e2e/"],
            "intent": {
                "objective": "Prove the incident happy path.",
                "journey": "Incident moves through backend and browser validation.",
                "done_when": ["Acceptance test asserts the scenario"],
            },
        }
    )

    prompt = build_code_review_request(
        tmp_path,
        task,
        scope_report={
            "changed_paths": ["backend/tests/test_acceptance/test_as001.py", "backend/src/mock/fixtures.py", "docs/test-results.json"],
            "out_of_scope": ["backend/src/mock/fixtures.py"],
        },
    )

    assert "validation-task review" in prompt
    assert "# Code Review Contract" in prompt
    assert "Task intent:" in prompt
    assert "Prove the incident happy path." in prompt
    assert "Declared output_tests are contractual validation entrypoints" in prompt
    assert "Each declared scenario must map to assertions" in prompt
    assert "placeholder tests, shallow smoke checks, missing scenario coverage" in prompt
    assert "Support code is acceptable only when needed for truthful validation." in prompt
    assert "out_of_scope: backend/src/mock/fixtures.py" in prompt
    assert "necessary validation support" in prompt
    assert "docs/test-results.json" not in prompt
    assert "requirement_assessment" in prompt
    assert "acceptance_assessment" in prompt
    assert "move_acceptance_scenario.id` must be one of: AS-001" in prompt

def test_code_review_skill_and_generated_prompts_share_review_contract(tmp_path: Path) -> None:
    from delivery.task import Task

    contract_text = Path("skills/code-review/references/review-contract.md").read_text(encoding="utf-8")
    skill_text = Path("skills/code-review/SKILL.md").read_text(encoding="utf-8")
    prompt = build_code_review_request(
        tmp_path,
        Task("T002", "Login", "review_pending", ["REQ-001"], ["AS-001"], [], [], []),
    )

    assert "references/review-contract.md" in skill_text
    assert "move_acceptance_scenario.id` must be one of:" in contract_text
    assert "move_acceptance_scenario.id` must be one of: AS-001" in prompt

def test_build_code_review_request_includes_task_intent(tmp_path: Path) -> None:
    from delivery.task import Task

    prompt = build_code_review_request(
        tmp_path,
        Task.from_dict(
            {
                "id": "T002",
                "title": "Case workspace",
                "status": "review_pending",
                "requirements": ["REQ-001"],
                "acceptance_scenarios": [],
                "dependencies": [],
                "output_tests": ["backend/tests/test_case.py"],
                "output_paths": ["backend/src/case/"],
                "intent": {
                    "objective": "Enable users to review cases.",
                    "journey": "User opens a case and sees status, evidence, and next action.",
                    "done_when": ["Case state can be queried"],
                    "non_goals": ["Do not implement reporting dashboards"],
                },
            }
        ),
    )

    assert "Task intent:" in prompt
    assert "Objective: Enable users to review cases." in prompt
    assert "Use task intent only for purpose" in prompt

def test_build_task_prompt_includes_dependency_hints(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "Workflow UI", "summary": "Users manage workflow screens."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Workflow screen", "summary": "User navigates the workflow UI.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "runtime": "claude",
                "dependency_hints": [
                    {"ecosystem": "frontend", "name": "Ant Design", "reason": "Requirements mandate the component library."}
                ],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_task_prompt(
        tmp_path,
        Task(
            "T020",
            "Workflow UI",
            "pending",
            ["REQ-001"],
            ["AS-001"],
            [],
            ["frontend/e2e/workflow.spec.ts"],
            ["frontend/src/pages/workflow.tsx", "frontend/src/components/workflow/WorkflowPanel.tsx"],
        ),
    )

    assert "Project technology constraints for this task:" not in prompt
    assert "Ant Design" not in prompt

def test_build_code_review_prompt_includes_dependency_hints(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "Auth API", "summary": "Users authenticate through the backend."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Auth flow", "summary": "User signs in successfully.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "runtime": "claude",
                "dependency_hints": [
                    {"ecosystem": "backend", "name": "FastAPI Users", "reason": "Requirements explicitly call for this auth library."}
                ],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_code_review_request(
        tmp_path,
        Task(
            "T021",
            "Auth API",
            "pending",
            ["REQ-001"],
            ["AS-001"],
            [],
            ["backend/tests/test_auth/test_login.py"],
            ["backend/src/auth/router.py", "backend/src/auth/service.py"],
        ),
    )

    assert "Project technology constraints to verify:" in prompt
    assert "FastAPI Users" in prompt
    assert "Require implementation evidence or an explicit superseding ADR/clarification." in prompt

def test_build_fix_prompt_reanchors_repairs_to_project_contract(tmp_path: Path) -> None:
    from delivery.task import Task

    prompt = build_fix_prompt(
        tmp_path,
        Task(
            "T002",
            "Tenant Authentication & Session Isolation",
            "active",
            ["REQ-001"],
            ["AS-001"],
            [],
            ["backend/src/tests/test_auth/test_login.py", "frontend/e2e/auth.spec.ts"],
            ["backend/src/auth/", "frontend/src/routes/login.tsx"],
        ),
        "- T002: FAIL (2 passed, 1 failed)\n  - frontend/e2e/auth.spec.ts: Running 4 tests using 4 workers",
    )

    assert f"Project path: {tmp_path}" in prompt
    assert "Declared output paths: backend/src/auth/, frontend/src/routes/login.tsx" in prompt
    assert "Declared output tests: backend/src/tests/test_auth/test_login.py, frontend/e2e/auth.spec.ts" in prompt
    assert "Do not search sibling projects or the framework repo" in prompt
    assert "cd backend && uv run pytest" in prompt
    assert "frontend/package.json" in prompt

def test_build_stalled_recovery_prompt_focuses_on_existing_work_and_stall_evidence(tmp_path: Path) -> None:
    from delivery.task import Task

    prompt = build_stalled_recovery_prompt(
        tmp_path,
        Task(
            "T003",
            "RFQ Data Management",
            "active",
            ["REQ-002"],
            [],
            [],
            ["backend/src/tests/test_rfq/test_rfq_crud.py"],
            ["backend/src/rfq/"],
        ),
        runtime_state={"session_id": "ses-op-stall", "started_at": "2026-06-24T00:00:00Z", "last_tool_at": "2026-06-24T00:19:00Z", "last_mutation_at": None},
        runtime_attention={"kind": "silent_stall", "message": "Runtime is still alive but has not produced any recent actionable tool progress.", "last_tool_name": "todowrite"},
    )

    assert "Previous run for task T003 appears stalled." in prompt
    assert "Previous session id: ses-op-stall" in prompt
    assert "Last tool event: todowrite" in prompt
    assert "Do not re-scan the whole repository from scratch" in prompt
    assert "Continue from the existing implementation already on disk" in prompt

def test_build_task_prompt_filters_recent_failures_to_task_scope(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    save_test_results(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "results": [
                {"task_id": "T099", "passed": False, "test_files": ["backend/tests/other/test_other.py"], "failed_count": 1},
                {"task_id": "T002", "passed": False, "test_files": ["backend/tests/login/test_login.py"], "failed_count": 2},
            ],
            "full_suite_results": {"passed": False, "scores": {}},
        },
    )
    from delivery.task import Task

    prompt = build_task_prompt(tmp_path, Task("T002", "Login", "pending", [], [], [], ["backend/tests/login/"], ["backend/src/login/"]))

    assert "Relevant recent failing test summary:" in prompt
    assert "backend/tests/login/test_login.py" in prompt
    assert "backend/tests/other/test_other.py" not in prompt

def test_build_task_prompt_includes_relevant_gate_context(tmp_path: Path) -> None:
    from delivery.state import save_gates
    from delivery.task import Task

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    save_gates(
        tmp_path,
        {
            "schema_version": "1",
            "project": "demo",
            "complexity": {"tier": "S", "score": 0, "signals": {}, "source": "task-decompose"},
            "gates": [
                {
                    "id": "gate-auth",
                    "title": "Auth gate",
                    "status": "blocked",
                    "scope_tasks": ["T002"],
                    "required_test_types": ["unit", "api", "e2e"],
                    "observed_test_types": ["unit"],
                    "missing_test_types": ["api", "e2e"],
                    "blocked_reason": "missing required test types: api, e2e",
                    "report_artifact": "docs/reviews/gate-report-gate-auth.md",
                    "repair_candidates": ["T002"],
                }
            ],
        },
    )

    prompt = build_task_prompt(
        tmp_path,
        Task("T002", "Login", "pending", [], [], [], ["frontend/src/auth/LoginPage.test.tsx"], ["frontend/src/auth/"]),
    )

    assert "Related gate requirements:" in prompt
    assert "gate-auth: status=blocked" in prompt
    assert "missing=api, e2e" in prompt
    assert "report=docs/reviews/gate-report-gate-auth.md" in prompt

def test_build_task_prompt_enforces_task_boundary_stop_instruction(tmp_path: Path) -> None:
    from delivery.task import Task

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}),
        encoding="utf-8",
    )

    prompt = build_task_prompt(
        tmp_path,
        Task("T002", "Login", "pending", [], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]),
    )

    assert "Complete only the current task." in prompt
    assert "When the current task is complete, blocked, or ready for review, stop" in prompt

def test_build_task_prompt_uses_project_context_reference_for_manifest_tasks(tmp_path: Path) -> None:
    from delivery.task import Task

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}),
        encoding="utf-8",
    )
    (docs_dir / "project-bootstrap.json").write_text(
        json.dumps(
            {
                "runtime": "claude",
                "dependency_hints": [
                    {"ecosystem": "backend", "name": "LangGraph", "reason": "Requirements explicitly mention LangGraph."}
                ],
            }
        ),
        encoding="utf-8",
    )

    prompt = build_task_prompt(
        tmp_path,
        Task("T002", "Agent foundation", "pending", [], [], [], ["backend/tests/test_agent.py"], ["backend/pyproject.toml"]),
    )

    assert "Project dependency hints from requirements:" not in prompt
    assert "Project technology constraints for this task:" in prompt
    assert "LangGraph" in prompt
    assert "Tech Design` section in AGENTS.md / CLAUDE.md" in prompt
    assert "Resolve exact packages deliberately" in prompt

def test_build_task_prompt_includes_review_feedback_for_changes_requested(tmp_path: Path) -> None:
    from delivery.task import Task

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}),
        encoding="utf-8",
    )

    prompt = build_task_prompt(
        tmp_path,
        Task(
            "T002",
            "Login",
            "pending",
            [],
            [],
            [],
            ["backend/src/tests/test_auth.py"],
            ["backend/src/auth/"],
            review_status="changes_requested",
            review_artifact="docs/reviews/code-review-T002.md",
            blocked_reason="Tenant-aware login is still missing tenant-scoped auth behavior.",
        ),
    )

    assert "Review repair mode:" in prompt
    assert "Review summary: Tenant-aware login is still missing tenant-scoped auth behavior." in prompt
    assert "Review artifact: docs/reviews/code-review-T002.md" in prompt
    assert "Read the review artifact first" in prompt

def test_parse_review_payload_accepts_fenced_json() -> None:
    payload = _parse_review_payload(
        "Review complete.\n```json\n{\"status\":\"pass\",\"summary\":\"Looks good.\",\"findings\":[]}\n```"
    )

    assert payload == {"status": "pass", "summary": "Looks good.", "findings": []}

def test_parse_review_payload_rejects_unknown_status() -> None:
    try:
        _parse_review_payload('{"status":"needs_work","summary":"Not ready","findings":[]}')
    except ValueError as exc:
        assert "pass, changes_requested" in str(exc)
    else:
        raise AssertionError("expected invalid review status to be rejected")

def test_review_validation_rejects_string_findings() -> None:
    from delivery.task import Task

    payload = _parse_review_payload('{"status":"changes_requested","summary":"Not ready","findings":["missing behavior"],"requirement_assessment":[],"acceptance_assessment":[]}')
    try:
        _validate_pass_review_matrix(Task("T002", "Login", "review_pending", [], [], [], [], []), payload)
    except ValueError as exc:
        assert "findings[0] must be an object" in str(exc)
    else:
        raise AssertionError("expected string findings to be rejected")

def test_write_code_review_request_persists_prompt(tmp_path: Path) -> None:
    from delivery.task import Task

    relative = write_code_review_request(
        tmp_path,
        Task("T002", "Login", "pending", ["REQ-001"], [], [], [], []),
        scope_report={
            "changed_paths": ["backend/src/login/service.py", "frontend/src/shared/auth.ts", "docs/work-items.json", "docs/reviews/test-report-T002.md"],
            "out_of_scope": ["frontend/src/shared/auth.ts"],
        },
    )

    request_path = code_review_request_path(tmp_path, "T002")
    assert relative == str(request_path.relative_to(tmp_path))
    text = request_path.read_text(encoding="utf-8")
    assert "Perform an independent code review for task T002." in text
    assert "out_of_scope: frontend/src/shared/auth.ts" in text
    assert "Pass only if they are necessary support for this task" in text
    assert "Implementation-relevant changed paths seen by the framework:" in text
    assert "backend/src/login/service.py" in text
    assert "docs/work-items.json" not in text
    assert "docs/reviews/test-report-T002.md" not in text
    assert "task-scoped snapshot" in text
    assert "current working tree" not in text
    assert "Return raw JSON only" in text

def test_build_code_review_prompt_frontend_acceptance_demands_browser_judgement(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [{"id": "REQ-001", "title": "Dashboard", "summary": "Users can navigate the dashboard."}],
                "acceptance_scenarios": [{"id": "AS-001", "title": "Dashboard flow", "summary": "User opens the dashboard and sees navigation.", "source_requirement_ids": ["REQ-001"]}],
            }
        ),
        encoding="utf-8",
    )
    from delivery.task import Task

    prompt = build_code_review_request(
        tmp_path,
        Task(
            "T010",
            "Dashboard",
            "pending",
            ["REQ-001"],
            ["AS-001"],
            [],
            ["frontend/src/Dashboard.test.tsx"],
            ["frontend/src/pages/dashboard.tsx"],
        ),
    )

    assert "Extra paths are not failures when necessary for this task" in prompt
    assert "Frontend acceptance flows need browser/e2e evidence" in prompt

