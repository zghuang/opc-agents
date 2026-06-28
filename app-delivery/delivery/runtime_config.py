from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import DeliveryError


SUPPORTED_RUNTIMES = {"claude", "opencode"}


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _enforce_opc_project_root() -> bool:
    return _truthy_env(os.environ.get("APP_DELIVERY_ENFORCE_OPC_PROJECT_ROOT"))


def _assert_project_under_opc_projects(project_root: Path) -> None:
    allowed_root = (opc_home() / "projects").resolve()
    try:
        project_root.relative_to(allowed_root)
    except ValueError as exc:
        raise DeliveryError(
            code="project_root_outside_opc_projects",
            message=f"project must live under {allowed_root}: {project_root}",
            exit_code=2,
            details={"project_root": str(project_root), "allowed_root": str(allowed_root)},
            suggested_action="Create or run the project from OPC_HOME/projects (for example via new-project.sh) instead of pointing app-delivery at an arbitrary workspace path.",
        ) from exc


def opc_home() -> Path:
    return Path(os.environ.get("OPC_HOME", str(Path.home() / "opc"))).expanduser().resolve()


def resolve_project_root(project_root: Path | str) -> Path:
    raw_value = str(project_root or "").strip()
    path = Path(raw_value).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
    # Treat a single bare project name as OPC_HOME/projects/<name> so CLI calls
    # are stable regardless of the current working directory.
    elif raw_value and not raw_value.startswith(".") and len(path.parts) == 1:
        resolved = (opc_home() / "projects" / path.name).resolve()
    else:
        resolved = path.resolve()
    if _enforce_opc_project_root():
        _assert_project_under_opc_projects(resolved)
    return resolved


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