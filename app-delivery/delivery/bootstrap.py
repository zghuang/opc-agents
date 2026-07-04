from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .errors import DeliveryError
from .runtime_config import normalize_runtime
from .scaffold import scaffold_project, sync_runtime_support
from .state import ensure_runtime_dirs, utc_now_iso


PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SUPPORTED_STACKS = {"python-react"}
REQUIREMENTS_ARCHIVE_FILENAME = "requirements-source"


def _json_file_pid(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("pid") or payload.get("runtime_pid") or 0)
    except (TypeError, ValueError):
        return 0


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid() or not _pid_is_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.1)
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _project_runtime_pids(project_dir: Path) -> list[int]:
    runtime_dir = project_dir / ".app-delivery-runtime"
    candidates = [
        runtime_dir / "watchdog-state.json",
        runtime_dir / "locks" / "execution.lock",
    ]
    active_tasks_dir = runtime_dir / "active-tasks"
    if active_tasks_dir.exists():
        candidates.extend(sorted(active_tasks_dir.glob("*.json")))
    pids: list[int] = []
    seen: set[int] = set()
    for path in candidates:
        pid = _json_file_pid(path)
        if pid > 0 and pid not in seen:
            seen.add(pid)
            pids.append(pid)
    return pids


def _stop_project_runtime_processes(project_dir: Path) -> None:
    for pid in _project_runtime_pids(project_dir):
        _terminate_pid(pid)


def _rmtree_onerror(function: Any, path: str, _exc_info: Any) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    function(path)


def _force_remove_project_dir(project_dir: Path) -> None:
    _stop_project_runtime_processes(project_dir)
    last_error: OSError | None = None
    for attempt in range(4):
        try:
            shutil.rmtree(project_dir, onerror=_rmtree_onerror)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            _stop_project_runtime_processes(project_dir)
            if attempt < 3:
                time.sleep(0.25 * (attempt + 1))
                continue
    raise DeliveryError(
        code="project_force_remove_failed",
        message=f"failed to replace existing project directory: {project_dir}",
        exit_code=2,
        details={"project_root": str(project_dir), "error": str(last_error) if last_error else "unknown"},
        suggested_action="Stop active app-delivery/runtime/watchdog processes for this project, remove the directory manually if needed, then rerun with --force.",
    )


def normalize_dependency_hints(raw_hints: Any) -> list[dict[str, str]]:
    if not isinstance(raw_hints, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in raw_hints:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        ecosystem = str(row.get("ecosystem") or "project").strip().lower() or "project"
        key = (ecosystem, name.casefold())
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "ecosystem": ecosystem,
                "name": name,
                "source": str(row.get("source") or "requirements-analysis").strip() or "requirements-analysis",
                "reason": str(row.get("reason") or row.get("rationale") or "Requirements or planning artifacts explicitly require this technology choice.").strip(),
                "evidence": str(row.get("evidence") or "").strip(),
            }
        )
    return normalized


def _command_check(name: str, command: list[str]) -> dict[str, Any]:
    def missing_payload() -> dict[str, Any]:
        suggestions = {
            "pnpm": "pnpm is required for frontend installs/tests. With Node 16+ run: corepack enable && corepack prepare pnpm@latest --activate. If corepack is unavailable, run: npm install -g pnpm.",
            "node": "Install Node.js 24 or newer, then rerun setup-opc.sh.",
            "npm": "Install npm with Node.js, or repair the Node.js installation, then rerun setup-opc.sh.",
            "docker": "Install and start Docker, ensure the current user can run `docker --version`, then rerun setup-opc.sh.",
            "git": "Install git and ensure it is on PATH, then rerun setup-opc.sh.",
            "hermes": "Install Hermes and ensure `hermes --version` works in this shell, then rerun setup-opc.sh.",
            "opencode": "Install OpenCode and ensure `opencode --version` works in this shell, then rerun setup-opc.sh.",
            "claude": "Install Claude Code and ensure `claude --version` works in this shell, then rerun setup-opc.sh.",
        }
        executable = command[0]
        return {
            "name": name,
            "status": "missing",
            "message": f"missing command: {executable}",
            "details": {"command": executable, "path": os.environ.get("PATH", "")},
            "suggested_action": suggestions.get(name, f"Install `{executable}` and ensure it is on PATH, then rerun setup-opc.sh."),
        }

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return missing_payload()
    if completed.returncode != 0:
        output = completed.stdout.strip()
        return {
            "name": name,
            "status": "error",
            "message": output or f"command failed: {' '.join(command)}",
            "details": {"command": " ".join(command), "exit_code": completed.returncode},
            "suggested_action": f"Run `{' '.join(command)}` directly, fix the reported error, then rerun setup-opc.sh.",
        }
    output = completed.stdout.strip().splitlines()
    return {"name": name, "status": "ok", "message": output[0] if output else "ok"}


