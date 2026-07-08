from __future__ import annotations

import json
import re
import shutil
from math import gcd
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
DATABASE_LOCALHOST_PORT_RE = re.compile(
    r"^(?P<prefix>DATABASE_URL=(?:postgresql(?:\+[A-Za-z0-9_]+)?|postgres)://[^\n]*?localhost:)5432(?P<suffix>(?:/|\?|$)[^\n]*)$",
    re.MULTILINE,
)
MAX_PROJECT_STRUCTURE_DEPTH = 4
MAX_PROJECT_STRUCTURE_CHILDREN = 40
MODULE_ARCHITECTURE_HEADING_RE = re.compile(r"^##+\s+(?:\d+\.\s*)?(?:Module Architecture|模块架构)\s*$", re.IGNORECASE | re.MULTILINE)
FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(?P<body>.*?)\n```", re.DOTALL | re.IGNORECASE)
NEXT_HEADING_RE = re.compile(r"^##+\s+", re.MULTILINE)
TREE_CONNECTOR_RE = re.compile(r"^(?P<prefix>.*?)(?:├── |└── |\|-- |\+-- |`-- |\\-- )(?P<content>.+?)\s*$")
MARKDOWN_BULLET_RE = re.compile(r"^(?:[-*+]\s+)(?P<content>.+?)\s*$")
PATH_LIKE_ENTRY_RE = re.compile(r"(?:/|\\|\.\w{1,8}$|^__init__\.py$|^\.gitkeep$)")


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


def _project_port_values(project_root: Path) -> dict[str, int]:
    seed = sum((index + 1) * ord(char) for index, char in enumerate(project_root.name)) % 1000
    return {
        "POSTGRES_HOST_PORT": 15432 + seed,
        "BACKEND_HOST_PORT": 18000 + seed,
        "FRONTEND_HOST_PORT": 15080 + seed,
        "MOCK_SERVER_HOST_PORT": 18888 + seed,
    }


