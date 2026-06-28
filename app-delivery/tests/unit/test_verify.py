from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from delivery.state import load_active_task_records, save_work_items
from delivery import test_env as test_env_module
from delivery.verify import _command_for_test_spec
from delivery.verify import _parse_pytest_output, detect_js_package_manager, infer_test_types, is_command_test_spec, is_placeholder_test_file, run_full_suite, run_task_tests, weak_command_reason, weak_test_file_reason
from delivery.test_env import ensure_task_test_environment, warm_shared_test_environment


def test_weak_command_reason_flags_echo() -> None:
    assert weak_command_reason("echo ok") == "echo-only validation"


def test_weak_command_reason_accepts_pytest() -> None:
    assert weak_command_reason("python3 -m pytest tests/unit -q") == ""


def test_infer_test_types_from_paths() -> None:
    kinds = infer_test_types(["tests/auth/test_api_register.py", "tests/e2e/test_register_flow.py"])
    assert kinds == ["api", "e2e"]


def test_command_and_placeholder_test_spec_detection() -> None:
    assert is_command_test_spec("curl -sf http://localhost:8000/health") is True
    assert is_placeholder_test_file("packages/backend/tests/test_health.py") is True
    assert is_placeholder_test_file("curl -sf http://localhost:8000/health") is False


def test_weak_test_file_reason_flags_trivial_frontend_placeholder(tmp_path: Path) -> None:
    test_file = tmp_path / "frontend" / "src" / "auth" / "LoginPage.test.tsx"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "import { describe, expect, it } from 'vitest'\n\n"
        "describe('LoginPage', () => {\n"
        "  it('placeholder', () => {\n"
        "    expect(true).toBe(true)\n"
        "  })\n"
        "})\n",
        encoding="utf-8",
    )

    reason = weak_test_file_reason(tmp_path, "frontend/src/auth/LoginPage.test.tsx")

    assert "placeholder test file" in reason


