from __future__ import annotations

import json
import os
from pathlib import Path

from delivery.loop import DeliveryLoop
from delivery.loop import recover
from delivery.loop_gitops import git_commit_task, task_scope_delta
from delivery.loop_reporting import project_summary, status as runtime_status
from delivery.loop_review import _parse_review_payload, build_code_review_request, code_review_request_path, write_code_review_request
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
    assert "Passing task tests is necessary but not sufficient" in prompt
    assert "user-visible frontend acceptance scenario" not in prompt


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
    tmp_path.joinpath("CODE_MAP.md").write_text("# map\n", encoding="utf-8")
    tmp_path.joinpath("AGENTS.md").write_text("# agents\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "feature.py").write_text("print('task')\n", encoding="utf-8")
    (tmp_path / "docs" / "project-structure.md").write_text("# structure updated\n", encoding="utf-8")
    tmp_path.joinpath("CODE_MAP.md").write_text("# map updated\n", encoding="utf-8")
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
    assert "Tech Design` section in AGENTS.md / CLAUDE.md" in prompt
    assert "silently contradict project-wide mandated stack choices" in prompt
    assert "Do not fail the task solely because the broader architecture or future tasks mention additional technologies" in prompt


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
    assert "Prefer the declared output paths and output tests" in prompt
    assert "minimum validation floor" in prompt
    assert "Passing tests are necessary but not sufficient" in prompt
    assert "cd backend && uv run pytest" in prompt
    assert "frontend/package.json" not in prompt


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

    assert "frontend-facing acceptance behavior" in prompt
    assert "No browser/e2e test is currently declared" in prompt


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

    assert "Project metadata declares technology constraints relevant to this task." in prompt
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

    assert "Project metadata declares technology constraints relevant to this task." in prompt
    assert "FastAPI Users" not in prompt


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
    assert "LangGraph" not in prompt
    assert "Tech Design` section in AGENTS.md / CLAUDE.md" in prompt
    assert "resolve the exact package entry deliberately" in prompt


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

    assert "Previous independent code review requested changes:" in prompt
    assert "Review summary: Tenant-aware login is still missing tenant-scoped auth behavior." in prompt
    assert "Review artifact: docs/reviews/code-review-T002.md" in prompt
    assert "Read that artifact and repair the cited issues before re-running validation." in prompt


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


def test_write_code_review_request_persists_prompt(tmp_path: Path) -> None:
    from delivery.task import Task

    relative = write_code_review_request(
        tmp_path,
        Task("T002", "Login", "pending", ["REQ-001"], [], [], [], []),
        scope_report={
            "changed_paths": ["backend/src/login/service.py", "frontend/src/shared/auth.ts"],
            "out_of_scope": ["frontend/src/shared/auth.ts"],
        },
    )

    request_path = code_review_request_path(tmp_path, "T002")
    assert relative == str(request_path.relative_to(tmp_path))
    text = request_path.read_text(encoding="utf-8")
    assert "Perform an independent code review for task T002." in text
    assert "out_of_scope: frontend/src/shared/auth.ts" in text
    assert "Treat these paths as advisory, not an automatic failure." in text
    assert "necessary shared infrastructure or contract-aligned support" in text
    assert "Reply in raw JSON" in text


def test_execute_task_stops_for_external_review_when_tests_pass(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")


def test_session_for_task_does_not_rotate_while_retrying_same_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    loop = DeliveryLoop(tmp_path, runtime="opencode", max_tasks_per_session=1)
    existing = RuntimeSession(
        id="ses-existing",
        runtime="opencode",
        task_count=99,
        created_at="2026-06-24T00:00:00Z",
        status="active",
        title="T002 old",
        current_task_id="T002",
        last_heartbeat="2026-06-24T00:00:00Z",
    )
    touched: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr("delivery.loop.start_task_session", lambda project_root, runtime, task_id, title="": existing)
    monkeypatch.setattr("delivery.loop.retire_session", lambda project_root, session: (_ for _ in ()).throw(AssertionError("should not rotate same-task session")))
    monkeypatch.setattr("delivery.loop.save_current_session", lambda project_root, session: None)
    monkeypatch.setattr("delivery.loop.touch_session", lambda project_root, session, task_id=None, title=None: touched.append((task_id, title)))

    session = loop._session_for_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert session is existing
    assert touched == [("T002", "Login")]


def test_execute_task_skips_initial_runtime_when_scoped_changes_exist(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "feature.py"
    target.write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)
    target.write_text("print('updated')\n", encoding="utf-8")

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/feature.py"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/feature.py"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == []
    assert code_review_request_path(tmp_path, "T002").exists()


def test_execute_task_skips_relaunch_when_runtime_already_completed(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/feature.py"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "session_id": "ses-op-done",
            "completed_at": "2026-06-24T01:00:00Z",
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/feature.py"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == []


def test_execute_task_uses_persisted_recovery_prompt_before_validation(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T003", "title": "RFQ", "status": "pending", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/src/tests/test_rfq/test_rfq_crud.py"], "output_paths": ["backend/src/rfq/"]},
            ],
        },
    )
    save_task_runtime_state(tmp_path, "T003", {"recovery_prompt": "Recover this task from the stalled run."})
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "RFQ"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T003"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T003", "RFQ", "pending", ["REQ-002"], [], [], ["backend/src/tests/test_rfq/test_rfq_crud.py"], ["backend/src/rfq/"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == ["Recover this task from the stalled run."]


def test_execute_task_relaunches_after_review_changes_requested_even_with_scoped_changes(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

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
                    "title": "Login",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_login.py"],
                    "output_paths": ["backend/src/feature.py"],
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                },
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "session_id": "ses-op-done",
            "completed_at": "2026-06-24T01:00:00Z",
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: ["backend/src/feature.py"])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(
        Task(
            "T002",
            "Login",
            "pending",
            ["REQ-001"],
            [],
            [],
            ["backend/tests/test_login.py"],
            ["backend/src/feature.py"],
            review_status="changes_requested",
            review_artifact="docs/reviews/code-review-T002.md",
        )
    )

    assert success is False
    assert state == "review_pending"
    assert len(prompts) == 1


