from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import DeliveryError


SUPPORTED_RUNTIMES = {"claude", "opencode"}


def opc_home() -> Path:
    return Path(os.environ.get("OPC_HOME", str(Path.home() / "opc"))).expanduser().resolve()


def resolve_project_root(project_root: Path | str) -> Path:
    raw_value = str(project_root or "").strip()
    path = Path(raw_value).expanduser()
    if path.is_absolute():
        return path.resolve()
    # Treat a single bare project name as OPC_HOME/projects/<name> so CLI calls
    # are stable regardless of the current working directory.
    if raw_value and not raw_value.startswith(".") and len(path.parts) == 1:
        return (opc_home() / "projects" / path.name).resolve()
    return path.resolve()


def active_runtime_file() -> Path:
    return opc_home() / "state" / "active-runtime"


def normalize_runtime(runtime: str) -> str:
    normalized = str(runtime or "").strip().lower()
    if normalized not in SUPPORTED_RUNTIMES:
        raise DeliveryError(
            code="runtime_invalid",
            message=f"unsupported runtime: {runtime}",
            exit_code=2,
            suggested_action="Use --runtime claude or --runtime opencode, or re-run setup-opc.sh with the intended runtime.",
        )
    return normalized


def load_active_runtime() -> str | None:
    path = active_runtime_file()
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return normalize_runtime(raw)


def load_project_runtime(project_root: Path | str) -> str | None:
    project_dir = resolve_project_root(project_root)
    metadata_path = project_dir / "docs" / "project-bootstrap.json"
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    runtime = str(payload.get("runtime") or "").strip()
    if not runtime:
        return None
    return normalize_runtime(runtime)


def load_project_metadata(project_root: Path | str) -> dict[str, object]:
    project_dir = resolve_project_root(project_root)
    metadata_path = project_dir / "docs" / "project-bootstrap.json"
    if not metadata_path.exists():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_runtime(runtime: str | None = None, *, project_root: Path | str | None = None) -> str:
    if runtime:
        return normalize_runtime(runtime)
    if project_root is not None:
        project_runtime = load_project_runtime(project_root)
        if project_runtime:
            return project_runtime
    active = load_active_runtime()
    if active:
        return active
    raise DeliveryError(
        code="runtime_missing",
        message="no runtime selected for this environment or project",
        exit_code=2,
        suggested_action="Run setup-opc.sh with --runtime claude|opencode, or provide --runtime explicitly during initialization.",
    )


def root_context_filename(runtime: str) -> str:
    return "CLAUDE.md" if normalize_runtime(runtime) == "claude" else "AGENTS.md"