def _upsert_env_values(path: Path, values: dict[str, str | int]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _configure_project_ports(project_root: Path) -> None:
    ports = _project_port_values(project_root)
    _upsert_env_values(project_root / ".env", ports)
    backend_env = project_root / "backend" / ".env"
    if backend_env.exists():
        values: dict[str, str] = {}
        text = backend_env.read_text(encoding="utf-8")
        database_url_match = DATABASE_LOCALHOST_PORT_RE.search(text)
        if database_url_match:
            values["DATABASE_URL"] = f"{database_url_match.group('prefix')}{ports['POSTGRES_HOST_PORT']}{database_url_match.group('suffix')}"
        if "EXTERNAL_API_BASE_URL=http://localhost:8888" in text:
            values["EXTERNAL_API_BASE_URL"] = f"http://localhost:{ports['MOCK_SERVER_HOST_PORT']}"
        if values:
            _upsert_env_values(backend_env, values)


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
    next_heading = NEXT_HEADING_RE.search(section)
    if next_heading:
        section = section[: next_heading.start()]
    fenced = FENCED_BLOCK_RE.search(section)
    if not fenced:
        tree_lines = [line for line in section.splitlines() if _looks_like_architecture_tree_source_line(line)]
        return "\n".join(tree_lines).strip()
    return str(fenced.group("body") or "").strip()


def _tree_prefix_depth(prefix: str) -> int:
    visible_depth = prefix.count("|") + prefix.count("│")
    return max(visible_depth, len(prefix) // 4)


def _strip_tree_entry_content(content: str) -> str:
    text = re.split(r"\s+#", str(content or ""), maxsplit=1)[0].strip()
    return text


def _looks_like_architecture_tree_source_line(raw_line: str) -> bool:
    line = str(raw_line or "").expandtabs(4).rstrip()
    if not line.strip():
        return False
    connector_match = TREE_CONNECTOR_RE.match(line)
    if connector_match:
        content = str(connector_match.group("content") or "")
    else:
        stripped = line.lstrip(" ")
        if stripped.startswith(("|", "│", "├", "└")):
            return True
        bullet_match = MARKDOWN_BULLET_RE.match(stripped)
        content = str(bullet_match.group("content") if bullet_match else stripped)
    return bool(PATH_LIKE_ENTRY_RE.search(_strip_tree_entry_content(content)))


def _detect_plain_tree_indent_unit(tree_body: str) -> int:
    unit = 0
    for raw_line in str(tree_body or "").splitlines():
        line = raw_line.expandtabs(4).rstrip()
        if not line.strip() or TREE_CONNECTOR_RE.match(line):
            continue
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith(("|", "│", "├", "└")):
            continue
        leading_spaces = len(line) - len(stripped)
        if leading_spaces <= 0:
            continue
        unit = leading_spaces if unit == 0 else gcd(unit, leading_spaces)
    return max(unit, 1) if unit else 4


def _parse_architecture_tree_line(raw_line: str, *, plain_indent_unit: int = 4) -> tuple[int, str, bool] | None:
    line = raw_line.expandtabs(4).rstrip()
    if not line.strip():
        return None
    connector_match = TREE_CONNECTOR_RE.match(line)
    if connector_match:
        depth = _tree_prefix_depth(str(connector_match.group("prefix") or ""))
        content = str(connector_match.group("content") or "")
        has_connector = True
    else:
        stripped = line.lstrip(" ")
        if stripped.startswith(("|", "│", "├", "└")):
            return None
        leading_spaces = len(line) - len(stripped)
        bullet_match = MARKDOWN_BULLET_RE.match(stripped)
        depth = leading_spaces // max(plain_indent_unit, 1)
        content = str(bullet_match.group("content") if bullet_match else stripped)
        has_connector = False
    content = _strip_tree_entry_content(content)
    if not content or content.startswith(("|", "│", "├", "└")):
        return None
    return depth, content, has_connector


def _parse_module_architecture_lines(tree_body: str) -> list[tuple[int, str, bool]]:
    plain_indent_unit = _detect_plain_tree_indent_unit(tree_body)
    parsed_lines = [
        parsed
        for raw_line in tree_body.splitlines()
        if (parsed := _parse_architecture_tree_line(raw_line, plain_indent_unit=plain_indent_unit)) is not None
    ]
    if parsed_lines and not parsed_lines[0][2] and any(has_connector for _depth, _content, has_connector in parsed_lines[1:]):
        return [(depth + 1 if has_connector else depth, content, has_connector) for depth, content, has_connector in parsed_lines]
    return parsed_lines


def _architecture_relative_paths(parsed_lines: list[tuple[int, str, bool]], *, skip_first: bool = False) -> list[Path]:
    paths: list[Path] = []
    stack: list[str] = []
    depth_offset = _skipped_root_depth_offset(parsed_lines) if skip_first else 0
    for index, parsed in enumerate(parsed_lines):
        raw_depth, content, _has_connector = parsed
        if index == 0 and skip_first:
            stack = []
            continue
        depth = max(raw_depth - depth_offset, 0)
        while len(stack) > depth:
            stack.pop()
        name = content.rstrip("/")
        if not name:
            continue
        parent = stack[: min(depth, len(stack))]
        paths.append(Path(*parent, name))
        if content.endswith("/"):
            stack = [*parent, name]
    return paths


def _skipped_root_depth_offset(parsed_lines: list[tuple[int, str, bool]]) -> int:
    remaining_depths = [depth for depth, _content, _has_connector in parsed_lines[1:]]
    return min(remaining_depths) if remaining_depths else 0


def _relative_path_roots(paths: list[Path]) -> set[str]:
    return {path.parts[0] for path in paths if path.parts}


def _existing_root_score(project_root: Path, paths: list[Path]) -> int:
    return sum(1 for root in _relative_path_roots(paths) if (project_root / root).exists())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _should_skip_first_architecture_node(project_root: Path, parsed_lines: list[tuple[int, str, bool]]) -> bool:
    if len(parsed_lines) < 2:
        return False
    first_depth, first_content, _first_has_connector = parsed_lines[0]
    if first_depth != 0 or not first_content.endswith("/"):
        return False

    keep_paths = _architecture_relative_paths(parsed_lines, skip_first=False)
    skip_paths = _architecture_relative_paths(parsed_lines, skip_first=True)
    if not skip_paths:
        return False

    first_name = first_content.rstrip("/")
    if _slug(first_name) == _slug(project_root.name):
        return True

    keep_existing = _existing_root_score(project_root, keep_paths)
    skip_existing = _existing_root_score(project_root, skip_paths)
    if skip_existing != keep_existing:
        return skip_existing > keep_existing

    keep_roots = _relative_path_roots(keep_paths)
    skip_roots = _relative_path_roots(skip_paths)
    if len(keep_roots) == 1 and len(skip_roots) > 1 and not (project_root / first_name).exists():
        return True
    return False


def _create_scaffold_from_module_architecture(project_root: Path) -> None:
    architecture_path = project_root / "docs" / "architecture.md"
    if not architecture_path.exists():
        return
    tree_body = _extract_module_architecture_tree(architecture_path.read_text(encoding="utf-8"))
    if not tree_body:
        return

    parsed_lines = _parse_module_architecture_lines(tree_body)
    if not parsed_lines:
        return

    skip_first_tree_root = _should_skip_first_architecture_node(project_root, parsed_lines)

    stack: list[str] = []
    depth_offset = _skipped_root_depth_offset(parsed_lines) if skip_first_tree_root else 0
    for index, parsed in enumerate(parsed_lines):
        raw_depth, content, _has_connector = parsed
        if index == 0 and skip_first_tree_root:
            stack = []
            continue
        depth = max(raw_depth - depth_offset, 0)
        while len(stack) > depth:
            stack.pop()
        is_dir = content.endswith("/")
        name = content.rstrip("/")
        if not name:
            continue
        parent = stack[: min(depth, len(stack))]
        relative = Path(*parent, name)
        target = project_root / relative
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            stack = [*parent, name]
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
    _configure_project_ports(project_dir)
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