def test_git_stage_task_snapshot_ignores_preserved_out_of_scope_paths(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_stage_task_snapshot
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    in_scope = tmp_path / "backend" / "src" / "auth" / "service.py"
    out_of_scope = tmp_path / "frontend" / "src" / "features" / "review" / "ReviewFlow.tsx"
    in_scope.parent.mkdir(parents=True, exist_ok=True)
    out_of_scope.parent.mkdir(parents=True, exist_ok=True)
    in_scope.write_text("print('base service')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'base'\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    in_scope.write_text("print('changed service')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'dirty'\n", encoding="utf-8")

    task = Task(
        "T002",
        "Auth",
        "pending",
        ["REQ-001"],
        [],
        [],
        ["backend/tests/test_auth/test_login.py"],
        ["backend/src/auth/service.py"],
    )

    staged = git_stage_task_snapshot(tmp_path, task, preserved_paths=["frontend/src/features/review/ReviewFlow.tsx"])

    assert staged == ["backend/src/auth/service.py"]


def test_restore_paths_to_head_removes_staged_added_paths_not_in_head(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, restore_paths_to_head

    ensure_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    package_init = tmp_path / "backend" / "src" / "domain" / "__init__.py"
    package_init.parent.mkdir(parents=True, exist_ok=True)
    package_init.write_text("# domain package\n", encoding="utf-8")
    git(["add", "--", "backend/src/domain/__init__.py"], cwd=tmp_path)

    restored = restore_paths_to_head(tmp_path, ["backend/src/domain/__init__.py"])

    assert restored == ["backend/src/domain/__init__.py"]
    assert not package_init.exists()
    assert git(["status", "--porcelain", "--", "backend/src/domain/__init__.py"], cwd=tmp_path).stdout.strip() == ""


def test_park_task_exception_changes_handles_staged_added_paths_not_in_head(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, park_task_exception_changes
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    package_init = tmp_path / "backend" / "src" / "domain" / "__init__.py"
    model_path = tmp_path / "backend" / "src" / "domain" / "models.py"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    package_init.write_text("# domain package\n", encoding="utf-8")
    model_path.write_text("MODEL = True\n", encoding="utf-8")
    git(["add", "--", "backend/src/domain/__init__.py", "backend/src/domain/models.py"], cwd=tmp_path)

    task = Task(
        "T002",
        "Core Backend Foundation",
        "pending",
        ["REQ-001"],
        [],
        [],
        [],
        ["backend/src/domain/models.py"],
    )

    patch_relative_path = park_task_exception_changes(tmp_path, task)

    assert patch_relative_path == ".app-delivery-runtime/exception-patches/T002.patch"
    assert (tmp_path / patch_relative_path).exists()
    assert not package_init.exists()
    assert not model_path.exists()
    assert git(
        ["status", "--porcelain", "--", "backend/src/domain/__init__.py", "backend/src/domain/models.py"],
        cwd=tmp_path,
    ).stdout.strip() == ""


def test_gate_report_paths_are_treated_as_framework_managed() -> None:
    from delivery.loop_gitops import _is_always_allowed_framework_path

    assert _is_always_allowed_framework_path("docs/reviews/gate-report-gate-auth.md") is True


def test_execute_task_routes_new_out_of_scope_changes_into_review_without_relaunch(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    in_scope = tmp_path / "backend" / "src" / "auth" / "service.py"
    out_of_scope = tmp_path / "frontend" / "src" / "features" / "review" / "ReviewFlow.tsx"
    in_scope.parent.mkdir(parents=True, exist_ok=True)
    out_of_scope.parent.mkdir(parents=True, exist_ok=True)
    in_scope.write_text("print('base service')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'base'\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    in_scope.write_text("print('task change')\n", encoding="utf-8")
    out_of_scope.write_text("export const review = 'task dirt'\n", encoding="utf-8")

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
                    "title": "Auth",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "session_id": "ses-op-done",
            "completed_at": "2026-06-24T01:00:00Z",
            "initial_changed_paths": [],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []
    attempts: list[int] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Auth"
        last_heartbeat = "2026-06-24T00:00:00Z"
        current_task_id = "T002"

    def _passed_result(task_payload: dict[str, object], attempt: int) -> object:
        attempts.append(attempt)
        return type(
            "R",
            (),
            {
                "passed": True,
                "passed_count": 1,
                "failed_count": 0,
                "failures": [],
                "test_files": task_payload.get("output_tests", []),
                "test_types": ["unit"],
                "requirement_ids": task_payload.get("requirements", []),
                "task_id": task_payload.get("id", ""),
            },
        )()

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: _passed_result(task, attempt))

    success, state = loop._execute_task(Task("T002", "Auth", "pending", ["REQ-001"], [], [], ["backend/tests/test_auth/test_login.py"], ["backend/src/auth/service.py"]))

    assert success is False
    assert state == "review_pending"
    assert prompts == []
    assert attempts == [1]
    assert in_scope.read_text(encoding="utf-8") == "print('task change')\n"
    assert out_of_scope.read_text(encoding="utf-8") == "export const review = 'task dirt'\n"
    review_request = code_review_request_path(tmp_path, "T002").read_text(encoding="utf-8")
    assert "out_of_scope: frontend/src/features/review/ReviewFlow.tsx" in review_request
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["pending_scope_report"]["out_of_scope"] == ["frontend/src/features/review/ReviewFlow.tsx"]


def test_import_task_review_pass_commits_review_accepted_scope_paths(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.loop_review import import_task_review
    from delivery.task import all_tasks

    ensure_git_repo(tmp_path)
    in_scope = tmp_path / "backend" / "src" / "auth" / "service.py"
    out_of_scope = tmp_path / "frontend" / "src" / "shared" / "auth.ts"
    in_scope.parent.mkdir(parents=True, exist_ok=True)
    out_of_scope.parent.mkdir(parents=True, exist_ok=True)
    in_scope.write_text("print('base service')\n", encoding="utf-8")
    out_of_scope.write_text("export const auth = 'base'\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    in_scope.write_text("print('task change')\n", encoding="utf-8")
    out_of_scope.write_text("export const auth = 'task change'\n", encoding="utf-8")
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
                    "title": "Auth",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                    "status_session_id": "ses-op-1",
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "pending_scope_report": {
                "changed_paths": [
                    "backend/src/auth/service.py",
                    "frontend/src/shared/auth.ts",
                ],
                "staged_paths": ["backend/src/auth/service.py"],
                "out_of_scope": ["frontend/src/shared/auth.ts"],
                "preserved_paths": [],
            },
        },
    )

    input_path = tmp_path / "review-input.json"
    exit_code = import_task_review(
        tmp_path,
        "T002",
        {
            "status": "pass",
            "summary": "Scope deviation is acceptable for this task.",
            "findings": [],
            "requirement_assessment": [
                {"id": "REQ-001", "status": "pass", "notes": "The declared auth behavior is complete for this task."}
            ],
        },
        input_path,
    )

    assert exit_code == 0
    tasks = all_tasks(tmp_path)
    assert tasks[0].status == "verified"
    committed = git(["show", "--stat", "--name-only", "HEAD"], cwd=tmp_path).stdout
    assert "backend/src/auth/service.py" in committed
    assert "frontend/src/shared/auth.ts" in committed
    runtime_state = load_task_runtime_state(tmp_path, "T002")
    assert runtime_state["accepted_scope_paths"] == ["frontend/src/shared/auth.ts"]
    assert runtime_state["pending_scope_report"] is None


def test_import_task_review_pass_ignores_unrelated_later_task_changes(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.loop_review import import_task_review
    from delivery.task import all_tasks

    ensure_git_repo(tmp_path)
    t005_file = tmp_path / "backend" / "src" / "otif" / "engine.py"
    t007_file = tmp_path / "backend" / "src" / "ai_infra" / "gateway.py"
    t005_file.parent.mkdir(parents=True, exist_ok=True)
    t007_file.parent.mkdir(parents=True, exist_ok=True)
    t005_file.write_text("print('base otif')\n", encoding="utf-8")
    t007_file.write_text("print('base gateway')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    t005_file.write_text("print('t005 change')\n", encoding="utf-8")
    t007_file.write_text("print('t007 unrelated change')\n", encoding="utf-8")

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T005",
                    "title": "OTIF",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_otif/test_engine.py"],
                    "output_paths": ["backend/src/otif/"],
                    "status_session_id": "ses-op-5",
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T005",
        {
            "status": "completed",
            "pending_scope_report": {
                "changed_paths": [
                    "backend/src/otif/engine.py",
                    "backend/src/ai_infra/gateway.py",
                ],
                "staged_paths": ["backend/src/otif/engine.py"],
                "out_of_scope": [],
                "preserved_paths": [],
            },
        },
    )

    input_path = tmp_path / "review-input.json"
    exit_code = import_task_review(
        tmp_path,
        "T005",
        {
            "status": "pass",
            "summary": "Looks good.",
            "findings": [],
            "requirement_assessment": [
                {"id": "REQ-001", "status": "pass", "notes": "The declared OTIF behavior is complete for this task."}
            ],
        },
        input_path,
    )

    assert exit_code == 0
    tasks = all_tasks(tmp_path)
    assert tasks[0].status == "verified"
    committed = git(["show", "--stat", "--name-only", "HEAD"], cwd=tmp_path).stdout
    assert "backend/src/otif/engine.py" in committed
    assert "backend/src/ai_infra/gateway.py" not in committed


def test_import_task_review_changes_requested_retires_matching_active_session(tmp_path: Path) -> None:
    from delivery.loop_review import import_task_review
    from delivery.task import all_tasks

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
                    "title": "Auth",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                    "status_session_id": "ses-op-1",
                    "attempts": 1,
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "completed",
            "pending_scope_report": {
                "changed_paths": ["backend/src/auth/service.py"],
                "staged_paths": ["backend/src/auth/service.py"],
                "out_of_scope": [],
                "preserved_paths": [],
            },
        },
    )
    save_current_session(
        tmp_path,
        RuntimeSession(
            id="ses-op-1",
            runtime="opencode",
            task_count=3,
            created_at="2026-06-24T00:00:00Z",
            status="active",
            title="Auth",
            last_heartbeat="2026-06-24T00:05:00Z",
            current_task_id="T002",
        ),
    )

    input_path = tmp_path / "review-input.json"
    exit_code = import_task_review(
        tmp_path,
        "T002",
        {"status": "changes_requested", "summary": "Needs rework.", "findings": ["future task leakage"]},
        input_path,
    )

    assert exit_code == 2
    tasks = all_tasks(tmp_path)
    assert tasks[0].status == "pending"
    assert tasks[0].status_session_id == "ses-op-1"
    assert tasks[0].review_status == "changes_requested"
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"] is None
    assert session_payload["retired"][-1]["id"] == "ses-op-1"


def test_import_task_review_pass_requires_explicit_passing_requirement_assessments(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

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
                    "title": "Auth",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                }
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        import_task_review(
            tmp_path,
            "T002",
            {"status": "pass", "summary": "Looks good.", "findings": []},
            tmp_path / "review-input.json",
        )

    assert exc_info.value.code == "review_assessment_invalid"
    assert "status=pass requires explicit passing review assessments" in exc_info.value.message


def test_import_task_review_pass_rejects_non_passing_acceptance_assessment(tmp_path: Path) -> None:
    import pytest

    from delivery.errors import DeliveryError
    from delivery.loop_review import import_task_review

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
                    "title": "Auth",
                    "status": "review_pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": ["AS-001"],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_auth/test_login.py"],
                    "output_paths": ["backend/src/auth/service.py"],
                }
            ],
        },
    )

    with pytest.raises(DeliveryError) as exc_info:
        import_task_review(
            tmp_path,
            "T002",
            {
                "status": "pass",
                "summary": "Looks good.",
                "findings": [],
                "requirement_assessment": [
                    {"id": "REQ-001", "status": "pass", "notes": "Requirement is complete."}
                ],
                "acceptance_assessment": [
                    {"id": "AS-001", "status": "changes_requested", "notes": "Acceptance flow is incomplete."}
                ],
            },
            tmp_path / "review-input.json",
        )

    assert exc_info.value.code == "review_assessment_invalid"
    assert "non-passing acceptance assessments: AS-001" in exc_info.value.message


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

    assert "Do not reject the task only because it touched files outside the original output_paths" in prompt
    assert "user-visible frontend acceptance scenario" in prompt
    assert "browser coverage" in prompt


