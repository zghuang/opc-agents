from __future__ import annotations

import json
import shutil
from pathlib import Path

from delivery.bootstrap import archive_requirements_source, doctor_report, initialize_project, normalize_dependency_hints, save_project_dependency_hints, start_preflight


def test_initialize_project_creates_scaffold_and_metadata(tmp_path: Path) -> None:
    framework_root = tmp_path / "framework"
    template_root = framework_root / "project_temp" / "stacks" / "python-react"
    (template_root / "backend").mkdir(parents=True)
    (template_root / "frontend" / "src").mkdir(parents=True)
    (template_root / "backend" / ".env.example").write_text("DATABASE_URL=postgresql://demo\n", encoding="utf-8")
    (template_root / "frontend" / "src" / "main.tsx").write_text("console.log('frontend')\n", encoding="utf-8")
    (template_root / ".gitignore").write_text(".venv\n", encoding="utf-8")
    (framework_root / "delivery").mkdir(parents=True)
    (framework_root / "skills").mkdir(parents=True)
    (framework_root / "scripts").mkdir(parents=True)
    (framework_root / "mock-server").mkdir(parents=True)
    (framework_root / "mock-server" / "README.md").write_text("mock\n", encoding="utf-8")
    (template_root / ".claude" / "rules").mkdir(parents=True)
    (template_root / ".claude" / "rules" / "README.md").write_text("claude\n", encoding="utf-8")
    (template_root / ".opencode").mkdir(parents=True)
    (template_root / ".opencode" / "opencode.json").write_text("{}\n", encoding="utf-8")

    payload = initialize_project(
        tmp_path / "demo-project",
        description="Demo project",
        runtime="claude",
        framework_root=framework_root,
    )

    project_root = Path(payload["project_root"])
    assert payload["status"] == "ok"
    assert (project_root / ".git").exists()
    assert (project_root / "backend").exists()
    assert not (project_root / "backend" / "src").exists()
    assert (project_root / "backend" / ".env").exists()
    assert (project_root / "frontend").exists()
    assert (project_root / "mock-server").exists()
    bootstrap_meta = json.loads((project_root / "docs" / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert bootstrap_meta["runtime"] == "claude"
    assert bootstrap_meta["mock_server"] == "embedded"
    assert bootstrap_meta["watchdog_enabled"] is True
    assert bootstrap_meta["host_fallback_enabled"] is True
    assert bootstrap_meta["host_fallback_after_seconds"] == 600
    assert bootstrap_meta["host_fallback_max_attempts"] == 1


def test_python_react_template_does_not_precreate_backend_package_layout() -> None:
    template_root = Path(__file__).resolve().parents[2] / "project_temp" / "stacks" / "python-react"

    assert (template_root / "backend" / "pyproject.toml").exists()
    assert not (template_root / "backend" / "src").exists()
    assert "src.main" not in (template_root / "backend" / "Dockerfile").read_text(encoding="utf-8")


def test_initialize_project_uses_opencode_scaffold_when_requested(tmp_path: Path) -> None:
    framework_root = tmp_path / "framework"
    template_root = framework_root / "project_temp" / "stacks" / "python-react"
    (template_root / "backend").mkdir(parents=True)
    (template_root / "frontend" / "src").mkdir(parents=True)
    (template_root / "backend" / ".env.example").write_text("DATABASE_URL=postgresql://demo\n", encoding="utf-8")
    (template_root / "frontend" / "src" / "main.tsx").write_text("console.log('frontend')\n", encoding="utf-8")
    (template_root / ".gitignore").write_text(".venv\n", encoding="utf-8")
    (template_root / ".claude" / "rules").mkdir(parents=True)
    (template_root / ".claude" / "rules" / "README.md").write_text("claude\n", encoding="utf-8")
    (template_root / ".opencode").mkdir(parents=True)
    (template_root / ".opencode" / "opencode.json").write_text("{}\n", encoding="utf-8")
    (framework_root / "delivery").mkdir(parents=True)
    (framework_root / "skills").mkdir(parents=True)
    (framework_root / "scripts").mkdir(parents=True)
    (framework_root / "mock-server").mkdir(parents=True)
    (framework_root / "mock-server" / "README.md").write_text("mock\n", encoding="utf-8")

    payload = initialize_project(
        tmp_path / "demo-project-opencode",
        description="Demo project",
        runtime="opencode",
        framework_root=framework_root,
    )

    project_root = Path(payload["project_root"])
    assert (project_root / ".opencode" / "opencode.json").exists()
    assert not (project_root / ".claude").exists()
    bootstrap_meta = json.loads((project_root / "docs" / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert bootstrap_meta["watchdog_enabled"] is True
    assert bootstrap_meta["host_fallback_enabled"] is True


def test_initialize_project_can_enable_watchdog_explicitly(tmp_path: Path) -> None:
    framework_root = tmp_path / "framework"
    template_root = framework_root / "project_temp" / "stacks" / "python-react"
    (template_root / "backend").mkdir(parents=True)
    (template_root / "frontend" / "src").mkdir(parents=True)
    (template_root / "backend" / ".env.example").write_text("DATABASE_URL=postgresql://demo\n", encoding="utf-8")
    (template_root / "frontend" / "src" / "main.tsx").write_text("console.log('frontend')\n", encoding="utf-8")
    (template_root / ".gitignore").write_text(".venv\n", encoding="utf-8")
    (framework_root / "delivery").mkdir(parents=True)
    (framework_root / "skills").mkdir(parents=True)
    (framework_root / "scripts").mkdir(parents=True)
    (framework_root / "mock-server").mkdir(parents=True)
    (framework_root / "mock-server" / "README.md").write_text("mock\n", encoding="utf-8")

    payload = initialize_project(
        tmp_path / "demo-project-watchdog",
        description="Demo project",
        runtime="claude",
        framework_root=framework_root,
        watchdog_enabled=True,
    )

    project_root = Path(payload["project_root"])
    bootstrap_meta = json.loads((project_root / "docs" / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert bootstrap_meta["watchdog_enabled"] is True


def test_initialize_project_force_retries_directory_not_empty(tmp_path: Path, monkeypatch) -> None:
    framework_root = tmp_path / "framework"
    template_root = framework_root / "project_temp" / "stacks" / "python-react"
    (template_root / "backend").mkdir(parents=True)
    (template_root / "frontend" / "src").mkdir(parents=True)
    (template_root / "backend" / ".env.example").write_text("DATABASE_URL=postgresql://demo\n", encoding="utf-8")
    (template_root / "frontend" / "src" / "main.tsx").write_text("console.log('frontend')\n", encoding="utf-8")
    (framework_root / "delivery").mkdir(parents=True)
    (framework_root / "skills").mkdir(parents=True)
    (framework_root / "scripts").mkdir(parents=True)
    (framework_root / "mock-server").mkdir(parents=True)
    (framework_root / "mock-server" / "README.md").write_text("mock\n", encoding="utf-8")
    project_root = tmp_path / "demo-project-force"
    (project_root / ".app-delivery-runtime").mkdir(parents=True)
    (project_root / ".app-delivery-runtime" / "watchdog-state.json").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
    (project_root / "README.md").write_text("old\n", encoding="utf-8")
    original_rmtree = shutil.rmtree
    calls = 0

    def flaky_rmtree(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(66, "Directory not empty", str(path))
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("delivery.bootstrap.shutil.rmtree", flaky_rmtree)

    payload = initialize_project(
        project_root,
        description="Demo project",
        runtime="opencode",
        framework_root=framework_root,
        force=True,
    )

    assert payload["status"] == "ok"
    assert calls == 2
    assert not (project_root / "README.md").read_text(encoding="utf-8").startswith("old")


def test_save_project_dependency_hints_normalizes_structured_skill_output(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps({"runtime": "claude", "dependency_hints": []}), encoding="utf-8")

    hints = save_project_dependency_hints(
        tmp_path,
        [
            {"name": "LangGraph", "ecosystem": "backend", "reason": "Explicit orchestration framework", "evidence": "Agent 编排框架：LangGraph"},
            {"name": "LangGraph", "ecosystem": "backend", "reason": "duplicate"},
            {"name": "assistant-ui", "ecosystem": "frontend", "reason": "Explicit AI interaction layer"},
        ],
    )

    assert {row["name"] for row in hints} == {"LangGraph", "assistant-ui"}
    persisted = json.loads((docs_dir / "project-bootstrap.json").read_text(encoding="utf-8"))
    assert {row["name"] for row in persisted["dependency_hints"]} == {"LangGraph", "assistant-ui"}


def test_start_preflight_requires_initialized_project(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("demo\n", encoding="utf-8")

    payload = start_preflight(tmp_path / "missing-project", requirements, runtime="claude")

    assert payload["status"] == "fail"
    failed_names = {row["name"] for row in payload["checks"] if row["status"] == "fail"}
    assert {"project_exists", "git_repo", "backend_dir", "frontend_dir", "mock_server_dir"}.issubset(failed_names)


def test_start_preflight_archives_requirements_source_when_project_is_valid(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    (project_root / "backend").mkdir(parents=True)
    (project_root / "frontend").mkdir(parents=True)
    (project_root / "mock-server").mkdir(parents=True)
    (project_root / ".opencode").mkdir(parents=True)
    (project_root / ".opencode" / "opc-guard.js").write_text("old-guard\n", encoding="utf-8")
    requirements = tmp_path / "requirements.md"
    requirements.write_text("# Raw requirements\n", encoding="utf-8")

    calls: list[tuple[str, str | None]] = []

    def fake_sync(project_root_arg, *, template_root=None, runtime=None):
        calls.append((str(project_root_arg), runtime))
        (project_root / ".opencode" / "opc-guard.js").write_text("new-guard\n", encoding="utf-8")

    monkeypatch.setattr("delivery.bootstrap.sync_runtime_support", fake_sync)

    payload = start_preflight(project_root, requirements, runtime="claude")

    assert payload["status"] == "ok"
    archived = project_root / "docs" / "requirements-source.md"
    assert archived.exists()
    assert archived.read_text(encoding="utf-8") == "# Raw requirements\n"
    assert calls == [(str(project_root), "claude")]
    assert (project_root / ".opencode" / "opc-guard.js").read_text(encoding="utf-8") == "new-guard\n"


def test_archive_requirements_source_noops_when_source_is_already_archived(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    archived = docs_dir / "requirements-source.md"
    archived.write_text("# Raw requirements\n", encoding="utf-8")

    target = archive_requirements_source(project_root, archived)

    assert target == archived
    assert archived.read_text(encoding="utf-8") == "# Raw requirements\n"


def test_doctor_report_detects_missing_framework_paths(tmp_path: Path) -> None:
    framework_root = tmp_path / "framework"
    framework_root.mkdir(parents=True)
    payload = doctor_report("claude", framework_root=framework_root)
    assert payload["status"] == "fail"
    missing = {row["name"] for row in payload["checks"] if row["status"] != "ok"}
    assert "framework:delivery" in missing