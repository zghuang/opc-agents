from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import ensure_runtime_dirs, load_json, write_json, utc_now_iso


ENV_STATE_FILE = "env-state.json"
VALIDATION_STRIP_ENV_VARS = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__")
APP_LOCAL_SERVICE_NAMES = ("backend", "frontend", "web", "app", "api", "mocks", "mock-server")
BROWSER_E2E_MARKERS = ("frontend/e2e/", "playwright", "npm run e2e", "pnpm run e2e", "yarn e2e")
BACKEND_ENTRYPOINT_EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "migrations",
    "node_modules",
    "tests",
    "venv",
}


@dataclass(frozen=True)
class EnvPrepareResult:
    ready: bool
    profile: str
    summary: str
    actions_run: list[str]
    details: dict[str, Any]


def _env_state_path(project_root: Path | str) -> Path:
    return ensure_runtime_dirs(project_root).runtime_dir / ENV_STATE_FILE


def load_env_state(project_root: Path | str) -> dict[str, Any]:
    return load_json(
        _env_state_path(project_root),
        {"schema_version": "1", "updated_at": None, "profiles": {}, "services": {}, "compose_file": None},
    )


def save_env_state(project_root: Path | str, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body.setdefault("schema_version", "1")
    body["updated_at"] = utc_now_iso()
    write_json(_env_state_path(project_root), body)


def validation_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in VALIDATION_STRIP_ENV_VARS:
        env.pop(key, None)
    return env


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            continue
        values[normalized_key] = value.strip().strip('"').strip("'")
    return values


def _project_env_value(project_root: Path | str | None, key: str) -> str:
    if project_root is None:
        return ""
    project_dir = Path(project_root).expanduser().resolve()
    for path in (project_dir / ".env", project_dir / "backend" / ".env"):
        value = _dotenv_values(path).get(key, "").strip()
        if value:
            return value
    return ""


def _configured_backend_host(project_root: Path | str | None = None) -> str:
    return str(os.environ.get("APP_DELIVERY_BACKEND_HOST") or _project_env_value(project_root, "BACKEND_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _configured_backend_port(project_root: Path | str | None = None) -> str:
    return str(
        os.environ.get("APP_DELIVERY_BACKEND_PORT")
        or _project_env_value(project_root, "BACKEND_HOST_PORT")
        or _project_env_value(project_root, "BACKEND_PORT")
        or "8000"
    ).strip() or "8000"


def _configured_backend_base_url(project_root: Path | str | None = None) -> str:
    explicit = str(os.environ.get("APP_DELIVERY_BACKEND_BASE_URL") or _project_env_value(project_root, "BACKEND_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://{_configured_backend_host(project_root)}:{_configured_backend_port(project_root)}"


PATH_LITERAL_PATTERN = re.compile(r"['\"](?P<path>/[A-Za-z0-9_./{}-]+)['\"]")
HEALTH_PATH_SEGMENTS = {"health", "healthz", "ready", "readyz", "readiness", "live", "livez", "liveness", "status", "ping"}


def _looks_like_backend_health_path(value: str) -> bool:
    path = str(value or "").strip().split("?", 1)[0].rstrip("/")
    if not path.startswith("/") or "{" in path or "}" in path:
        return False
    segments = [segment.casefold() for segment in path.split("/") if segment]
    return bool(segments) and segments[-1] in HEALTH_PATH_SEGMENTS


def _discovered_backend_health_paths(project_root: Path | str | None) -> list[str]:
    if project_root is None:
        return []
    backend_root = Path(project_root).expanduser().resolve() / "backend"
    if not backend_root.exists():
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for path in sorted(backend_root.rglob("*.py")):
        try:
            relative = path.relative_to(backend_root)
        except ValueError:
            continue
        if any(part in BACKEND_ENTRYPOINT_EXCLUDED_PARTS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in PATH_LITERAL_PATTERN.finditer(text):
            value = match.group("path").strip()
            if _looks_like_backend_health_path(value) and value not in seen:
                seen.add(value)
                ordered.append(value)
    return ordered


def _configured_backend_health_urls(project_root: Path | str | None = None) -> list[str]:
    explicit = str(os.environ.get("APP_DELIVERY_BACKEND_HEALTH_URL") or _project_env_value(project_root, "BACKEND_HEALTH_URL") or "").strip()
    if explicit:
        return [explicit]
    base_url = _configured_backend_base_url(project_root)
    paths = [*_discovered_backend_health_paths(project_root), "/health", "/api/health"]
    urls: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized_path = path if path.startswith("/") else f"/{path}"
        url = base_url + normalized_path
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _configured_backend_health_url(project_root: Path | str | None = None) -> str:
    return _configured_backend_health_urls(project_root)[0]


def _backend_entrypoint_module(backend_root: Path, main_path: Path) -> str | None:
    relative = main_path.relative_to(backend_root)
    if any(part in BACKEND_ENTRYPOINT_EXCLUDED_PARTS or part.startswith(".") for part in relative.parts[:-1]):
        return None
    if relative.parts == ("main.py",):
        module_parts = ["main"]
    elif relative.parts and relative.parts[0] == "src":
        module_parts = list(relative.with_suffix("").parts[1:])
    else:
        module_parts = list(relative.with_suffix("").parts)
    if not module_parts or not all(part.isidentifier() for part in module_parts):
        return None
    return ".".join(module_parts) + ":app"


def _backend_entrypoint_candidates(backend_root: Path) -> list[str]:
    prioritized_paths = [backend_root / "main.py", backend_root / "app" / "main.py", backend_root / "api" / "main.py"]
    discovered_paths = sorted(
        backend_root.rglob("main.py"),
        key=lambda path: (len(path.relative_to(backend_root).parts), path.relative_to(backend_root).as_posix()),
    )
    candidates: list[str] = []
    seen_paths: set[Path] = set()
    for path in [*prioritized_paths, *discovered_paths]:
        resolved = path.resolve()
        if resolved in seen_paths or not path.exists():
            continue
        seen_paths.add(resolved)
        module = _backend_entrypoint_module(backend_root, path)
        if module is not None and module not in candidates:
            candidates.append(module)
    return candidates


def default_backend_e2e_command(project_root: Path | str) -> str | None:
    if str(os.environ.get("E2E_BACKEND_CMD") or "").strip():
        return None
    project_dir = Path(project_root).expanduser().resolve()
    backend_root = project_dir / "backend"
    if not backend_root.joinpath("pyproject.toml").exists():
        return None
    entrypoint = next(iter(_backend_entrypoint_candidates(backend_root)), None)
    if entrypoint is None:
        return None
    return (
        f"cd {shlex.quote(str(backend_root))} && uv run uvicorn {entrypoint} "
        f"--host {_configured_backend_host(project_dir)} --port {_configured_backend_port(project_dir)}"
    )


def frontend_e2e_env_prefix(project_root: Path | str) -> str:
    env_parts: list[str] = []
    backend_command = default_backend_e2e_command(project_root)
    if backend_command is not None:
        env_parts.append(f"E2E_BACKEND_CMD={shlex.quote(backend_command)}")
    if backend_command is not None and not str(os.environ.get("VITE_API_PROXY_TARGET") or "").strip():
        env_parts.append(f"VITE_API_PROXY_TARGET={_configured_backend_base_url(project_root)}")
    if backend_command is not None and not str(os.environ.get("E2E_BACKEND_HEALTHCHECK_URL") or "").strip():
        env_parts.append(f"E2E_BACKEND_HEALTHCHECK_URL={_configured_backend_health_url(project_root)}")
    return (" ".join(env_parts) + " ") if env_parts else ""


def classify_test_environment(task: dict[str, Any], spec: str) -> str:
    task_id = str(task.get("id") or "").strip()
    normalized = str(spec or "").strip().casefold()
    if any(marker in normalized for marker in BROWSER_E2E_MARKERS):
        return "browser_e2e"
    if task_id == "T001":
        return "shared_infra"
    return "none"


def project_has_browser_e2e(project_root: Path | str) -> bool:
    project_dir = Path(project_root).expanduser().resolve()
    frontend_root = project_dir / "frontend"
    if not frontend_root.exists():
        return False
    if any(frontend_root.glob("e2e/**/*.spec.*")):
        return True
    package_json = frontend_root / "package.json"
    if not package_json.exists():
        return False
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
    return any(str(name).strip() in {"e2e", "test:e2e", "playwright"} for name in scripts)


def _package_manager(frontend_root: Path) -> str:
    if frontend_root.joinpath("pnpm-lock.yaml").exists():
        return "pnpm"
    if frontend_root.joinpath("yarn.lock").exists():
        return "yarn"
    return "npm"


def _install_frontend_dependencies(frontend_root: Path) -> list[str]:
    if not frontend_root.joinpath("package.json").exists() or frontend_root.joinpath("node_modules").exists():
        return []
    package_manager = _package_manager(frontend_root)
    command = {
        "pnpm": ["pnpm", "install", "--silent"],
        "yarn": ["yarn", "install", "--silent"],
        "npm": ["npm", "install", "--silent"],
    }[package_manager]
    completed = subprocess.run(
        command,
        cwd=frontend_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=validation_subprocess_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout or "frontend dependency install failed").strip()[:4000])
    return [f"{package_manager} install"]


def _compose_service_names(project_root: Path) -> list[str]:
    compose_file = project_root / "docker-compose.yml"
    if not compose_file.exists():
        return []
    completed = subprocess.run(
        ["docker", "compose", "config", "--services"],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=validation_subprocess_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout or "docker compose config --services failed").strip()[:4000])
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _explicit_shared_services() -> list[str]:
    raw = str(os.environ.get("APP_DELIVERY_SHARED_SERVICES") or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for value in raw.split(","):
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _shared_service_names(project_root: Path) -> list[str]:
    declared = _compose_service_names(project_root)
    explicit = _explicit_shared_services()
    if explicit:
        return [name for name in declared if name in explicit]
    return [name for name in declared if name not in APP_LOCAL_SERVICE_NAMES]


def _service_status(project_root: Path, service: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "compose", "ps", service, "--format", "json"],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=validation_subprocess_env(),
        check=False,
    )
    if completed.returncode != 0:
        return {"service": service, "status": "error", "message": (completed.stdout or "").strip()}
    text = completed.stdout.strip()
    if not text:
        return {"service": service, "status": "missing", "message": "service not listed in docker compose ps"}
    lines = [line for line in text.splitlines() if line.strip()]
    payload: Any
    if len(lines) == 1:
        payload = json.loads(lines[0])
    else:
        payload = [json.loads(line) for line in lines]
    row = payload[0] if isinstance(payload, list) else payload
    health = str(row.get("Health") or "").strip().lower()
    state = str(row.get("State") or row.get("Status") or "").strip().lower()
    if health:
        ready = health == "healthy"
    else:
        ready = state.startswith("running") or state.startswith("up")
    return {
        "service": service,
        "status": "ready" if ready else "starting",
        "health": health or None,
        "state": state or None,
    }


def _ensure_shared_services(project_root: Path, services: list[str]) -> list[str]:
    if not services:
        return []
    completed = subprocess.run(
        ["docker", "compose", "up", "-d", *services],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=validation_subprocess_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout or "docker compose up failed").strip()[:4000])
    return [f"docker compose up -d {' '.join(services)}"]


def _wait_for_services(project_root: Path, services: list[str], *, timeout_seconds: int = 45) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_statuses: list[dict[str, Any]] = []
    while time.time() < deadline:
        last_statuses = [_service_status(project_root, service) for service in services]
        if all(row.get("status") == "ready" for row in last_statuses):
            return {"ready": True, "services": last_statuses}
        time.sleep(1.0)
    return {"ready": False, "services": last_statuses}


def _probe_backend_health(project_root: Path, command: str, *, timeout_seconds: int = 20) -> dict[str, Any]:
    shell = os.environ.get("APP_DELIVERY_SHELL") or shutil.which("zsh") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    process = subprocess.Popen(
        [shell, "-lc", command],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=validation_subprocess_env(),
    )
    output = ""
    deadline = time.time() + timeout_seconds
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                return {"ready": False, "message": (output or "backend exited before health probe succeeded").strip()[:4000]}
            for health_url in _configured_backend_health_urls(project_root):
                try:
                    response = urllib.request.urlopen(health_url, timeout=1.5)
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    continue
                with response:
                    body = response.read().decode("utf-8", errors="replace")
                    if response.status == 200:
                        return {"ready": True, "message": body.strip() or "ok", "health_url": health_url}
            time.sleep(0.5)
        return {"ready": False, "message": "backend health probe timed out"}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            remainder = process.stdout.read()
            if remainder:
                output = f"{output}\n{remainder}".strip()


def warm_shared_test_environment(project_root: Path | str, *, reason: str = "") -> EnvPrepareResult:
    project_dir = Path(project_root).expanduser().resolve()
    services = _shared_service_names(project_dir)
    if not services:
        state = load_env_state(project_dir)
        state["profiles"]["shared_infra"] = {"ready": True, "last_ready_at": utc_now_iso(), "reason": reason or None}
        save_env_state(project_dir, state)
        return EnvPrepareResult(True, "shared_infra", "no shared docker services declared", [], {"services": []})
    actions = _ensure_shared_services(project_dir, services)
    waited = _wait_for_services(project_dir, services)
    details = {"services": waited.get("services", []), "reason": reason or None}
    state = load_env_state(project_dir)
    state["compose_file"] = str(project_dir / "docker-compose.yml")
    state["services"] = {row.get("service"): row for row in waited.get("services", []) if str(row.get("service") or "").strip()}
    state["profiles"]["shared_infra"] = {
        "ready": bool(waited.get("ready")),
        "last_ready_at": utc_now_iso() if bool(waited.get("ready")) else None,
        "reason": reason or None,
    }
    save_env_state(project_dir, state)
    if waited.get("ready"):
        return EnvPrepareResult(True, "shared_infra", "shared test services are ready", actions, details)
    summary = "shared test services did not become healthy"
    return EnvPrepareResult(False, "shared_infra", summary, actions, details)


def ensure_task_test_environment(project_root: Path | str, task: dict[str, Any], spec: str) -> EnvPrepareResult:
    project_dir = Path(project_root).expanduser().resolve()
    profile = classify_test_environment(task, spec)
    if profile == "none":
        return EnvPrepareResult(True, profile, "no extra test environment required", [], {})

    actions: list[str] = []
    if profile in {"shared_infra", "browser_e2e"}:
        shared = warm_shared_test_environment(project_dir, reason=f"task {task.get('id') or '-'}")
        actions.extend(shared.actions_run)
        if not shared.ready:
            return EnvPrepareResult(False, profile, shared.summary, actions, shared.details)

    if profile == "shared_infra":
        return EnvPrepareResult(True, profile, "shared test environment is ready", actions, {})

    frontend_root = project_dir / "frontend"
    try:
        actions.extend(_install_frontend_dependencies(frontend_root))
    except RuntimeError as exc:
        return EnvPrepareResult(False, profile, f"frontend dependency install failed: {exc}", actions, {"service_profile": "browser_e2e"})

    backend_command = default_backend_e2e_command(project_dir)
    if backend_command:
        probe = _probe_backend_health(project_dir, backend_command)
        if not probe.get("ready"):
            return EnvPrepareResult(False, profile, f"backend health probe failed: {probe.get('message') or 'unknown error'}", actions, probe)
        details = {"backend_probe": probe}
    else:
        details = {}

    state = load_env_state(project_dir)
    state["profiles"][profile] = {"ready": True, "last_ready_at": utc_now_iso(), "task_id": str(task.get("id") or "").strip() or None}
    save_env_state(project_dir, state)
    return EnvPrepareResult(True, profile, "task test environment is ready", actions, details)


def warm_browser_e2e_environment(project_root: Path | str, *, reason: str = "") -> EnvPrepareResult:
    project_dir = Path(project_root).expanduser().resolve()
    if not project_has_browser_e2e(project_dir):
        state = load_env_state(project_dir)
        state["profiles"]["browser_e2e"] = {"ready": True, "last_ready_at": utc_now_iso(), "reason": reason or "no browser e2e declared"}
        save_env_state(project_dir, state)
        return EnvPrepareResult(True, "browser_e2e", "no browser e2e tests declared", [], {})
    return ensure_task_test_environment(
        project_dir,
        {"id": "T001", "output_tests": ["frontend/e2e/__environment__.spec.ts"]},
        "frontend/e2e/__environment__.spec.ts",
    )