def test_execute_task_blocks_and_retires_session_on_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: (_ for _ in ()).throw(RuntimeErrorResponse("runtime command failed", "boom")))

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert item["status_session_id"] == "session-1"
    assert item["blocked_reason"] == "boom"
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"]["id"] == "session-1"


def test_execute_task_converts_unexpected_local_exception_into_task_exception(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: (_ for _ in ()).throw(ValueError("unexpected local failure")))

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert item["status_session_id"] == "session-1"
    assert item["blocked_reason"].startswith("framework task execution error: unexpected local failure")
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"]["id"] == "session-1"


def test_execute_task_runtime_failure_preserves_reusable_opencode_session(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path, runtime="opencode")

    class DummySession:
        id = "ses-op-1"
        runtime = "opencode"
        task_count = 2
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"
        current_task_id = "T002"
        last_heartbeat = "2026-06-24T00:05:00Z"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: (_ for _ in ()).throw(RuntimeErrorResponse("runtime command failed", "boom")))

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "exception"
    assert item["status_session_id"] == "ses-op-1"
    session_payload = load_session_state(tmp_path)
    assert session_payload["active"]["id"] == "ses-op-1"
    assert session_payload["active"]["runtime"] == "opencode"
    assert session_payload["active"]["current_task_id"] == "T002"
    assert session_payload["retired"] == []


