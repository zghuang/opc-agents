from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .runtime_config import load_project_runtime, root_context_filename
from .state import ensure_runtime_dirs, project_paths, save_work_items, utc_now_iso
from .verify import is_placeholder_test_file


IGNORED_STRUCTURE_DIR_NAMES = {
    ".git",
    ".app-delivery-runtime",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
    "coverage",
    "coverage-v8",
    "v8-coverage",
    "htmlcov",
    "playwright-report",
    "test-results",
}
IGNORED_STRUCTURE_FILE_SUFFIXES = {".pyc", ".pyo", ".log"}
MAX_PROJECT_STRUCTURE_DEPTH = 4
MAX_PROJECT_STRUCTURE_CHILDREN = 40
MODULE_ARCHITECTURE_HEADING_RE = re.compile(r"^##+\s+(?:\d+\.\s*)?(?:Module Architecture|模块架构)\s*$", re.IGNORECASE | re.MULTILINE)
FENCED_BLOCK_RE = re.compile(r"```(?:text|plaintext|txt)?\n(?P<body>.*?)\n```", re.DOTALL | re.IGNORECASE)
TREE_LINE_RE = re.compile(r"^(?P<prefix>(?:│   |    )*)(?:(?:├── |└── ))?(?P<content>.+?)\s*$")