def _framework_checks(framework_root: Path) -> list[dict[str, Any]]:
    required_paths = {
        "delivery": framework_root / "delivery",
        "skills": framework_root / "skills",
        "project_temp": framework_root / "project_temp" / "stacks" / "python-react",
        "mock-server": framework_root / "mock-server",
        "scripts": framework_root / "scripts",
    }
    results: list[dict[str, Any]] = []
    for name, path in required_paths.items():
        if path.exists():
            results.append({"name": f"framework:{name}", "status": "ok", "message": str(path)})
        else:
            results.append({"name": f"framework:{name}", "status": "missing", "message": str(path)})
    return results


def runtime_command_name(runtime: str) -> str:
    return normalize_runtime(runtime)


def doctor_report(
    runtime: str,
    *,
    framework_root: Path | str | None = None,
    opc_home: Path | str | None = None,
) -> dict[str, Any]:
    normalized_runtime = runtime_command_name(runtime)
    root = Path(framework_root).expanduser().resolve() if framework_root is not None else Path(__file__).resolve().parents[1]
    checks = [
        _command_check("python3", [sys.executable, "--version"]),
        _command_check("git", ["git", "--version"]),
        _command_check("node", ["node", "--version"]),
        _command_check("npm", ["npm", "--version"]),
        _command_check("pnpm", ["pnpm", "--version"]),
        _command_check("docker", ["docker", "--version"]),
        _command_check("hermes", ["hermes", "--version"]),
        _command_check(normalized_runtime, [normalized_runtime, "--version"]),
    ]
    checks.extend(_framework_checks(root))
    if opc_home is not None:
        home = Path(opc_home).expanduser().resolve()
        for name in ["bin", "projects", "state"]:
            path = home / name
            checks.append(
                {
                    "name": f"opc_home:{name}",
                    "status": "ok" if path.exists() else "missing",
                    "message": str(path),
                }
            )
    failing = [check for check in checks if check["status"] != "ok"]
    return {
        "status": "ok" if not failing else "fail",
        "runtime": normalized_runtime,
        "framework_root": str(root),
        "checks": checks,
    }