def test_execute_task_reused_session_id_is_written_to_work_items(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    save_current_session(
        tmp_path,
        RuntimeSession(
            id="ses-shared",
            runtime="opencode",
            task_count=1,
            created_at="2026-06-24T00:00:00Z",
            status="active",
            title="Previous Task",
            last_heartbeat="2026-06-24T00:05:00Z",
            current_task_id=None,
        ),
    )
    loop = DeliveryLoop(tmp_path, runtime="opencode")
    session = RuntimeSession(
        id="ses-task",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:10:00Z",
        status="active",
        title="Login",
        last_heartbeat="2026-06-24T00:10:00Z",
        current_task_id="T002",
    )

    monkeypatch.setattr("delivery.loop.start_task_session", lambda project_root, runtime, task_id, title="": session)
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.git_head_sha", lambda project_root: "base123")

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "review_pending"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status_session_id"] == "ses-task"
    work_items_md = (tmp_path / "docs" / "work-items.md").read_text(encoding="utf-8")
    assert "ses-task" in work_items_md


def test_session_for_task_starts_new_session_for_new_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    loop = DeliveryLoop(tmp_path, runtime="opencode")
    new_session = RuntimeSession(
        id="ses-new",
        runtime="opencode",
        task_count=0,
        created_at="2026-06-24T00:10:00Z",
        status="active",
        title="New Task",
        last_heartbeat="2026-06-24T00:10:00Z",
        current_task_id="T002",
    )
    touched: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr("delivery.loop.start_task_session", lambda project_root, runtime, task_id, title="": new_session)
    monkeypatch.setattr("delivery.loop.touch_session", lambda project_root, session, task_id=None, title=None: touched.append((task_id, title)))

    session = loop._session_for_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert session.id == "ses-new"
    assert touched == [("T002", "Login")]


def test_execute_task_does_not_loop_review_inside_core(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": task.get("output_tests", []), "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["backend/tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "review_pending"
    assert len(prompts) == 1
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["attempts"] == 1
    assert payload["items"][0]["status"] == "review_pending"
    assert code_review_request_path(tmp_path, "T002").exists()


def test_execute_task_exception_parks_scope_changes(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "workspace").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "workspace" / "router.py"
    target.write_text("print('base')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    target.write_text("print('changed')\n", encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Workspace", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/workspace/router.py"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    prompts: list[str] = []

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Workspace"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: prompts.append(prompt) or {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": [], "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.write_code_review_request", lambda project_root, task, scope_report=None: (_ for _ in ()).throw(RuntimeError("review request failed")))

    success, state = loop._execute_task(Task("T002", "Workspace", "pending", ["REQ-001"], [], [], [], ["backend/src/workspace/router.py"]))

    assert success is False
    assert state == "exception"
    assert target.read_text(encoding="utf-8") == "print('base')\n"
    patch_path = tmp_path / ".app-delivery-runtime" / "exception-patches" / "T002.patch"
    assert patch_path.exists()


def test_project_summary_reports_unplanned_requirements(tmp_path: Path) -> None:
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": False, "scores": {}}})
    save_session_state(tmp_path, {"active": {"id": "session-1", "runtime": "claude", "task_count": 1, "created_at": "2026-06-24T00:00:00Z", "status": "active", "title": "Feature", "current_task_id": "T002", "last_heartbeat": "2026-06-24T00:05:00Z"}, "retired": []})

    payload = project_summary(tmp_path)

    assert payload["requirements"]["total"] == 2
    assert payload["requirements"]["unplanned"] == ["REQ-002"]
    assert payload["active_session"]["id"] == "session-1"
    assert payload["active_task"]["id"] == "T002"
    assert payload["delivery_claim_allowed"] is False
    assert payload["control_status_hint"] == "not_ready"
    assert "planning" in payload
    assert "tokens" in payload
    assert "task_metrics" in payload


def test_execute_task_writes_exception_report_artifact(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    target = tmp_path / "backend" / "src" / "workspace" / "router.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('base')\n", encoding="utf-8")
    ensure_git_repo(tmp_path)
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Workspace", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": ["backend/src/workspace/router.py"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Workspace"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: {"text": "ok"})
    monkeypatch.setattr("delivery.loop.run_task_tests", lambda project_root, task, attempt=1: type("R", (), {"passed": True, "passed_count": 1, "failed_count": 0, "failures": [], "test_files": [], "test_types": ["unit"], "requirement_ids": task.get("requirements", []), "task_id": task.get("id", "")})())
    monkeypatch.setattr("delivery.loop.task_scoped_changed_paths", lambda project_root, task: [])
    monkeypatch.setattr("delivery.loop.git_stage_task_snapshot", lambda project_root, task, extra_paths=None, preserved_paths=None: [])
    monkeypatch.setattr("delivery.loop.write_code_review_request", lambda project_root, task, scope_report=None: (_ for _ in ()).throw(RuntimeError("review request failed")))

    success, state = loop._execute_task(Task("T002", "Workspace", "pending", ["REQ-001"], [], [], [], ["backend/src/workspace/router.py"]))

    assert success is False
    assert state == "exception"
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["review_artifact"] == "docs/reviews/exception-report-T002.md"
    report_text = (tmp_path / "docs" / "reviews" / "exception-report-T002.md").read_text(encoding="utf-8")
    assert "# Exception Report" in report_text
    assert "review request failed" in report_text


def test_project_summary_prefers_active_task_record_phase(tmp_path: Path) -> None:
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": False, "scores": {}}})
    save_session_state(tmp_path, {"active": {"id": "session-1", "runtime": "claude", "task_count": 1, "created_at": "2026-06-24T00:00:00Z", "status": "active", "title": "Feature", "current_task_id": "T002", "last_heartbeat": "2026-06-24T00:05:00Z"}, "retired": []})
    active_dir = tmp_path / ".app-delivery-runtime" / "active-tasks"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "review.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "task_id": "T002",
                "task_title": "review-T002",
                "phase": "review",
                "updated_at": "2026-06-24T00:06:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = project_summary(tmp_path)

    assert payload["active_task"]["id"] == "T002"
    assert payload["active_task"]["phase"] == "review"


def test_project_summary_marks_delivery_claim_allowed_only_when_final_is_verified(tmp_path: Path) -> None:
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": []},
                {"id": "T-FINAL", "title": "最终验证", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    save_test_results(tmp_path, {"schema_version": "1", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "results": [], "full_suite_results": {"passed": True, "scores": {}}})

    payload = project_summary(tmp_path)

    assert payload["delivery_claim_allowed"] is True
    assert payload["control_status_hint"] == "complete"


def test_git_commit_task_allows_always_managed_artifacts(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "feature.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "work-items.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "work-items.md").write_text("# work items\n", encoding="utf-8")
    (tmp_path / "docs" / "test-results.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "project-summary.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "project-summary.md").write_text("# summary\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "prompts" / "T002.md").write_text("prompt\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "session-state.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "logs" / "turn-1.log").write_text("runtime log\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "active-tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "active-tasks" / "T002.json").write_text('{"pid": 0}\n', encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "task-log.jsonl").write_text('{"message":"started"}\n', encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "feature.py").write_text("print('updated')\n", encoding="utf-8")
    (tmp_path / "docs" / "work-items.json").write_text('{"updated": true}\n', encoding="utf-8")
    (tmp_path / "docs" / "work-items.md").write_text("# updated\n", encoding="utf-8")
    (tmp_path / "docs" / "test-results.json").write_text('{"results": []}\n', encoding="utf-8")
    (tmp_path / "docs" / "project-summary.json").write_text('{"updated": true}\n', encoding="utf-8")
    (tmp_path / "docs" / "project-summary.md").write_text("# updated summary\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "prompts" / "T002.md").write_text("updated prompt\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "session-state.json").write_text('{"active": null}\n', encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "logs" / "turn-1.log").write_text("updated runtime log\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "task-log.jsonl").write_text('{"message":"updated"}\n', encoding="utf-8")

    task = Task("T002", "Feature", "pending", ["REQ-001"], [], [], [], ["backend/src/feature.py"])

    commit_sha = git_commit_task(tmp_path, task, "feat(T002): feature")

    assert commit_sha
    committed_paths = git(["show", "--name-only", "--pretty=", "HEAD"], cwd=tmp_path).stdout
    assert ".app-delivery-runtime/logs/turn-1.log" not in committed_paths
    assert ".app-delivery-runtime/task-log.jsonl" not in committed_paths


def test_git_commit_task_allows_t001_foundation_entrypoints_and_lockfiles(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "e2e").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "backend" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "backend" / "conftest.py").write_text("# root conftest\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "main.py").write_text("print('main')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core" / "event_bus.py").write_text("print('event')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "core" / "test_event_bus.py").write_text("def test_event():\n    assert True\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => {})\n", encoding="utf-8")
    (tmp_path / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "frontend" / "e2e" / "app-shell.spec.ts").write_text("import { test } from '@playwright/test'\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='demo-updated'\n", encoding="utf-8")
    (tmp_path / "backend" / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    (tmp_path / "backend" / "conftest.py").write_text("# updated root conftest\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "main.py").write_text("print('updated main')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core" / "event_bus.py").write_text("print('updated event')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "core" / "test_event_bus.py").write_text("def test_event():\n    assert 1 == 1\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export default function App() { return 'ok' }\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => expect(true).toBe(true))\n", encoding="utf-8")
    (tmp_path / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.1'\n", encoding="utf-8")
    (tmp_path / "frontend" / "e2e" / "app-shell.spec.ts").write_text("import { test, expect } from '@playwright/test'\n", encoding="utf-8")

    task = Task(
        "T001",
        "共享基础设施",
        "pending",
        [],
        [],
        ["T000"],
        [
            "backend/tests/core/test_event_bus.py",
            "frontend/e2e/app-shell.spec.ts",
        ],
        [
            "backend/pyproject.toml",
            "backend/uv.lock",
            "backend/conftest.py",
            "backend/src/main.py",
            "backend/src/core/",
            "backend/src/shared/",
            "backend/src/tests/",
            "backend/tests/core/",
            "frontend/src/shared/",
            "frontend/src/lib/",
            "frontend/src/layouts/",
            "frontend/src/styles/",
            "frontend/src/App.tsx",
            "frontend/src/App.test.tsx",
            "frontend/pnpm-lock.yaml",
        ],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T001): shared infrastructure")

    assert commit_sha


def test_git_commit_task_allows_feature_integration_entry_files(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "frontend" / "src" / "layouts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "features" / "city").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export function App(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => {})\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "layouts" / "AppShell.tsx").write_text("export function AppShell(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "city" / "CityListPage.tsx").write_text("export function CityListPage(){return null}\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export function App(){return 'city'}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.test.tsx").write_text("test('app', () => expect(true).toBe(true))\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "layouts" / "AppShell.tsx").write_text("export function AppShell(){return 'shell'}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "city" / "CityListPage.tsx").write_text("export function CityListPage(){return 'page'}\n", encoding="utf-8")

    task = Task(
        "T003",
        "城市、区域与车辆管理",
        "pending",
        ["REQ-001"],
        [],
        ["T001"],
        ["frontend/e2e/city-management.spec.ts"],
        ["frontend/src/features/city/"],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T003): city feature")

    assert commit_sha


def test_git_commit_task_allows_package_init_for_declared_python_module_files(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "merchant" / "services").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "merchant" / "router.py").write_text("print('router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "services" / "merchant_service.py").write_text("print('service')\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "merchant" / "router.py").write_text("print('updated router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "services" / "merchant_service.py").write_text("print('updated service')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "__init__.py").write_text("\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "merchant" / "services" / "__init__.py").write_text("\n", encoding="utf-8")

    task = Task(
        "T004",
        "仓站与商家基础管理",
        "pending",
        ["REQ-003"],
        [],
        ["T003"],
        ["backend/tests/merchant/test_merchant_api.py"],
        [
            "backend/src/merchant/router.py",
            "backend/src/merchant/services/merchant_service.py",
        ],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T004): merchant feature")

    assert commit_sha


def test_git_commit_task_allows_feature_support_files_for_declared_services_and_pages(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "station" / "services").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "station" / "models").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "station" / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "station").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "features" / "station" / "pages").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "features" / "station" / "components").mkdir(parents=True, exist_ok=True)

    (tmp_path / "backend" / "src" / "station" / "services" / "pick_service.py").write_text("print('pick')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "handoff_service.py").write_text("print('handoff')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "readiness_service.py").write_text("print('ready')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "station_service.py").write_text("print('station')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "router.py").write_text("print('router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "models" / "station_operations.py").write_text("print('models')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "schemas" / "operations.py").write_text("print('schemas')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "station" / "test_pick_workflow.py").write_text("def test_pick():\n    assert True\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "pages" / "PickPage.tsx").write_text("export function PickPage(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "pages" / "StationDetailPage.tsx").write_text("export function StationDetailPage(){return null}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "api.ts").write_text("export const api = {}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "components" / "AlertPanel.tsx").write_text("export function AlertPanel(){return null}\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "src" / "station" / "services" / "pick_service.py").write_text("print('updated pick')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "services" / "station_service.py").write_text("print('updated station')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "router.py").write_text("print('updated router')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "models" / "station_operations.py").write_text("print('updated models')\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "station" / "schemas" / "operations.py").write_text("print('updated schemas')\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "station" / "conftest.py").write_text("def helper():\n    return True\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "api.ts").write_text("export const api = { updated: true }\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "features" / "station" / "components" / "AlertPanel.tsx").write_text("export function AlertPanel(){return 'ok'}\n", encoding="utf-8")

    task = Task(
        "T007",
        "仓站运营：拣货、取货与备货确认",
        "pending",
        ["REQ-012"],
        [],
        ["T006"],
        ["backend/tests/station/test_pick_workflow.py", "frontend/e2e/station-operations.spec.ts"],
        [
            "backend/src/station/services/pick_service.py",
            "backend/src/station/services/handoff_service.py",
            "backend/src/station/services/readiness_service.py",
            "frontend/src/features/station/pages/PickPage.tsx",
            "frontend/src/features/station/pages/StationDetailPage.tsx",
        ],
    )

    commit_sha = git_commit_task(tmp_path, task, "feat(T007): station ops")

    assert commit_sha


def test_git_stage_task_snapshot_includes_untracked_foundation_tests(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_stage_task_snapshot
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "e2e").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "main.py").write_text("print('main')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "backend" / "tests" / "core" / "test_auth.py").write_text("def test_auth():\n    assert True\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "core" / "auth.py").write_text("AUTH = True\n", encoding="utf-8")

    task = Task(
        "T001",
        "共享基础设施",
        "pending",
        [],
        [],
        ["T000"],
        ["backend/tests/core/"],
        ["backend/src/core/", "backend/tests/core/"],
    )

    staged = git_stage_task_snapshot(tmp_path, task)

    assert "backend/tests/core/test_auth.py" in staged
    assert "backend/src/core/auth.py" in staged
    diff_names = git(["diff", "--cached", "--name-only"], cwd=tmp_path).stdout.splitlines()
    assert "backend/tests/core/test_auth.py" in diff_names
    assert "backend/src/core/auth.py" in diff_names


def test_git_commit_task_allows_t000_planning_artifacts(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mock-server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "modules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "adr").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "ui").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "architecture.md").write_text("arch\n", encoding="utf-8")
    (tmp_path / "docs" / "shared-components.md").write_text("shared\n", encoding="utf-8")
    (tmp_path / "docs" / "requirements.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "project-bootstrap.json").write_text('{"runtime": "claude"}\n', encoding="utf-8")
    (tmp_path / "docs" / "test-plan.json").write_text('{"coverage": []}\n', encoding="utf-8")
    (tmp_path / "docs" / "modules" / "01-module.md").write_text("module\n", encoding="utf-8")
    (tmp_path / "docs" / "adr" / "001-adr.md").write_text("adr\n", encoding="utf-8")
    (tmp_path / "docs" / "ui" / "states.md").write_text("states\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "stage-inputs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
    (tmp_path / "CODE_MAP.md").write_text("map\n", encoding="utf-8")

    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "docs" / "architecture.md").write_text("updated\n", encoding="utf-8")
    (tmp_path / "docs" / "project-bootstrap.json").write_text('{"runtime": "claude", "updated": true}\n', encoding="utf-8")
    (tmp_path / "docs" / "modules" / "01-module.md").write_text("updated\n", encoding="utf-8")
    (tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json").write_text('{"updated": true}\n', encoding="utf-8")

    task = Task("T000", "脚手架", "pending", [], [], [], [], ["backend/", "frontend/"])

    commit_sha = git_commit_task(tmp_path, task, "chore(T000): scaffold")

    assert commit_sha


def test_git_commit_task_allows_t000_raw_planning_evidence(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_commit_task, task_scoped_changed_paths
    from delivery.task import Task

    ensure_git_repo(tmp_path)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mock-server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "project-bootstrap.json").write_text('{"runtime": "opencode"}\n', encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)

    (tmp_path / "docs" / "arch-raw.json").write_text('{"architecture_md": "raw"}\n', encoding="utf-8")
    task = Task("T000", "脚手架", "pending", [], [], [], [], ["backend/", "frontend/"])

    scoped = task_scoped_changed_paths(tmp_path, task)
    commit_sha = git_commit_task(tmp_path, task, "chore(T000): scaffold")

    assert "docs/arch-raw.json" in scoped
    assert commit_sha


def test_git_commit_task_raises_when_commit_fails(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git, git_commit_task as real_git_commit_task
    from delivery.task import Task
    import delivery.loop_gitops as loop_gitops

    ensure_git_repo(tmp_path)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "file.py").write_text("print('x')\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "init"], cwd=tmp_path)
    (tmp_path / "backend" / "file.py").write_text("print('y')\n", encoding="utf-8")
    task = Task("T000", "脚手架", "pending", [], [], [], [], ["backend/"])

    original_git = loop_gitops.git

    def fake_git(args, *, cwd):
        if args[:2] == ["commit", "-m"]:
            class Result:
                returncode = 1
                stdout = "simulated git commit failure"
            return Result()
        return original_git(args, cwd=cwd)

    loop_gitops.git = fake_git

    try:
        real_git_commit_task(tmp_path, task, "chore(T000): scaffold")
    except RuntimeError as exc:
        assert "simulated git commit failure" in str(exc)
    else:
        raise AssertionError("expected git commit failure to raise")
    finally:
        loop_gitops.git = original_git


def test_execute_task_blocks_if_task_disappears_mid_run(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import Task

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Login", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_login.py"], "output_paths": ["backend/src/login/"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    class DummySession:
        id = "session-1"
        runtime = "claude"
        task_count = 0
        created_at = "2026-06-24T00:00:00Z"
        status = "active"
        title = "Login"

    monkeypatch.setattr(loop, "_session_for_task", lambda task: DummySession())
    monkeypatch.setattr("delivery.loop.execute_in_session", lambda project_root, session, prompt: save_work_items(project_root, {"schema_version": "2", "project": "demo", "generated_at": "2026-06-24T00:00:00Z", "last_updated_commit": "", "items": []}) or {"text": "ok"})

    success, state = loop._execute_task(Task("T002", "Login", "pending", ["REQ-001"], [], [], ["tests/test_login.py"], ["backend/src/login/"]))

    assert success is False
    assert state == "blocked"


def test_run_continues_after_task_exception_when_other_task_is_ready(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks, mark_task, save_tasks

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"]},
                {"id": "T002", "title": "功能", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T001", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(loop, "_pause_requested", lambda: False)
    monkeypatch.setattr(loop, "all_done", lambda tasks: False)

    def fake_execute(task):
        tasks = all_tasks(tmp_path)
        if task.id == "T001":
            save_tasks(tmp_path, mark_task(tasks, "T001", "exception", blocked_reason="need follow-up"))
            return (False, "exception")
        save_tasks(tmp_path, mark_task(tasks, "T002", "verified", verified_at="2026-06-24T00:30:00Z"))
        return (True, "verified")

    monkeypatch.setattr(loop, "_execute_task", fake_execute)

    result = loop.run()

    assert result == {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": ["T001"]}
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["T001"]["status"] == "exception"
    assert by_id["T002"]["status"] == "verified"


def test_run_retries_exception_task_when_pending_tasks_depend_on_it(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"], "blocked_reason": "need retry"},
                {"id": "T002", "title": "功能", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000", "T001", "T002"], "output_tests": [], "output_paths": []},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    calls: list[str] = []

    def fake_pause_requested() -> bool:
        return len(calls) > 0

    monkeypatch.setattr(loop, "_pause_requested", fake_pause_requested)

    def fake_fix_exception_task(tasks):
        return next(task for task in tasks if task.id == "T001")

    monkeypatch.setattr(loop, "_next_exception_task_to_retry", fake_fix_exception_task)

    def fake_execute(task):
        calls.append(task.id)
        return (False, "exception")

    monkeypatch.setattr(loop, "_execute_task", fake_execute)

    result = loop.run()

    assert calls == ["T001"]
    assert result == {"status": "paused"}


def test_run_does_not_retry_exception_task_after_repeated_identical_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T000", "title": "脚手架", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
                {"id": "T001", "title": "共享基础设施", "status": "exception", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": [], "output_paths": ["backend/src/shared/"], "blocked_reason": "same scope violation"},
                {"id": "T002", "title": "功能", "status": "pending", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature/"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T001",
        {
            "status": "failed",
            "failure_kind": "scope_violation",
            "failure_signature": "abc123",
            "failure_count": 2,
        },
    )
    loop = DeliveryLoop(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(loop, "_pause_requested", lambda: False)
    monkeypatch.setattr(loop, "all_done", lambda tasks: False)
    monkeypatch.setattr(loop, "_execute_task", lambda task: calls.append(task.id) or (False, "exception"))

    result = loop.run()

    assert calls == []
    assert result == {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": ["T001"]}


def test_run_does_not_retry_exception_task_after_local_repair_budget_is_consumed(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T000",
                    "title": "脚手架",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": [],
                },
                {
                    "id": "T001",
                    "title": "共享基础设施",
                    "status": "exception",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/"],
                    "blocked_reason": "failed after retry budget was exhausted",
                    "attempts": 4,
                },
                {
                    "id": "T002",
                    "title": "功能",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T001"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature/"],
                },
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(loop, "_pause_requested", lambda: False)
    monkeypatch.setattr(loop, "all_done", lambda tasks: False)
    monkeypatch.setattr(loop, "_execute_task", lambda task: calls.append(task.id) or (False, "exception"))

    result = loop.run()

    assert calls == []
    assert result == {"status": "exception", "reason": "no runnable tasks", "exception_task_ids": ["T001"]}


def test_recover_preserves_matching_active_session_for_reuse(tmp_path: Path) -> None:
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
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/feature/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                    "attempts": 2,
                }
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
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    updated = recover(tmp_path)

    payload = load_session_state(tmp_path)
    assert updated[0].status == "active"
    assert updated[0].status_session_id == "session-1"
    assert updated[0].started_at == "2026-06-24T00:01:00Z"
    assert updated[0].attempts == 2
    assert payload["active"]["id"] == "session-1"
    assert payload["active"]["current_task_id"] == "T002"
    assert payload["active"]["title"] == "Feature"
    assert payload["retired"] == []


def test_recover_keeps_running_active_task_in_place(tmp_path: Path) -> None:
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
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/feature/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                    "attempts": 2,
                }
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "running",
            "runtime_pid": 1,
            "wrapper_pid": 1,
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
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "active"
    assert updated[0].status_session_id == "session-1"
    assert updated[0].started_at == "2026-06-24T00:01:00Z"
    assert updated[0].attempts == 2


def test_run_prefers_resuming_active_task_before_pending_tasks(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Active feature",
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/active/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                },
                {
                    "id": "T003",
                    "title": "Pending feature",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/pending/"],
                },
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
                "title": "Active feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    seen: list[str] = []

    def fake_execute_task(self, task):
        seen.append(task.id)
        return False, "review_pending"

    monkeypatch.setattr(DeliveryLoop, "_execute_task", fake_execute_task)

    loop = DeliveryLoop(tmp_path, runtime="claude")
    result = loop.run()

    assert seen == ["T002"]
    assert result["status"] == "review_pending"
    assert result["task_id"] == "T002"


def test_run_resumes_interrupted_active_task_before_pending_tasks(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Interrupted active feature",
                    "status": "active",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/active/"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                },
                {
                    "id": "T003",
                    "title": "Pending feature",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/pending/"],
                },
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T002",
        {
            "status": "running",
            "runtime_pid": 0,
            "wrapper_pid": 0,
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
                "title": "Interrupted active feature",
                "current_task_id": "T002",
                "last_heartbeat": "2026-06-24T00:05:00Z",
            },
            "retired": [],
        },
    )

    seen: list[str] = []

    def fake_execute_task(self, task):
        seen.append(task.id)
        return False, "review_pending"

    monkeypatch.setattr(DeliveryLoop, "_execute_task", fake_execute_task)

    loop = DeliveryLoop(tmp_path, runtime="claude")
    result = loop.run()

    assert seen == ["T002"]
    assert result["status"] == "review_pending"
    assert result["task_id"] == "T002"


def test_recover_does_not_reset_pending_tasks_with_history(tmp_path: Path) -> None:
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
                    "title": "Shared",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/"],
                    "git_commit": "abc123",
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/code-review-T001.md",
                    "reviewed_at": "2026-06-24T00:02:00Z",
                    "attempts": 2,
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "pending"
    assert updated[0].git_commit == "abc123"
    assert updated[0].review_status == "changes_requested"
    assert updated[0].attempts == 2
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["id"] == "T001"
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["git_commit"] == "abc123"
    assert payload["items"][0]["review_status"] == "changes_requested"
    assert payload["items"][0]["attempts"] == 2


def test_recover_preserves_changes_requested_feedback_when_resetting_stale_pending_metadata(tmp_path: Path) -> None:
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
                    "title": "Login",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["backend/tests/test_login.py"],
                    "output_paths": ["backend/src/login/"],
                    "status_session_id": "ses-stale",
                    "started_at": "2026-06-24T00:01:00Z",
                    "completed_at": "2026-06-24T00:02:00Z",
                    "review_status": "changes_requested",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "reviewed_at": "2026-06-24T00:03:00Z",
                    "blocked_reason": "Tenant isolation behavior is still missing.",
                    "attempts": 2,
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "pending"
    assert updated[0].status_session_id is None
    assert updated[0].started_at is None
    assert updated[0].completed_at is None
    assert updated[0].review_status == "changes_requested"
    assert updated[0].review_artifact == "docs/reviews/code-review-T002.md"
    assert updated[0].blocked_reason == "Tenant isolation behavior is still missing."
    assert updated[0].attempts == 2


def test_recover_reconciles_verified_task_from_pass_review_and_commit(tmp_path: Path) -> None:
    from delivery.loop_gitops import ensure_git_repo, git

    ensure_git_repo(tmp_path)
    (tmp_path / "backend" / "src" / "shared").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "backend" / "src" / "shared" / "response.py"
    target.write_text("print('base')\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    review_path = tmp_path / "docs" / "reviews" / "code-review-T001.md"
    review_path.write_text("status: pass\nreview_type: code\nwork_item: T001\n", encoding="utf-8")
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "feat(T001): Shared"], cwd=tmp_path)

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
                    "title": "Shared",
                    "status": "active",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/response.py"],
                    "status_session_id": "session-1",
                    "started_at": "2026-06-24T00:01:00Z",
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "verified"
    assert updated[0].git_commit is not None
    assert updated[0].review_status == "pass"
    assert updated[0].review_artifact == "docs/reviews/code-review-T001.md"


def test_recover_resets_invalid_verified_scaffold_without_git_history(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T000",
                    "title": "脚手架",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/"],
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T000.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T001",
                    "title": "共享基础设施",
                    "status": "pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": [],
                    "output_paths": ["backend/src/core/"],
                },
            ],
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T000"].status == "pending"
    assert by_id["T000"].review_status is None
    assert by_id["T000"].review_artifact is None
    assert by_id["T000"].verified_at is None


def test_recover_resets_verified_task_without_commit_or_review_evidence(tmp_path: Path) -> None:
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
                    "title": "共享基础设施",
                    "status": "verified",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["backend/src/shared/"],
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T001.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                }
            ],
        },
    )

    updated = recover(tmp_path)

    assert updated[0].status == "pending"
    assert updated[0].git_commit is None
    assert updated[0].review_status is None
    assert updated[0].review_artifact is None
    assert updated[0].verified_at is None
    assert updated[0].blocked_reason == "verification evidence missing: missing task commit and pass code review evidence"


def test_recover_reopens_verified_task_when_validation_gate_is_blocked(tmp_path: Path, monkeypatch) -> None:
    from delivery.loop_gitops import ensure_git_repo, git

    ensure_git_repo(tmp_path)
    (tmp_path / "frontend" / "src" / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "auth" / "LoginPage.tsx").write_text("export const LoginPage = () => null\n", encoding="utf-8")
    (tmp_path / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "reviews" / "code-review-T002.md").write_text(
        "status: pass\nreview_type: code\nwork_item: T002\n",
        encoding="utf-8",
    )
    git(["add", "--", "."], cwd=tmp_path)
    git(["commit", "-m", "feat(T002): Auth"], cwd=tmp_path)
    commit_sha = git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

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
                    "title": "Auth",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/src/auth/LoginPage.test.tsx"],
                    "output_paths": ["frontend/src/auth/"],
                    "git_commit": commit_sha,
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T003",
                    "title": "Review",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/review-flow.spec.ts"],
                    "output_paths": ["frontend/src/rfq/"],
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.refresh_gates",
        lambda project_root: {
            "gates": [
                {
                    "id": "GATE-auth",
                    "status": "blocked",
                    "repair_candidates": ["T002"],
                }
            ]
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T002"].status == "pending"
    assert by_id["T002"].review_status is None
    assert by_id["T002"].verified_at is None
    assert by_id["T002"].blocked_reason == "validation gate GATE-auth requires repair"


def test_run_reopens_blocked_gate_repair_task_before_downstream_execution(tmp_path: Path, monkeypatch) -> None:
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
                    "title": "Auth",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/src/auth/LoginPage.test.tsx"],
                    "output_paths": ["frontend/src/auth/"],
                    "git_commit": "abc123",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T003",
                    "title": "Review",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/review-flow.spec.ts"],
                    "output_paths": ["frontend/src/rfq/"],
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.refresh_gates",
        lambda project_root: {
            "gates": [
                {
                    "id": "GATE-auth",
                    "status": "blocked",
                    "repair_candidates": ["T002"],
                }
            ]
        },
    )

    executed: list[str] = []

    def fake_execute(task):
        executed.append(task.id)
        return False, "review_pending"

    loop = DeliveryLoop(tmp_path)
    monkeypatch.setattr(loop, "_execute_task", fake_execute)

    result = loop.run()

    assert executed == ["T002"]
    assert result["status"] == "review_pending"
    assert result["task_id"] == "T002"


def test_recover_does_not_reopen_multiple_verified_gate_repair_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("delivery.loop.repair_invalid_verified_tasks", lambda project_root, tasks: (tasks, {}))
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
                    "title": "Auth",
                    "status": "verified",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": ["frontend/src/auth/LoginPage.test.tsx"],
                    "output_paths": ["frontend/src/auth/"],
                    "git_commit": "abc123",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T002.md",
                    "verified_at": "2026-06-24T00:02:00Z",
                },
                {
                    "id": "T003",
                    "title": "Review",
                    "status": "verified",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T002"],
                    "output_tests": ["frontend/e2e/review-flow.spec.ts"],
                    "output_paths": ["frontend/src/rfq/"],
                    "git_commit": "def456",
                    "review_status": "pass",
                    "review_artifact": "docs/reviews/code-review-T003.md",
                    "verified_at": "2026-06-24T00:03:00Z",
                },
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.refresh_gates",
        lambda project_root: {
            "gates": [
                {
                    "id": "GATE-release",
                    "status": "blocked",
                    "repair_candidates": ["T002", "T003"],
                }
            ]
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T002"].status == "verified"
    assert by_id["T003"].status == "verified"


def test_final_verify_creates_dedicated_repair_task_without_reopening_verified_tasks(tmp_path: Path, monkeypatch) -> None:
    from delivery.verify import TestResult
    from delivery.task import all_tasks

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/service.py", "frontend/src/routes/login.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Review", "status": "verified", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/review.spec.ts"], "output_paths": ["backend/src/review/service.py", "frontend/src/routes/app/rfqs/$rfqId/route.tsx"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr(
        "delivery.loop.check_test_type_coverage",
        lambda project_root: [("REQ-002", "integration"), ("REQ-001", "typecheck"), ("REQ-001", "lint"), ("REQ-001", "build")],
    )

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    by_id = {task.id: task for task in tasks}
    repair_task_id = result["repair_task_id"]
    assert repair_task_id is not None
    assert by_id["T002"].status == "verified"
    assert by_id["T003"].status == "verified"
    assert by_id[repair_task_id].task_kind == "repair"
    assert by_id[repair_task_id].status == "pending"
    assert "frontend/e2e/auth.spec.ts" in by_id[repair_task_id].output_tests
    assert "npm run typecheck" in by_id[repair_task_id].output_tests
    assert "backend/tests/" in by_id[repair_task_id].output_tests
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == repair_task_id


def test_final_verify_includes_blocked_gate_missing_type_specs_in_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.state import save_gates
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/service.py", "frontend/src/routes/login.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Review", "status": "verified", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/review.spec.ts"], "output_paths": ["backend/src/review/service.py", "frontend/src/routes/app/rfqs/$rfqId/route.tsx"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
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
                    "id": "GATE-release",
                    "kind": "release",
                    "title": "Release gate",
                    "status": "blocked",
                    "scope_tasks": ["T002", "T003"],
                    "scope_requirements": ["REQ-001", "REQ-002"],
                    "required_test_types": ["lint", "typecheck", "build", "browser"],
                    "missing_test_types": ["lint", "typecheck", "build"],
                    "repair_candidates": ["T002", "T003"],
                    "report_artifact": "docs/reviews/gate-report-GATE-release.md",
                    "source": "task-decompose",
                }
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [("REQ-002", "integration")])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    by_id = {task.id: task for task in tasks}
    repair_task_id = result["repair_task_id"]
    assert repair_task_id is not None
    assert "npm run lint" in by_id[repair_task_id].output_tests
    assert "npm run typecheck" in by_id[repair_task_id].output_tests
    assert "npm run build" in by_id[repair_task_id].output_tests
    assert "backend/tests/" in by_id[repair_task_id].output_tests


def test_final_verify_prioritizes_missing_type_specs_when_failed_test_list_is_already_full(tmp_path: Path, monkeypatch) -> None:
    from delivery.state import save_gates
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["backend/src/auth/service.py", "frontend/src/routes/login.tsx"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Review", "status": "verified", "requirements": ["REQ-002"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/review.spec.ts"], "output_paths": ["backend/src/review/service.py", "frontend/src/routes/app/rfqs/$rfqId/route.tsx"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002", "T003"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
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
                    "id": "GATE-release",
                    "kind": "release",
                    "title": "Release gate",
                    "status": "blocked",
                    "scope_tasks": ["T002", "T003"],
                    "scope_requirements": ["REQ-001", "REQ-002"],
                    "required_test_types": ["lint", "typecheck", "build", "browser"],
                    "missing_test_types": ["lint", "typecheck", "build"],
                    "repair_candidates": ["T002", "T003"],
                    "report_artifact": "docs/reviews/gate-report-GATE-release.md",
                    "source": "task-decompose",
                }
            ],
        },
    )
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint src/", "build": "vite build", "e2e": "playwright test"}}), encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    loop = DeliveryLoop(tmp_path)

    failed_specs = [f"tests/test_{index}.py" for index in range(10)]
    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=failed_specs,
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=10,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [("REQ-002", "integration")])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    by_id = {task.id: task for task in tasks}
    repair_task_id = result["repair_task_id"]
    assert repair_task_id is not None
    assert "npm run lint" in by_id[repair_task_id].output_tests
    assert "npm run typecheck" in by_id[repair_task_id].output_tests
    assert "npm run build" in by_id[repair_task_id].output_tests
    assert "backend/tests/" in by_id[repair_task_id].output_tests


def test_final_verify_reuses_exception_repair_task_without_creating_another(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
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
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "exception", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "blocked_reason": "repair budget exhausted", "attempts": 3},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {
            "task_id": "T-FINAL",
            "repair_candidates": ["T002"],
            "repair_task_id": "T003",
            "final_verify_status": "repair_required",
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    repair_tasks = [task for task in tasks if task.task_kind == "repair"]
    assert result["repair_task_id"] == "T003"
    assert len(repair_tasks) == 1
    assert repair_tasks[0].id == "T003"
    assert repair_tasks[0].status == "exception"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_task_id"] == "T003"


def test_final_verify_does_not_create_second_repair_task_after_verified_repair_task(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
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
                {"id": "T002", "title": "Auth", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "abc123"},
                {"id": "T003", "title": "Final Verification Repair Bundle (T002)", "status": "verified", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": ["frontend/e2e/auth.spec.ts"], "output_paths": ["frontend/src/auth/"], "review_status": "pass", "git_commit": "def456"},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    save_task_runtime_state(
        tmp_path,
        "T-FINAL",
        {"task_id": "T-FINAL", "repair_candidates": ["T002"], "repair_task_id": "T003", "final_verify_status": "repair_required"},
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["frontend/e2e/auth.spec.ts"],
                test_types=["browser"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    tasks = all_tasks(tmp_path)
    repair_tasks = [task for task in tasks if task.task_kind == "repair"]
    assert result["status"] == "blocked"
    assert result["repair_task_id"] == "T003"
    assert len(repair_tasks) == 1
    assert repair_tasks[0].id == "T003"




def test_recover_resets_stale_pending_metadata_and_retires_orphan_session(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T003",
                    "title": "Feature",
                    "status": "pending",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature/"],
                    "status_session_id": "session-9",
                    "started_at": "2026-06-24T00:01:00Z",
                    "git_commit": "deadbeef",
                    "attempts": 1,
                }
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "session-9",
                "runtime": "claude",
                "task_count": 2,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
            },
            "retired": [],
        },
    )

    updated = recover(tmp_path)

    by_id = {task.id: task for task in updated}
    assert by_id["T003"].status == "pending"
    assert by_id["T003"].status_session_id is None
    assert by_id["T003"].started_at is None
    assert by_id["T003"].git_commit is None
    assert by_id["T003"].attempts == 0
    payload = load_session_state(tmp_path)
    assert payload["active"] is None
    assert payload["retired"][-1]["id"] == "session-9"


def test_recover_preserves_reusable_opencode_session_while_work_remains(tmp_path: Path) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T003",
                    "title": "Feature",
                    "status": "exception",
                    "requirements": ["REQ-001"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T000"],
                    "output_tests": ["backend/tests/test_feature.py"],
                    "output_paths": ["backend/src/feature/"],
                    "attempts": 3,
                },
                {
                    "id": "T004",
                    "title": "Dependent",
                    "status": "pending",
                    "requirements": ["REQ-002"],
                    "acceptance_scenarios": [],
                    "dependencies": ["T003"],
                    "output_tests": ["backend/tests/test_dependent.py"],
                    "output_paths": ["backend/src/dependent/"],
                },
            ],
        },
    )
    save_session_state(
        tmp_path,
        {
            "active": {
                "id": "ses-op-keep",
                "runtime": "opencode",
                "task_count": 4,
                "created_at": "2026-06-24T00:00:00Z",
                "status": "active",
                "title": "Feature",
                "last_heartbeat": "2026-06-24T00:05:00Z",
                "current_task_id": None,
            },
            "retired": [],
        },
    )

    recover(tmp_path)

    payload = load_session_state(tmp_path)
    assert payload["active"] is None
    assert payload["retired"][-1]["id"] == "ses-op-keep"
    assert payload["retired"][-1]["runtime"] == "opencode"
    assert payload["retired"][-1]["current_task_id"] is None


