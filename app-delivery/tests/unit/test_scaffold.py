from __future__ import annotations

from pathlib import Path

from delivery.scaffold import scaffold_project, sync_runtime_support


def test_scaffold_project_preserves_existing_files(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    (template_root / "backend" / "src").mkdir(parents=True)
    (template_root / "backend" / "src" / "main.py").write_text("print('template')\n", encoding="utf-8")
    (template_root / ".gitignore").write_text(".venv\n", encoding="utf-8")
    project_root = tmp_path / "project"
    (project_root / "backend" / "src").mkdir(parents=True)
    (project_root / "backend" / "src" / "main.py").write_text("print('existing')\n", encoding="utf-8")

    scaffold_project(project_root, template_root=template_root, mock_server_root=tmp_path / "missing-mock")

    assert (project_root / "backend" / "src" / "main.py").read_text(encoding="utf-8") == "print('existing')\n"
    assert (project_root / ".gitignore").read_text(encoding="utf-8") == ".venv\n"
    assert (project_root / "docs" / "project-structure.md").exists()


def test_scaffold_project_copies_backend_env_from_example(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    (template_root / "backend").mkdir(parents=True)
    (template_root / "backend" / ".env.example").write_text("DATABASE_URL=postgresql://demo\n", encoding="utf-8")
    project_root = tmp_path / "project"

    scaffold_project(project_root, template_root=template_root, mock_server_root=tmp_path / "missing-mock")

    assert (project_root / "backend" / ".env").read_text(encoding="utf-8") == "DATABASE_URL=postgresql://demo\n"


def test_scaffold_project_keeps_existing_backend_env(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    (template_root / "backend").mkdir(parents=True)
    (template_root / "backend" / ".env.example").write_text("DATABASE_URL=postgresql://demo\n", encoding="utf-8")
    project_root = tmp_path / "project"
    (project_root / "backend").mkdir(parents=True)
    (project_root / "backend" / ".env").write_text("DATABASE_URL=postgresql://custom\n", encoding="utf-8")

    scaffold_project(project_root, template_root=template_root, mock_server_root=tmp_path / "missing-mock")

    assert (project_root / "backend" / ".env").read_text(encoding="utf-8") == "DATABASE_URL=postgresql://custom\n"


def test_scaffold_skips_command_style_output_tests(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    project_root = tmp_path / "project"
    template_root.mkdir(parents=True)
    (project_root / "docs").mkdir(parents=True, exist_ok=True)
    (project_root / "docs" / "work-items.json").write_text(
        '{"schema_version":"2","project":"demo","generated_at":"2026-06-24T00:00:00Z","last_updated_commit":"","items":[{"id":"T001","title":"Test","status":"pending","requirements":[],"acceptance_scenarios":[],"dependencies":[],"output_tests":["curl -sf http://localhost:8000/health","tests/unit/test_demo.py"],"output_paths":[]}]}',
        encoding="utf-8",
    )

    scaffold_project(project_root, template_root=template_root, mock_server_root=tmp_path / "missing-mock")

    assert not (project_root / "curl -sf http://localhost:8000/health").exists()
    assert (project_root / "tests" / "unit" / "test_demo.py").exists()


def test_sync_runtime_support_refreshes_existing_opencode_files(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    (template_root / ".opencode").mkdir(parents=True)
    (template_root / ".opencode" / "opc-guard.js").write_text("new-guard\n", encoding="utf-8")
    project_root = tmp_path / "project"
    (project_root / ".opencode").mkdir(parents=True)
    (project_root / ".opencode" / "opc-guard.js").write_text("old-guard\n", encoding="utf-8")
    (project_root / "docs").mkdir(parents=True)
    (project_root / "docs" / "project-bootstrap.json").write_text('{"runtime":"opencode"}\n', encoding="utf-8")

    sync_runtime_support(project_root, template_root=template_root, runtime="opencode")

    assert (project_root / ".opencode" / "opc-guard.js").read_text(encoding="utf-8") == "new-guard\n"


def test_scaffold_project_structure_omits_generated_artifacts(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    project_root = tmp_path / "project"
    (template_root / "backend" / "src").mkdir(parents=True)
    (template_root / "backend" / "src" / "main.py").write_text("print('template')\n", encoding="utf-8")
    (project_root / "node_modules" / "pkg" / "dist").mkdir(parents=True, exist_ok=True)
    (project_root / "node_modules" / "pkg" / "dist" / "index.js").write_text("console.log('x')\n", encoding="utf-8")
    (project_root / "frontend" / "test-results").mkdir(parents=True, exist_ok=True)
    (project_root / "frontend" / "test-results" / "report.json").write_text("{}\n", encoding="utf-8")

    scaffold_project(project_root, template_root=template_root, mock_server_root=tmp_path / "missing-mock")

    structure = (project_root / "docs" / "project-structure.md").read_text(encoding="utf-8")
    assert "node_modules/" not in structure
    assert "test-results/" not in structure
    assert "backend/" in structure


def test_scaffold_project_creates_directory_skeleton_from_module_architecture(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    project_root = tmp_path / "project"
    (template_root / "backend").mkdir(parents=True)
    (project_root / "docs").mkdir(parents=True, exist_ok=True)
    (project_root / "docs" / "architecture.md").write_text(
        "# Architecture\n\n## 3. Module Architecture\n\n```text\notif-control-tower/\n├── backend/\n│   ├── src/\n│   │   ├── agents/\n│   │   └── shared/\n├── frontend/\n│   └── src/\n│       └── features/\n└── docs/\n    └── modules/\n```\n",
        encoding="utf-8",
    )

    scaffold_project(project_root, template_root=template_root, mock_server_root=tmp_path / "missing-mock")

    assert (project_root / "backend" / "src" / "agents").is_dir()
    assert (project_root / "backend" / "src" / "shared").is_dir()
    assert (project_root / "frontend" / "src" / "features").is_dir()
    assert (project_root / "docs" / "modules").is_dir()