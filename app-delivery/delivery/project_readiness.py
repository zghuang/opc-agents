from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runtime_config import load_project_runtime, root_context_filename
from .state import load_architecture_meta
from .task import SCAFFOLD_TASK_ID, all_tasks, lint_task_contracts


def load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def file_has_content(path: Path) -> bool:
    return path.exists() and bool(path.read_text(encoding="utf-8").strip())


def blocking_clarifications_present(project_root: Path) -> bool:
    clarification_path = project_root / "docs" / "clarification-needed.md"
    if not clarification_path.exists():
        return False
    text = clarification_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if any(line.strip().startswith("## C1") for line in lines):
        return True
    for line in lines:
        if line.lower().startswith("blocking_count:"):
            try:
                return int(line.split(":", 1)[1].strip()) > 0 and any(
                    row.strip().startswith("## C1") for row in lines
                )
            except ValueError:
                return any(row.strip().startswith("## C1") for row in lines)
    return False


def requirements_ready(project_root: Path) -> bool:
    payload = load_json_file(project_root / "docs" / "requirements.json")
    if payload is None:
        return False
    requirements = payload.get("requirements")
    return isinstance(requirements, list) and len(requirements) > 0


def architecture_ready(project_root: Path) -> bool:
    docs_dir = project_root / "docs"
    meta = load_json_file(docs_dir / "architecture-meta.json")
    return (
        file_has_content(docs_dir / "architecture.md")
        and file_has_content(docs_dir / "shared-components.md")
        and isinstance(meta, dict)
        and isinstance(meta.get("ui_required"), bool)
    )


def context_ready(project_root: Path) -> bool:
    docs_dir = project_root / "docs"
    test_plan = load_json_file(docs_dir / "test-plan.json")
    project_runtime = load_project_runtime(project_root) or "claude"
    context_file = root_context_filename(project_runtime)
    return (
        file_has_content(project_root / context_file)
        and file_has_content(project_root / "CODE_MAP.md")
        and isinstance(test_plan, dict)
        and isinstance(test_plan.get("coverage"), list)
    )


def ui_required(project_root: Path) -> bool:
    return bool(load_architecture_meta(project_root).get("ui_required"))


def ui_ready(project_root: Path) -> bool:
    ui_dir = project_root / "docs" / "ui"
    return all(
        file_has_content(path)
        for path in [
            ui_dir / "template-selection.md",
            ui_dir / "design-system.md",
            ui_dir / "page-archetypes.md",
            ui_dir / "states.md",
        ]
    )


def needs_scaffold(project_root: Path) -> bool:
    tasks = all_tasks(project_root)
    scaffold_task = next((task for task in tasks if task.id == SCAFFOLD_TASK_ID), None)
    if scaffold_task is not None:
        return scaffold_task.status != "verified"
    return not (project_root / "backend").exists()


def work_items_ready(project_root: Path) -> bool:
    payload = load_json_file(project_root / "docs" / "work-items.json")
    if payload is None:
        return False
    items = payload.get("items")
    return isinstance(items, list) and len(items) > 0


def discover_requirements_source(project_root: Path, explicit_path: str | None = None) -> Path | None:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    docs_dir = project_root / "docs"
    matches = sorted(path for path in docs_dir.glob("requirements-source.*") if path.is_file())
    return matches[0] if matches else None


def work_item_contract_errors(project_root: Path) -> dict[str, list[str]]:
    tasks = all_tasks(project_root)
    contracts = lint_task_contracts(tasks)
    return {
        task_id: payload["errors"]
        for task_id, payload in contracts.items()
        if payload["errors"]
    }