def test_final_verify_writes_repair_report_and_candidates(tmp_path: Path, monkeypatch) -> None:
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "NFR-001", "title": "Frontend", "summary": "Frontend checks"},
                ],
                "acceptance_scenarios": [],
            }
        ),
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["backend/tests/test_feature.py"],
                test_types=["api"],
                requirement_ids=["REQ-001"],
                passed=False,
                passed_count=0,
                failed_count=1,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    assert result["status"] == "repair_required"
    assert result["repair_candidates"] == ["T002"]
    assert result["repair_report"] == "docs/reviews/final-repair-report.md"
    repair_report = (tmp_path / "docs" / "reviews" / "final-repair-report.md").read_text(encoding="utf-8")
    assert "repair_candidates: T002" in repair_report
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["repair_candidates"] == ["T002"]
    assert runtime_state["final_verify_status"] == "repair_required"


def test_final_verify_blocks_when_validation_gate_is_not_verified(tmp_path: Path, monkeypatch) -> None:
    from delivery.state import save_gates
    from delivery.verify import TestResult

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    {"id": "REQ-001", "title": "One", "summary": "One"},
                    {"id": "NFR-001", "title": "Frontend", "summary": "Frontend checks"},
                ],
                "acceptance_scenarios": [],
            }
        ),
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
                {"id": "T002", "title": "Feature", "status": "verified", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"], "review_status": "pass"},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001"], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
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
                    "required_test_types": ["browser"],
                    "source": "task-decompose",
                }
            ],
        },
    )
    loop = DeliveryLoop(tmp_path)

    monkeypatch.setattr(
        "delivery.loop.run_full_suite",
        lambda project_root, mode="all": [
            TestResult(
                task_id="T002",
                timestamp="2026-06-24T00:00:00Z",
                test_files=["backend/tests/test_feature.py"],
                test_types=["api"],
                requirement_ids=["REQ-001"],
                passed=True,
                passed_count=1,
                failed_count=0,
                failures=[],
                attempt=1,
            )
        ],
    )
    monkeypatch.setattr("delivery.loop.check_test_type_coverage", lambda project_root: [])

    result = loop.final_verify()

    assert result["status"] == "repair_required"
    assert result["repair_candidates"] == ["T002"]
    assert result["blocked_gates"][0]["id"] == "GATE-auth"
    runtime_state = load_task_runtime_state(tmp_path, "T-FINAL")
    assert runtime_state["final_verify_gate_statuses"][0]["id"] == "GATE-auth"
    assert runtime_state["final_verify_status"] == "repair_required"