def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        target = dst / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _copy_file(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_backend_env_from_example(project_root: Path) -> None:
    example = project_root / "backend" / ".env.example"
    target = project_root / "backend" / ".env"
    if not example.exists() or target.exists():
        return
    _copy_file(example, target)


def _sync_runtime_support_files(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(src_dir.rglob("*")):
        relative = path.relative_to(src_dir)
        target = dst_dir / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def sync_runtime_support(
    project_root: Path | str,
    *,
    template_root: Path | None = None,
    runtime: str | None = None,
) -> None:
    project_dir = Path(project_root).expanduser().resolve()
    if not project_dir.exists():
        return
    runtime_name = (str(runtime or "").strip().lower() or load_project_runtime(project_dir) or "claude")
    if runtime_name != "opencode":
        return
    root = template_root or (Path(__file__).resolve().parents[1] / "project_temp" / "stacks" / "python-react")
    _sync_runtime_support_files(root / ".opencode", project_dir / ".opencode")


def _render_text(text: str, project_name: str) -> str:
    return text.replace("{{PROJECT_NAME}}", project_name)


def _render_tree(root: Path, project_name: str) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rendered = _render_text(text, project_name)
        if rendered != text:
            path.write_text(rendered, encoding="utf-8")


def _extract_module_architecture_tree(architecture_text: str) -> str:
    match = MODULE_ARCHITECTURE_HEADING_RE.search(str(architecture_text or ""))
    if not match:
        return ""
    section = str(architecture_text or "")[match.end() :]
    fenced = FENCED_BLOCK_RE.search(section)
    if not fenced:
        return ""
    return str(fenced.group("body") or "").strip()


def _create_scaffold_from_module_architecture(project_root: Path) -> None:
    architecture_path = project_root / "docs" / "architecture.md"
    if not architecture_path.exists():
        return
    tree_body = _extract_module_architecture_tree(architecture_path.read_text(encoding="utf-8"))
    if not tree_body:
        return

    stack: list[str] = []
    saw_tree_root = False
    for raw_line in tree_body.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        match = TREE_LINE_RE.match(line)
        if not match:
            continue
        prefix = str(match.group("prefix") or "")
        content = re.split(r"\s+#", str(match.group("content") or ""), maxsplit=1)[0].rstrip()
        if not content:
            continue
        depth = len(prefix) // 4
        if content.endswith("/") and depth == 0 and not saw_tree_root:
            saw_tree_root = True
            stack = []
            continue
        while len(stack) > depth:
            stack.pop()
        is_dir = content.endswith("/")
        name = content.rstrip("/")
        if not name:
            continue
        relative = Path(*stack, name)
        target = project_root / relative
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            stack = [*stack[:depth], name]
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.name in {"__init__.py", ".gitkeep"} and not target.exists():
            target.write_text("", encoding="utf-8")


def write_project_structure_snapshot(project_root: Path) -> None:
    lines = [
        "# Project Structure",
        "",
        "> Generated scaffold snapshot. Runtime caches, package installs, and build/test artifacts are omitted.",
        f"> Tree depth is capped at {MAX_PROJECT_STRUCTURE_DEPTH} levels and large directories are summarized.",
        "",
    ]

    def include_path(path: Path) -> bool:
        if any(part in IGNORED_STRUCTURE_DIR_NAMES for part in path.parts):
            return False
        if path.suffix.lower() in IGNORED_STRUCTURE_FILE_SUFFIXES:
            return False
        return True

    def render_dir(directory: Path, depth: int) -> None:
        children = [child for child in sorted(directory.iterdir(), key=lambda row: (not row.is_dir(), row.name.lower())) if include_path(child)]
        limited_children = children[:MAX_PROJECT_STRUCTURE_CHILDREN]
        for child in limited_children:
            try:
                rel = child.relative_to(project_root)
            except ValueError:
                continue
            indent = "  " * (len(rel.parts) - 1)
            name = child.name + ("/" if child.is_dir() else "")
            lines.append(f"{indent}- {name}")
            if child.is_dir() and depth < MAX_PROJECT_STRUCTURE_DEPTH:
                render_dir(child, depth + 1)
        omitted = len(children) - len(limited_children)
        if omitted > 0:
            indent = "  " * max(depth, 0)
            lines.append(f"{indent}- ... ({omitted} more entries omitted)")

    render_dir(project_root, 0)
    (project_root / "docs" / "project-structure.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ensure_output_test_files(project_root: Path, payload: dict[str, Any]) -> None:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        for test_path in item.get("output_tests", []) or []:
            if not is_placeholder_test_file(str(test_path)):
                continue
            relative = Path(str(test_path))
            target = project_root / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_placeholder_test_content(relative), encoding="utf-8")


def _placeholder_test_content(relative: Path) -> str:
    normalized = Path(str(relative))
    suffix = normalized.suffix.lower()
    parts = {part.lower() for part in normalized.parts}
    if suffix in {".ts", ".tsx", ".js", ".jsx"} and "frontend" in parts and "e2e" in parts:
        return (
            "import { test } from '@playwright/test'\n\n"
            "test('placeholder', async () => {\n"
            "  throw new Error('placeholder test not implemented yet')\n"
            "})\n"
        )
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return (
            "import { describe, it } from 'vitest'\n\n"
            "describe('placeholder', () => {\n"
            "  it('not implemented', () => {\n"
            "    throw new Error('placeholder test not implemented yet')\n"
            "  })\n"
            "})\n"
        )
    return "def test_placeholder():\n    assert False, 'placeholder test not implemented yet'\n"


def scaffold_project(project_root: Path | str, *, template_root: Path | None = None, mock_server_root: Path | None = None, runtime: str | None = None) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    project_name = project_dir.name
    ensure_runtime_dirs(project_dir)
    root = template_root or (Path(__file__).resolve().parents[1] / "project_temp" / "stacks" / "python-react")
    mock_root = mock_server_root or (Path(__file__).resolve().parents[1] / "mock-server")
    project_dir.mkdir(parents=True, exist_ok=True)

    runtime_name = (str(runtime or "").strip().lower() or load_project_runtime(project_dir) or "claude")

    for name in ["backend", "frontend", ".githooks", ".github"]:
        src = root / name
        if src.exists():
            _copy_tree(src, project_dir / name)
    if runtime_name == "claude":
        src = root / ".claude"
        if src.exists():
            _copy_tree(src, project_dir / ".claude")
        if (project_dir / ".opencode").exists():
            shutil.rmtree(project_dir / ".opencode")
    if runtime_name == "opencode":
        src = root / ".opencode"
        if src.exists():
            _copy_tree(src, project_dir / ".opencode")
            sync_runtime_support(project_dir, template_root=root, runtime=runtime_name)
        if (project_dir / ".claude").exists():
            shutil.rmtree(project_dir / ".claude")
    for name in ["docker-compose.yml", ".gitignore", ".worktreeinclude", ".env"]:
        src = root / name
        if src.exists():
            _copy_file(src, project_dir / name)

    if mock_root.exists():
        _copy_tree(mock_root, project_dir / "mock-server")

    (project_dir / "docs").mkdir(parents=True, exist_ok=True)
    _render_tree(project_dir, project_name)
    _copy_backend_env_from_example(project_dir)
    _create_scaffold_from_module_architecture(project_dir)
    work_items_path = project_dir / "docs" / "work-items.json"
    if work_items_path.exists():
        payload = json.loads(work_items_path.read_text(encoding="utf-8"))
        _ensure_output_test_files(project_dir, payload)
    write_project_structure_snapshot(project_dir)
    return project_dir


def scaffold_task_prompt(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "").strip() or "Scaffold"
    return f"Scaffold task: {title}"