def test_run_task_tests_supports_command_specs(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_sample.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    result = run_task_tests(
        tmp_path,
        {
            "id": "T100",
            "requirements": ["REQ-001"],
            "output_tests": ["tests/test_sample.py", "python3 -m pytest tests/test_sample.py -q"],
        },
    )
    assert result.passed is True
    assert result.passed_count >= 2
    report = tmp_path / "docs" / "reviews" / "test-report-T100.md"
    assert report.exists()
    assert "status: pass" in report.read_text(encoding="utf-8")


def test_run_task_tests_with_no_output_tests_passes(tmp_path: Path) -> None:
    result = run_task_tests(tmp_path, {"id": "T101", "requirements": ["REQ-001"], "output_tests": []})
    assert result.passed is True
    assert result.passed_count == 0
    assert result.failed_count == 0


def test_run_records_validation_activity_for_status_visibility(tmp_path: Path, monkeypatch) -> None:
    from delivery import verify as verify_module

    seen_records: list[dict[str, object]] = []

    def fake_run(*args, **kwargs):
        records = load_active_task_records(tmp_path)
        assert len(records) == 1
        seen_records.extend(records)
        return subprocess.CompletedProcess(args[0], 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(verify_module.subprocess, "run", fake_run)

    completed = verify_module._run(
        [sys.executable, "-m", "pytest", "tests/test_sample.py", "-q"],
        cwd=tmp_path,
        project_root=tmp_path,
        task_id="T200",
        task_title="Feature",
        spec="tests/test_sample.py",
    )

    assert completed.returncode == 0
    assert seen_records[0]["task_id"] == "T200"
    assert seen_records[0]["phase"] == "verification"
    assert seen_records[0]["pid"] == os.getpid()
    assert seen_records[0]["spec"] == "tests/test_sample.py"
    assert load_active_task_records(tmp_path) == []
    task_log = (tmp_path / ".app-delivery-runtime" / "task-log.jsonl").read_text(encoding="utf-8")
    assert "Started verification T200" in task_log
    assert "Completed verification T200" in task_log


def test_infer_test_types_unknown_path_falls_back_to_unit() -> None:
    assert infer_test_types(["checks/custom-validator.txt"]) == ["unit"]


def test_infer_test_types_marks_backend_suite_as_api_and_integration() -> None:
    assert infer_test_types(["backend/tests/test_auth/test_login.py"]) == ["api", "integration"]


def test_infer_test_types_marks_backend_core_and_src_tests_as_unit() -> None:
    assert infer_test_types(["backend/tests/core/test_database.py"]) == ["unit"]
    assert infer_test_types(["backend/src/tests/test_health.py"]) == ["unit"]


def test_infer_test_types_marks_backend_feature_src_tests_as_api() -> None:
    assert infer_test_types(["backend/src/tests/test_auth/test_login.py"]) == ["api"]


def test_infer_test_types_marks_frontend_e2e_specs_as_browser_and_e2e() -> None:
    assert infer_test_types(["frontend/e2e/auth.spec.ts"]) == ["browser", "e2e"]


def test_infer_test_types_marks_mock_server_suites_as_integration_and_contract() -> None:
    assert infer_test_types(
        [
            "mock-server/tests/test_routers/test_erp_orders.py",
            "mock-server/tests/mcp_servers/test_procurement_mcp.py",
            "mock-server/tests/test_connectivity.py",
        ]
    ) == ["integration", "contract"]


def test_infer_test_types_marks_mock_server_contract_named_suite_as_contract() -> None:
    assert infer_test_types(["mock-server/tests/test_all_mocks_contract.py"]) == ["contract", "integration"]


def test_parse_pytest_output_handles_nonstandard_output() -> None:
    passed, failed, failures = _parse_pytest_output("ERROR collecting tests/test_sample.py\n")
    assert passed == 0
    assert failed == 0
    assert failures == []


def test_failure_from_output_skips_npm_banner_lines() -> None:
    from delivery.verify import _failure_from_output

    failure = _failure_from_output(
        "npm run lint",
        "> lint\n> npm run lint --prefix frontend\n\n/Users/me/project/file.ts\n  10:1  error  Boom\n",
        "test command failed",
    )

    assert failure.message == "/Users/me/project/file.ts"


def test_detect_js_package_manager_prefers_lockfile(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    assert detect_js_package_manager(tmp_path, "frontend/tests/example.spec.ts") == "npm"


def test_command_for_test_spec_uses_current_python_for_pytest(tmp_path: Path) -> None:
    command = _command_for_test_spec(tmp_path, "tests/test_sample.py")
    assert command[0] == sys.executable
    assert command[1:] == ["-m", "pytest", "tests/test_sample.py", "--tb=short", "-q"]


def test_command_for_backend_test_spec_runs_in_backend_with_uv(tmp_path: Path) -> None:
    command = _command_for_test_spec(tmp_path, "backend/tests/core/test_database.py")
    assert command[0:2] == ["/bin/zsh", "-lc"]
    assert "cd " in command[2]
    assert "uv run pytest tests/core/test_database.py --tb=short -q" in command[2]


def test_command_for_bare_pytest_spec_rewrites_to_backend_uv(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    command = _command_for_test_spec(tmp_path, "python -m pytest src/tests/auth/test_login.py -q")

    assert command[0:2] == ["/bin/zsh", "-lc"]
    assert f"cd {tmp_path / 'backend'}" in command[2]
    assert "uv run python -m pytest src/tests/auth/test_login.py -q" in command[2]


def test_command_for_npm_script_runs_in_frontend(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    command = _command_for_test_spec(tmp_path, "npm run test")

    assert command[0:2] == ["/bin/zsh", "-lc"]
    assert f"cd {tmp_path / 'frontend'}" in command[2]
    assert command[2].endswith("&& npm run test")


def test_command_for_backend_test_directory_runs_in_backend_with_uv(tmp_path: Path) -> None:
    command = _command_for_test_spec(tmp_path, "backend/tests/core/")
    assert command[0:2] == ["/bin/zsh", "-lc"]
    assert "cd " in command[2]
    assert "uv run pytest tests/core --tb=short -q" in command[2]


def test_command_for_frontend_e2e_spec_runs_in_frontend(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    command = _command_for_test_spec(tmp_path, "frontend/e2e/app-shell.spec.ts")
    assert command[0:2] == ["/bin/zsh", "-lc"]
    assert "cd " in command[2]
    assert "playwright" in command[2]
    assert "e2e/app-shell.spec.ts" in command[2]


def test_run_full_suite_supports_non_verified_mode(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_sample.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    save_work_items(
        tmp_path,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {"id": "T001", "title": "Done", "status": "verified", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_sample.py"], "output_paths": []},
                {"id": "T002", "title": "Pending", "status": "pending", "requirements": [], "acceptance_scenarios": [], "dependencies": [], "output_tests": ["tests/test_sample.py"], "output_paths": []},
            ],
        },
    )
    results = run_full_suite(tmp_path, mode="non-verified")
    assert len(results) == 1
    assert results[0].task_id == "T002"


def test_run_full_suite_adds_frontend_quality_gate_commands(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                        "test": "vitest run",
                    "typecheck": "tsc --noEmit",
                    "lint": "eslint src/",
                    "build": "vite build",
                    "e2e": "playwright test",
                }
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
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001", "NFR-003", "NFR-004"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
            ],
        },
    )

    from delivery import verify as verify_module

    seen_specs: list[dict[str, object]] = []

    def fake_run_task_tests(project_root, task, *, attempt=1):
        seen_specs.append(task)
        return verify_module.TestResult(
            task_id=str(task.get("id") or ""),
            timestamp="2026-06-24T00:00:00Z",
            test_files=[str(value) for value in task.get("output_tests", [])],
            test_types=verify_module.infer_test_types([str(value) for value in task.get("output_tests", [])]),
            requirement_ids=[str(value) for value in task.get("requirements", [])],
            passed=True,
            passed_count=len(task.get("output_tests", [])),
            failed_count=0,
            failures=[],
            attempt=attempt,
        )

    original_run_task_tests = verify_module.run_task_tests
    verify_module.run_task_tests = fake_run_task_tests
    try:
        results = run_full_suite(tmp_path, mode="all")
    finally:
        verify_module.run_task_tests = original_run_task_tests

    assert any(task["id"] == "T-FINAL:frontend-quality-gates" for task in seen_specs)
    assert any(task["id"] == "T-FINAL:frontend-browser-qa" for task in seen_specs)
    quality_gate = next(task for task in seen_specs if task["id"] == "T-FINAL:frontend-quality-gates")
    assert quality_gate["output_tests"] == ["npm run test", "npm run typecheck", "npm run lint", "npm run build"]
    assert quality_gate["requirements"] == []
    browser_gate = next(task for task in seen_specs if task["id"] == "T-FINAL:frontend-browser-qa")
    assert browser_gate["output_tests"] == ["npm run e2e"]
    assert browser_gate["requirements"] == []
    assert any(result.task_id == "T-FINAL:frontend-quality-gates" for result in results)
    assert any(result.task_id == "T-FINAL:frontend-browser-qa" for result in results)
    final_report = tmp_path / "docs" / "reviews" / "test-report-T-FINAL.md"
    assert final_report.exists()
    assert "report_type: test" in final_report.read_text(encoding="utf-8")


def test_command_for_frontend_e2e_spec_auto_wires_backend_when_template_backend_exists(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir(parents=True)
    (tmp_path / "frontend" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "backend" / "src").mkdir(parents=True)
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "main.py").write_text("app = object()\n", encoding="utf-8")

    command = _command_for_test_spec(tmp_path, "frontend/e2e/app-shell.spec.ts")

    assert "E2E_BACKEND_CMD=" in command[2]
    assert "VITE_API_PROXY_TARGET=http://127.0.0.1:8000" in command[2]
    assert "uvicorn src.main:app --host 127.0.0.1 --port 8000" in command[2]


def test_command_for_npm_e2e_runs_in_frontend_with_backend_env_when_available(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir(parents=True)
    (tmp_path / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "backend" / "src").mkdir(parents=True)
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "main.py").write_text("app = object()\n", encoding="utf-8")

    command = _command_for_test_spec(tmp_path, "npm run e2e")

    assert command[0:2] == ["/bin/zsh", "-lc"]
    assert f"cd {tmp_path / 'frontend'}" in command[2]
    assert "E2E_BACKEND_CMD=" in command[2]
    assert command[2].endswith("npm run e2e")


def test_run_full_suite_frontend_quality_gates_have_no_requirement_binding(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest run",
                    "typecheck": "tsc --noEmit",
                    "lint": "eslint src/",
                    "build": "vite build",
                    "e2e": "playwright test",
                }
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
                {"id": "T-FINAL", "title": "最终验证", "status": "pending", "requirements": ["NFR-001", "NFR-003", "NFR-004"], "acceptance_scenarios": [], "dependencies": [], "output_tests": [], "output_paths": []},
            ],
        },
    )

    from delivery import verify as verify_module

    seen_specs: list[dict[str, object]] = []

    def fake_run_task_tests(project_root, task, *, attempt=1):
        seen_specs.append(task)
        return verify_module.TestResult(
            task_id=str(task.get("id") or ""),
            timestamp="2026-06-24T00:00:00Z",
            test_files=[str(value) for value in task.get("output_tests", [])],
            test_types=verify_module.infer_test_types([str(value) for value in task.get("output_tests", [])]),
            requirement_ids=[str(value) for value in task.get("requirements", [])],
            passed=True,
            passed_count=len(task.get("output_tests", [])),
            failed_count=0,
            failures=[],
            attempt=attempt,
        )

    original_run_task_tests = verify_module.run_task_tests
    verify_module.run_task_tests = fake_run_task_tests
    try:
        run_full_suite(tmp_path, mode="all")
    finally:
        verify_module.run_task_tests = original_run_task_tests

    quality_gate = next(task for task in seen_specs if task["id"] == "T-FINAL:frontend-quality-gates")
    browser_gate = next(task for task in seen_specs if task["id"] == "T-FINAL:frontend-browser-qa")
    assert quality_gate["requirements"] == []
    assert browser_gate["requirements"] == []


