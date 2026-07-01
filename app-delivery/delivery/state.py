from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .runtime_config import resolve_project_root


DOCS_DIR = Path("docs")
WORK_ITEMS_FILE = DOCS_DIR / "work-items.json"
WORK_ITEMS_VIEW_FILE = DOCS_DIR / "work-items.md"
TEST_RESULTS_FILE = DOCS_DIR / "test-results.json"
TEST_PLAN_FILE = DOCS_DIR / "test-plan.json"
GATES_FILE = DOCS_DIR / "gates.json"
ARCHITECTURE_META_FILE = DOCS_DIR / "architecture-meta.json"
RUNTIME_DIR = Path(".app-delivery-runtime")
LOCKS_DIR = RUNTIME_DIR / "locks"
SESSION_STATE_FILE = RUNTIME_DIR / "session-state.json"
LOGS_DIR = RUNTIME_DIR / "logs"
TASK_LOG_FILE = RUNTIME_DIR / "task-log.jsonl"
ACTIVE_TASKS_DIR = RUNTIME_DIR / "active-tasks"
TASK_RUNTIME_DIR = RUNTIME_DIR / "task-runtime"
REVIEWS_DIR = DOCS_DIR / "reviews"
TOKEN_USAGE_LOG_FILE = RUNTIME_DIR / "token-usage.jsonl"

MARKDOWN_REQ_COLLAPSE_THRESHOLD = 8
MARKDOWN_REQ_PREVIEW_COUNT = 5
WORK_ITEMS_JSON_REFERENCE = "See docs/work-items.json for details"


@dataclass(frozen=True)
class Paths:
    project_root: Path

    @property
    def docs_dir(self) -> Path:
        return self.project_root / DOCS_DIR

    @property
    def work_items(self) -> Path:
        return self.project_root / WORK_ITEMS_FILE

    @property
    def work_items_view(self) -> Path:
        return self.project_root / WORK_ITEMS_VIEW_FILE

    @property
    def test_results(self) -> Path:
        return self.project_root / TEST_RESULTS_FILE

    @property
    def test_plan(self) -> Path:
        return self.project_root / TEST_PLAN_FILE

    @property
    def gates(self) -> Path:
        return self.project_root / GATES_FILE

    @property
    def architecture_meta(self) -> Path:
        return self.project_root / ARCHITECTURE_META_FILE

    @property
    def runtime_dir(self) -> Path:
        return self.project_root / RUNTIME_DIR

    @property
    def locks_dir(self) -> Path:
        return self.project_root / LOCKS_DIR

    @property
    def session_state(self) -> Path:
        return self.project_root / SESSION_STATE_FILE

    @property
    def logs_dir(self) -> Path:
        return self.project_root / LOGS_DIR

    @property
    def task_log(self) -> Path:
        return self.project_root / TASK_LOG_FILE

    @property
    def active_tasks_dir(self) -> Path:
        return self.project_root / ACTIVE_TASKS_DIR

    @property
    def task_runtime_dir(self) -> Path:
        return self.project_root / TASK_RUNTIME_DIR

    @property
    def reviews_dir(self) -> Path:
        return self.project_root / REVIEWS_DIR

    @property
    def token_usage_log(self) -> Path:
        return self.project_root / TOKEN_USAGE_LOG_FILE


def project_paths(project_root: Path | str) -> Paths:
    return Paths(resolve_project_root(project_root))


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return dt.date.today().isoformat()


def ensure_runtime_dirs(project_root: Path | str) -> Paths:
    paths = project_paths(project_root)
    paths.docs_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.locks_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.active_tasks_dir.mkdir(parents=True, exist_ok=True)
    paths.task_runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.task_log.parent.mkdir(parents=True, exist_ok=True)
    paths.task_log.touch(exist_ok=True)
    paths.token_usage_log.parent.mkdir(parents=True, exist_ok=True)
    paths.token_usage_log.touch(exist_ok=True)
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _compact_requirement_ids(values: list[Any]) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized:
        return "-"
    if len(normalized) <= MARKDOWN_REQ_COLLAPSE_THRESHOLD:
        return ", ".join(normalized)
    preview = ", ".join(normalized[:MARKDOWN_REQ_PREVIEW_COUNT])
    return f"{preview}, ... {WORK_ITEMS_JSON_REFERENCE}"


