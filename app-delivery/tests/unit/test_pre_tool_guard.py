from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "app_delivery_pre_tool_guard.py"


def _run_guard(project_root: Path, command: str, *, workdir: str | None = None) -> dict[str, object]:
    tool_input: dict[str, object] = {"command": command}
    if workdir is not None:
        tool_input["workdir"] = workdir
    payload = {
        "tool_name": "bash",
        "cwd": str(project_root),
        "tool_input": tool_input,
    }
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = completed.stdout.strip()
    return json.loads(output) if output else {}


def _run_guard_tool(project_root: Path, tool_name: str, tool_input: dict[str, object]) -> dict[str, object]:
    payload = {
        "tool_name": tool_name,
        "cwd": str(project_root),
        "tool_input": tool_input,
    }
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = completed.stdout.strip()
    return json.loads(output) if output else {}


def _seed_project(project_root: Path, *, task_id: str = "T003", output_paths: list[str] | None = None) -> None:
    (project_root / "docs").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime").mkdir(parents=True, exist_ok=True)
    (project_root / ".app-delivery-runtime" / "active-tasks").mkdir(parents=True, exist_ok=True)
    (project_root / "docs" / "work-items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": task_id,
                        "title": "Task",
                        "output_paths": output_paths
                        or [
                            "frontend/src/routes/login.tsx",
                            "backend/src/auth/routes.py",
                        ],
                        "output_tests": ["backend/src/tests/auth/test_login.py"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (project_root / ".app-delivery-runtime" / "session-state.json").write_text(
        json.dumps({"active": {"current_task_id": task_id, "runtime": "opencode", "id": "ses-1"}, "retired": []}),
        encoding="utf-8",
    )


def test_guard_allows_frontend_lint_command(tmp_path: Path) -> None:
    _seed_project(tmp_path)

    result = _run_guard(tmp_path, "cd frontend && npm run lint")

    assert result == {}


def test_guard_allows_frontend_npm_install_when_lockfile_is_in_scope(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/package-lock.json"])

    result = _run_guard(tmp_path, "cd frontend && npm install")

    assert result == {}


def test_guard_allows_backend_uv_lock_when_lockfile_is_in_scope(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["backend/pyproject.toml", "backend/uv.lock"])

    result = _run_guard(tmp_path, "uv lock", workdir=str(tmp_path / "backend"))

    assert result == {}


def test_guard_allows_backend_uv_lock_without_explicit_workdir_when_lockfile_is_in_scope(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["backend/pyproject.toml", "backend/uv.lock"])

    result = _run_guard(tmp_path, "uv lock")

    assert result == {}


def test_guard_allows_frontend_local_tsc_when_lockfile_is_in_scope(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/package-lock.json"])

    result = _run_guard(tmp_path, "node_modules/.bin/tsc --noEmit 2>&1", workdir=str(tmp_path / "frontend"))

    assert result == {}


def test_guard_allows_frontend_npm_run_list_probe(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/package-lock.json"])

    result = _run_guard(tmp_path, "npm run --list 2>&1 | head -20", workdir=str(tmp_path / "frontend"))

    assert result == {}


def test_guard_blocks_filtered_playwright_without_pipefail(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/package-lock.json"])

    result = _run_guard(tmp_path, "npx playwright test e2e/incidents/ --reporter=list 2>&1 | tail -50", workdir=str(tmp_path / "frontend"))

    assert result["decision"] == "block"
    assert "pipefail" in str(result["reason"])


def test_guard_allows_filtered_playwright_with_pipefail(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/package-lock.json"])

    result = _run_guard(tmp_path, "set -o pipefail; npx playwright test e2e/incidents/ --reporter=list 2>&1 | tail -50", workdir=str(tmp_path / "frontend"))

    assert result == {}


def test_guard_allows_node_and_npm_version_checks(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/package-lock.json"])

    result = _run_guard(tmp_path, "node --version && npm --version")

    assert result == {}


def test_guard_allows_pnpm_install_when_frontend_lockfile_is_in_scope(tmp_path: Path) -> None:
    _seed_project(tmp_path, task_id="T001", output_paths=["frontend/package.json", "frontend/pnpm-lock.yaml"])

    result = _run_guard(tmp_path, "pnpm install 2>&1", workdir=str(tmp_path / "frontend"))

    assert result == {}


def test_guard_allows_in_scope_delete_of_new_sibling_file(tmp_path: Path) -> None:
    _seed_project(tmp_path)

    result = _run_guard(tmp_path, "cd frontend && rm src/routes/index.tsx")

    assert result == {}


def test_guard_blocks_out_of_scope_delete(tmp_path: Path) -> None:
    _seed_project(tmp_path)

    result = _run_guard(tmp_path, "rm docs/requirements-source.md")

    assert result["decision"] == "block"


def test_guard_blocks_direct_framework_review_artifact_writes(tmp_path: Path) -> None:
    _seed_project(tmp_path)

    for path in [
        "docs/reviews/code-review-T008.md",
        "docs/reviews/exception-report-T008.md",
        "docs/reviews/test-report-T008.md",
        "docs/reviews/final-review.md",
        "docs/reviews/final-repair-report.md",
    ]:
        result = _run_guard_tool(tmp_path, "write", {"filePath": str(tmp_path / path), "content": "status: pass\n"})
        assert result["decision"] == "block"
        assert "framework review artifact" in str(result["reason"])


def test_guard_allows_task_owned_review_docs_without_framework_name(tmp_path: Path) -> None:
    _seed_project(tmp_path, output_paths=["docs/reviews/test-report-auth-domain.md"])

    result = _run_guard_tool(tmp_path, "write", {"filePath": str(tmp_path / "docs/reviews/test-report-auth-domain.md"), "content": "status: pass\n"})

    assert result == {}