def test_final_verify_defers_when_non_final_feature_tasks_are_incomplete(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "requirements.json").write_text(
        json.dumps({"requirements": [{"id": "REQ-001", "title": "One", "summary": "One"}], "acceptance_scenarios": []}),
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
                {"id": "T002", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T002"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )

    loop = DeliveryLoop(tmp_path)
    result = loop.final_verify()

    assert result["status"] == "deferred"
    assert result["deferred_task_ids"] == ["T002"]


def test_shared_foundation_enters_exception_when_shared_env_cannot_warm(tmp_path: Path, monkeypatch) -> None:
    from delivery.task import all_tasks

    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "共享基础设施", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T000"], "output_tests": ["backend/src/tests/test_health.py"], "output_paths": ["backend/src/core/"]},
            ],
        },
    )

    monkeypatch.setattr(
        "delivery.loop.warm_shared_test_environment",
        lambda project_root, reason="": type("Result", (), {"ready": False, "summary": "shared env unavailable"})(),
    )

    loop = DeliveryLoop(tmp_path)
    task = next(task for task in all_tasks(tmp_path) if task.id == "T001")
    success, state = loop._execute_task(task)

    assert success is False
    assert state == "exception"


def test_run_blocks_active_repair_task_until_non_repair_feature_tasks_finish(tmp_path: Path, monkeypatch) -> None:
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T007", "title": "Feature", "status": "active", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T001"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T010", "title": "Final Verification Repair Bundle (T007)", "status": "active", "task_kind": "repair", "requirements": ["REQ-001"], "acceptance_scenarios": [], "dependencies": ["T007"], "output_tests": ["backend/tests/test_feature.py"], "output_paths": ["backend/src/feature.py"]},
                {"id": "T-FINAL", "title": "最终验证", "status": "blocked", "requirements": [], "acceptance_scenarios": [], "dependencies": ["T007", "T010"], "output_tests": [], "output_paths": ["docs/release-evidence.md", "docs/reviews/final-review.md"]},
            ],
        },
    )

    loop = DeliveryLoop(tmp_path)
    monkeypatch.setattr(loop, "_next_active_task_to_resume", lambda tasks: next(task for task in tasks if task.id == "T010"))
    monkeypatch.setattr(loop, "_execute_task", lambda task: (_ for _ in ()).throw(AssertionError("repair task should not execute")))

    result = loop.run()

    assert result["status"] == "blocked"
    assert result["task_id"] == "T010"
    assert result["blocked_task_ids"] == ["T007"]