def _git_init(project_dir: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["git", "config", "user.name", "app-delivery"], cwd=project_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["git", "config", "user.email", "app-delivery@local"], cwd=project_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def archive_requirements_source(project_root: Path | str, requirements_path: Path | str) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    source_path = Path(requirements_path).expanduser().resolve()
    docs_dir = project_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix or ".md"
    target = docs_dir / f"{REQUIREMENTS_ARCHIVE_FILENAME}{suffix}"
    if target.exists() and target.resolve() == source_path:
        return target
    shutil.copy2(source_path, target)
    return target


def save_project_dependency_hints(project_root: Path | str, raw_hints: Any) -> list[dict[str, str]]:
    project_dir = Path(project_root).expanduser().resolve()
    docs_dir = project_dir / "docs"
    metadata_path = docs_dir / "project-bootstrap.json"
    if not metadata_path.exists():
        return []
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(metadata, dict):
        return []
    hints = normalize_dependency_hints(raw_hints)
    metadata["dependency_hints"] = hints
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return hints


def initialize_project(
    project_root: Path | str,
    *,
    description: str,
    runtime: str,
    stack: str = "python-react",
    force: bool = False,
    framework_root: Path | str | None = None,
    watchdog_enabled: bool = True,
) -> dict[str, Any]:
    normalized_runtime = runtime_command_name(runtime)
    normalized_stack = str(stack or "python-react").strip().lower()
    if normalized_stack not in SUPPORTED_STACKS:
        raise DeliveryError(code="stack_invalid", message=f"unsupported stack: {stack}", exit_code=2)
    project_dir = Path(project_root).expanduser().resolve()
    project_name = project_dir.name
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise DeliveryError(
            code="project_name_invalid",
            message="project name may contain only letters, numbers, dot, underscore, and hyphen",
            exit_code=2,
        )
    if force and project_dir.exists():
        _force_remove_project_dir(project_dir)
    elif project_dir.exists() and any(project_dir.iterdir()):
        raise DeliveryError(
            code="project_exists",
            message=f"project already exists: {project_dir}",
            exit_code=2,
            suggested_action="Pass --force to replace the project, or choose a new project name.",
        )
    root = Path(framework_root).expanduser().resolve() if framework_root is not None else Path(__file__).resolve().parents[1]
    template_root = root / "project_temp" / "stacks" / normalized_stack
    mock_server_root = root / "mock-server"
    scaffold_project(project_dir, template_root=template_root, mock_server_root=mock_server_root, runtime=normalized_runtime)
    ensure_runtime_dirs(project_dir)
    metadata = {
        "schema_version": "1",
        "project": project_name,
        "description": description,
        "runtime": normalized_runtime,
        "stack": normalized_stack,
        "mock_server": "embedded",
        "watchdog_enabled": bool(watchdog_enabled),
        "host_fallback_enabled": bool(watchdog_enabled),
        "host_fallback_after_seconds": 600,
        "host_fallback_max_attempts": 1,
        "created_at": utc_now_iso(),
        "dependency_hints": [],
    }
    docs_dir = project_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "project-bootstrap.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    readme_path = project_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(
            "\n".join(
                [
                    f"# {project_name}",
                    "",
                    description.strip() or "App-delivery project.",
                    "",
                    "## Next Steps",
                    "",
                    "1. Run spec-review through /app-delivery or the dedicated stage skill.",
                    "2. Continue with arch-design, ui-design when required, and project-context-sync.",
                    "3. Let the delivery loop advance from T000 onward.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    _git_init(project_dir)
    return {
        "status": "ok",
        "project_root": str(project_dir),
        "metadata_path": str(docs_dir / "project-bootstrap.json"),
        "runtime": normalized_runtime,
        "stack": normalized_stack,
        "mock_server_path": str(project_dir / "mock-server"),
    }


def start_preflight(project_root: Path | str, requirements_path: Path | str, *, runtime: str) -> dict[str, Any]:
    normalized_runtime = runtime_command_name(runtime)
    project_dir = Path(project_root).expanduser().resolve()
    requirements_file = Path(requirements_path).expanduser().resolve()
    sync_runtime_support(project_dir, runtime=normalized_runtime)
    payload = {
        "status": "ok",
        "runtime": normalized_runtime,
        "project_root": str(project_dir),
        "requirements_path": str(requirements_file),
        "checks": [],
        "next_action": "run app-delivery",
    }

    def add_check(name: str, ok: bool, message: str) -> None:
        payload["checks"].append({"name": name, "status": "ok" if ok else "fail", "message": message})
        if not ok:
            payload["status"] = "fail"

    add_check("runtime", _command_check(normalized_runtime, [normalized_runtime, "--version"])["status"] == "ok", normalized_runtime)
    add_check("project_exists", project_dir.exists(), str(project_dir))
    add_check("requirements_exists", requirements_file.exists() and requirements_file.is_file(), str(requirements_file))
    add_check("git_repo", (project_dir / ".git").exists(), str(project_dir / ".git"))
    add_check("backend_dir", (project_dir / "backend").exists(), str(project_dir / "backend"))
    add_check("frontend_dir", (project_dir / "frontend").exists(), str(project_dir / "frontend"))
    add_check("mock_server_dir", (project_dir / "mock-server").exists(), str(project_dir / "mock-server"))
    archived_source = None
    if payload["status"] == "ok":
        archived = archive_requirements_source(project_dir, requirements_file)
        archived_source = str(archived)
        add_check("requirements_archived", True, archived_source)
    if payload["status"] != "ok":
        payload["next_action"] = "run setup-opc.sh or new-project.sh first, then retry preflight"
    elif archived_source:
        payload["requirements_source_archive"] = archived_source
    return payload