def _compact_session_ids(values: list[Any], current_session_id: str | None = None) -> str:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in [*(values if isinstance(values, list) else []), current_session_id or ""]:
        session_id = str(value or "").strip()
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        normalized.append(session_id)
    if not normalized:
        return "-"
    if len(normalized) <= 2:
        return ", ".join(normalized)
    return f"{normalized[0]}, ... {normalized[-1]} ({len(normalized)} total; see docs/work-items.json)"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    file_descriptor, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _slug(value: str) -> str:
    normalized = str(value or "").strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._") or "task"


def active_task_record_path(project_root: Path | str, *, runtime: str, task_id: str, session_id: str = "") -> Path:
    paths = ensure_runtime_dirs(project_root)
    record_name = "-".join(part for part in [_slug(runtime), _slug(task_id), _slug(session_id or str(os.getpid()))] if part)
    return paths.active_tasks_dir / f"{record_name}.json"


def write_active_task_record(path: Path, payload: dict[str, Any]) -> None:
    existing = load_json(path, {})
    body = existing if isinstance(existing, dict) else {}
    body = {**body, **dict(payload)}
    body["updated_at"] = utc_now_iso()
    body.setdefault("created_at", body["updated_at"])
    write_json(path, body)


def remove_active_task_record(path: Path | None) -> None:
    if path is None:
        return
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def append_task_log_event(
    project_root: Path | str,
    *,
    runtime: str,
    level: str,
    message: str,
    task_id: str = "",
    task_title: str = "",
    session_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    paths = ensure_runtime_dirs(project_root)
    payload: dict[str, Any] = {
        "ts": utc_now_iso(),
        "runtime": runtime,
        "level": level,
        "message": message,
    }
    if task_id:
        payload["task_id"] = task_id
    if task_title:
        payload["task_title"] = task_title
    if session_id:
        payload["session_id"] = session_id
    if extra:
        payload.update(extra)
    with paths.task_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_lock_metadata_to_handle(handle: Any, metadata: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(metadata, ensure_ascii=False) + "\n")
    handle.flush()
    with contextlib.suppress(OSError):
        os.fsync(handle.fileno())


def read_lock_metadata(lock_path: Path | str) -> dict[str, Any]:
    path = Path(lock_path)
    for _ in range(3):
        payload = load_json(path, {})
        if isinstance(payload, dict) and payload:
            return payload
        if not path.exists():
            return {}
        time.sleep(0.01)
    payload = load_json(path, {})
    return payload if isinstance(payload, dict) else {}


@contextlib.contextmanager
def acquire_lock(project_root: Path | str, name: str = "ledger", timeout: int = 30, heartbeat_interval: float = 10.0) -> Iterator[Path]:
    paths = ensure_runtime_dirs(project_root)
    lock_path = paths.locks_dir / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        start = dt.datetime.now(dt.timezone.utc)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                elapsed = (dt.datetime.now(dt.timezone.utc) - start).total_seconds()
                if elapsed >= timeout:
                    raise TimeoutError(f"could not acquire lock {lock_path} within {timeout}s")
                time.sleep(0.2)
        metadata = {
            "lock_name": name,
            "pid": os.getpid(),
            "acquired_at": utc_now_iso(),
            "heartbeat_at": utc_now_iso(),
            "project": str(paths.project_root),
        }
        _write_lock_metadata_to_handle(handle, metadata)
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None

        def heartbeat_worker() -> None:
            while not heartbeat_stop.wait(heartbeat_interval):
                metadata["heartbeat_at"] = utc_now_iso()
                _write_lock_metadata_to_handle(handle, metadata)

        if heartbeat_interval > 0:
            heartbeat_thread = threading.Thread(target=heartbeat_worker, name=f"app-delivery-lock-{name}", daemon=True)
            heartbeat_thread.start()
        try:
            yield lock_path
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def default_work_items(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    return {
        "schema_version": "2",
        "project": paths.project_root.name,
        "generated_at": utc_now_iso(),
        "last_updated_commit": "",
        "items": [],
    }


def default_test_results(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    return {
        "schema_version": "1",
        "project": paths.project_root.name,
        "generated_at": utc_now_iso(),
        "results": [],
        "full_suite_results": {
            "last_run": None,
            "passed": False,
            "total": 0,
            "passed_count": 0,
            "failed_count": 0,
            "scores": {},
        },
    }


def load_work_items(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    payload = load_json(paths.work_items, default_work_items(project_root))
    if not isinstance(payload, dict):
        return default_work_items(project_root)
    payload.setdefault("schema_version", "2")
    payload.setdefault("project", paths.project_root.name)
    payload.setdefault("generated_at", utc_now_iso())
    payload.setdefault("last_updated_commit", "")
    payload.setdefault("items", [])
    return payload


def save_work_items(project_root: Path | str, payload: dict[str, Any]) -> None:
    paths = ensure_runtime_dirs(project_root)
    payload = dict(payload)
    payload["generated_at"] = utc_now_iso()
    write_json(paths.work_items, payload)
    render_work_items_markdown(project_root, payload)


def load_test_results(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    payload = load_json(paths.test_results, default_test_results(project_root))
    if not isinstance(payload, dict):
        return default_test_results(project_root)
    payload.setdefault("schema_version", "1")
    payload.setdefault("project", paths.project_root.name)
    payload.setdefault("generated_at", utc_now_iso())
    payload.setdefault("results", [])
    payload.setdefault(
        "full_suite_results",
        {
            "last_run": None,
            "passed": False,
            "total": 0,
            "passed_count": 0,
            "failed_count": 0,
            "scores": {},
        },
    )
    return payload


def save_test_results(project_root: Path | str, payload: dict[str, Any]) -> None:
    paths = ensure_runtime_dirs(project_root)
    payload = dict(payload)
    payload["generated_at"] = utc_now_iso()
    write_json(paths.test_results, payload)


def load_test_plan(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    payload = load_json(
        paths.test_plan,
        {
            "schema_version": "1",
            "generated_at": utc_now_iso(),
            "coverage": [],
        },
    )
    if not isinstance(payload, dict):
        return {"schema_version": "1", "generated_at": utc_now_iso(), "coverage": []}
    payload.setdefault("schema_version", "1")
    payload.setdefault("generated_at", utc_now_iso())
    payload.setdefault("coverage", [])
    return payload


def save_test_plan(project_root: Path | str, payload: dict[str, Any]) -> None:
    paths = ensure_runtime_dirs(project_root)
    payload = dict(payload)
    payload["generated_at"] = utc_now_iso()
    write_json(paths.test_plan, payload)


def load_gates(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    payload = load_json(
        paths.gates,
        {
            "schema_version": "1",
            "generated_at": utc_now_iso(),
            "project": paths.project_root.name,
            "complexity": {"tier": "S", "score": 0, "signals": {}},
            "gates": [],
        },
    )
    if not isinstance(payload, dict):
        return {"schema_version": "1", "generated_at": utc_now_iso(), "project": paths.project_root.name, "complexity": {"tier": "S", "score": 0, "signals": {}}, "gates": []}
    payload.setdefault("schema_version", "1")
    payload.setdefault("generated_at", utc_now_iso())
    payload.setdefault("project", paths.project_root.name)
    payload.setdefault("complexity", {"tier": "S", "score": 0, "signals": {}})
    payload.setdefault("gates", [])
    return payload


def save_gates(project_root: Path | str, payload: dict[str, Any]) -> None:
    paths = ensure_runtime_dirs(project_root)
    payload = dict(payload)
    payload["generated_at"] = utc_now_iso()
    write_json(paths.gates, payload)


def load_architecture_meta(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    payload = load_json(
        paths.architecture_meta,
        {
            "schema_version": "1",
            "generated_at": utc_now_iso(),
            "ui_required": False,
        },
    )
    if not isinstance(payload, dict):
        return {"schema_version": "1", "generated_at": utc_now_iso(), "ui_required": False}
    payload.setdefault("schema_version", "1")
    payload.setdefault("generated_at", utc_now_iso())
    payload.setdefault("ui_required", False)
    return payload


def save_architecture_meta(project_root: Path | str, payload: dict[str, Any]) -> None:
    paths = ensure_runtime_dirs(project_root)
    payload = dict(payload)
    payload["generated_at"] = utc_now_iso()
    write_json(paths.architecture_meta, payload)


def load_session_state(project_root: Path | str) -> dict[str, Any]:
    paths = ensure_runtime_dirs(project_root)
    payload = load_json(paths.session_state, {"active": None, "retired": [], "history": []})
    if not isinstance(payload, dict):
        return {"active": None, "retired": [], "history": []}
    payload.setdefault("active", None)
    payload.setdefault("retired", [])
    payload.setdefault("history", [])
    return payload


def save_session_state(project_root: Path | str, payload: dict[str, Any]) -> None:
    paths = ensure_runtime_dirs(project_root)
    write_json(paths.session_state, payload)


def process_alive(pid: int) -> bool:
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


def load_active_task_records(project_root: Path | str) -> list[dict[str, Any]]:
    paths = ensure_runtime_dirs(project_root)
    payloads: list[dict[str, Any]] = []
    for path in sorted(paths.active_tasks_dir.glob("*.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if not process_alive(pid):
            continue
        payloads.append(payload)
    return payloads


def load_stale_active_task_records(project_root: Path | str) -> list[dict[str, Any]]:
    paths = ensure_runtime_dirs(project_root)
    payloads: list[dict[str, Any]] = []
    for path in sorted(paths.active_tasks_dir.glob("*.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if process_alive(pid):
            continue
        payloads.append(payload)
    return payloads


def prune_stale_active_task_records(project_root: Path | str) -> list[dict[str, Any]]:
    paths = ensure_runtime_dirs(project_root)
    removed: list[dict[str, Any]] = []
    for path in sorted(paths.active_tasks_dir.glob("*.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        if process_alive(pid):
            continue
        removed.append(payload)
        with contextlib.suppress(OSError):
            path.unlink()
    return removed


def latest_task_log_event(project_root: Path | str) -> dict[str, Any] | None:
    paths = ensure_runtime_dirs(project_root)
    try:
        lines = paths.task_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def task_runtime_state_path(project_root: Path | str, task_id: str) -> Path:
    paths = ensure_runtime_dirs(project_root)
    normalized = str(task_id or "").strip()
    return paths.task_runtime_dir / f"{normalized}.json"


def load_task_runtime_state(project_root: Path | str, task_id: str) -> dict[str, Any]:
    path = task_runtime_state_path(project_root, task_id)
    payload = load_json(path, {})
    return payload if isinstance(payload, dict) else {}


def normalize_task_runtime_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = dict(payload or {})
    status = str(state.get("status") or "").strip()
    if status != "running":
        return state
    pid_values: list[int] = []
    for key in ("runtime_pid", "wrapper_pid", "pid"):
        try:
            pid = int(state.get(key) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            pid_values.append(pid)
    if pid_values and any(process_alive(pid) for pid in pid_values):
        return state
    state["status"] = "interrupted"
    state.setdefault("interrupted_at", utc_now_iso())
    return state


def _normalize_failure_signature(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def remember_task_runtime_failure(
    project_root: Path | str,
    task_id: str,
    *,
    kind: str,
    message: str,
    session_id: str | None = None,
) -> Path:
    existing = load_task_runtime_state(project_root, task_id)
    signature = _normalize_failure_signature(message)
    previous_signature = str(existing.get("failure_signature") or "").strip()
    previous_kind = str(existing.get("failure_kind") or "").strip()
    previous_count = int(existing.get("failure_count") or 0)
    same_failure_kind = bool(kind) and kind == previous_kind and kind in {"guard_blocked", "scope_violation", "shared_scope_regression"}
    failure_count = previous_count + 1 if signature and (signature == previous_signature or same_failure_kind) else 1
    return save_task_runtime_state(
        project_root,
        task_id,
        {
            "failure_kind": kind,
            "failure_signature": signature,
            "failure_message": str(message or "")[:4000],
            "failure_count": failure_count,
            "failure_updated_at": utc_now_iso(),
            "session_id": str(session_id or "").strip() or existing.get("session_id") or None,
        },
    )


def clear_task_runtime_failure(project_root: Path | str, task_id: str) -> Path:
    return save_task_runtime_state(
        project_root,
        task_id,
        {
            "failure_kind": "",
            "failure_signature": "",
            "failure_message": "",
            "failure_count": 0,
            "failure_updated_at": utc_now_iso(),
        },
    )


def repeated_task_runtime_failure_block(project_root: Path | str, task_id: str) -> dict[str, Any] | None:
    state = normalize_task_runtime_state(load_task_runtime_state(project_root, task_id))
    failure_signature = str(state.get("failure_signature") or "").strip()
    failure_kind = str(state.get("failure_kind") or "").strip()
    failure_count = int(state.get("failure_count") or 0)
    if not failure_signature:
        return None
    required_failures = 1 if failure_kind in {"guard_blocked", "scope_violation", "shared_scope_regression"} else 2
    if failure_count < required_failures:
        return None
    task_label = str(task_id or "task").strip() or "task"
    if failure_kind == "guard_blocked":
        message = (
            f"Refusing to relaunch {task_label}: the same pre-tool guard block happened {failure_count} time(s) in a row. "
            "Repair the task binding, scope contract, or routing state before retrying."
        )
    elif failure_kind == "scope_violation":
        message = (
            f"Refusing to relaunch {task_label}: the same out-of-scope change happened {failure_count} time(s) in a row. "
            "Refresh the canonical work-item scope before retrying implementation."
        )
    elif failure_kind == "shared_scope_regression":
        message = (
            f"Refusing to relaunch {task_label}: the same shared-scope regression happened {failure_count} time(s) in a row. "
            "Replan the shared change and affected task contracts before retrying."
        )
    else:
        message = (
            f"Refusing to relaunch {task_label}: the same runtime failure happened {failure_count} time(s) in a row. "
            "Inspect the repeated failure and repair the contract or implementation approach before retrying."
        )
    return {
        "task_id": task_label,
        "failure_kind": failure_kind,
        "failure_count": failure_count,
        "failure_signature": failure_signature,
        "message": message,
    }


def save_task_runtime_state(project_root: Path | str, task_id: str, payload: dict[str, Any]) -> Path:
    path = task_runtime_state_path(project_root, task_id)
    existing = load_task_runtime_state(project_root, task_id)
    body = {**existing, **dict(payload)}
    body["task_id"] = str(task_id or "").strip()
    body["updated_at"] = utc_now_iso()
    body.setdefault("created_at", body["updated_at"])
    write_json(path, body)
    return path


def load_all_task_runtime_states(project_root: Path | str) -> dict[str, dict[str, Any]]:
    paths = ensure_runtime_dirs(project_root)
    payloads: dict[str, dict[str, Any]] = {}
    for path in sorted(paths.task_runtime_dir.glob("*.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        task_id = str(payload.get("task_id") or path.stem).strip()
        if not task_id:
            continue
        payloads[task_id] = payload
    return payloads


def render_work_items_markdown(project_root: Path | str, payload: dict[str, Any] | None = None) -> None:
    paths = ensure_runtime_dirs(project_root)
    data = payload if payload is not None else load_work_items(project_root)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    lines = [
        f"# Work Items - {data.get('project', paths.project_root.name)}",
        "",
        "> Generated from `docs/work-items.json`. Do not edit this file directly.",
        "",
        f"Generated: {data.get('generated_at', utc_now_iso())}",
        "",
        "| ID | Title | Status | Depends | Sessions | Requirements | Commit |",
        "|----|-------|--------|---------|---------|--------------|--------|",
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        requirements = _compact_requirement_ids(item.get("requirements", []))
        dependencies = ", ".join(str(value) for value in item.get("dependencies", [])) or "-"
        commit = str(item.get("git_commit") or "-")
        session_id = _compact_session_ids(item.get("session_ids", []), str(item.get("status_session_id") or "").strip() or None)
        lines.append(
            f"| {item.get('id', '-') } | {str(item.get('title', '-')).replace('|', '/')} | {item.get('status', '-')} | {dependencies} | {session_id} | {requirements} | {commit} |"
        )
    paths.work_items_view.write_text("\n".join(lines) + "\n", encoding="utf-8")
