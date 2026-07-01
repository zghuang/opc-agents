from __future__ import annotations

import re
from pathlib import Path
from typing import Any


CONFIG_PATHS = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "litellm-config.yaml",
    "litellm-config.yml",
    "mock-server/docker-compose.yml",
    "mock-server/docker-compose.yaml",
]


def _workflow_paths(project_root: Path) -> list[Path]:
    workflow_dir = project_root / ".github" / "workflows"
    if not workflow_dir.exists():
        return []
    return sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")])


def _inline_mapping_sequence_error(text: str) -> str | None:
    pattern = re.compile(r"^\s*-\s+[^'\"\n]*[\[{][^\n]*:\s", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    line = text.count("\n", 0, match.start()) + 1
    return f"line {line}: quote command/list values that contain inline YAML maps"


def _yaml_parse_error(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return _inline_mapping_sequence_error(text)
    try:
        yaml.safe_load(text)
    except Exception as exc:
        mark = getattr(exc, "problem_mark", None)
        location = ""
        if mark is not None:
            location = f" at line {int(mark.line) + 1}, column {int(mark.column) + 1}"
        return f"{exc.__class__.__name__}{location}: {exc}"
    return None


def validate_project_config_files(project_root: Path | str) -> list[dict[str, str]]:
    project_dir = Path(project_root).expanduser().resolve()
    paths = [project_dir / relative for relative in CONFIG_PATHS]
    paths.extend(_workflow_paths(project_dir))
    failures: list[dict[str, str]] = []
    for path in sorted({candidate for candidate in paths if candidate.exists()}):
        error = _yaml_parse_error(path)
        if error:
            failures.append({"path": str(path.relative_to(project_dir)), "error": error})
    return failures


def config_validation_summary(failures: list[dict[str, str]]) -> str:
    if not failures:
        return ""
    lines = ["configuration validation failed"]
    lines.extend(f"- {row['path']}: {row['error']}" for row in failures)
    return "\n".join(lines)