def test_warm_shared_test_environment_is_noop_when_no_shared_services_declared(tmp_path: Path) -> None:
    result = warm_shared_test_environment(tmp_path, reason="unit-test")

    assert result.ready is True
    assert result.profile == "shared_infra"
    assert result.actions_run == []


def test_ensure_task_test_environment_skips_extra_setup_for_non_browser_specs(tmp_path: Path) -> None:
    result = ensure_task_test_environment(
        tmp_path,
        {"id": "T002", "output_tests": ["backend/tests/test_feature.py"]},
        "backend/tests/test_feature.py",
    )

    assert result.ready is True
    assert result.profile == "none"


def test_shared_service_names_are_derived_from_compose_and_exclude_app_local_services(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("APP_DELIVERY_SHARED_SERVICES", raising=False)
    monkeypatch.setattr(
        test_env_module,
        "_compose_service_names",
        lambda project_root: ["postgres", "redis", "backend", "frontend", "mocks"],
    )

    assert test_env_module._shared_service_names(tmp_path) == ["postgres", "redis"]


def test_shared_service_override_respects_declared_compose_services(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APP_DELIVERY_SHARED_SERVICES", "redis,postgres,unknown")
    monkeypatch.setattr(
        test_env_module,
        "_compose_service_names",
        lambda project_root: ["postgres", "redis", "backend"],
    )

    assert test_env_module._shared_service_names(tmp_path) == ["postgres", "redis"]
