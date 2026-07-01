from __future__ import annotations

import json
import datetime as dt
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scaffold import sync_runtime_support
from .state import load_session_state, project_paths, save_session_state, utc_now_iso


@dataclass
class RuntimeSession:
    id: str
    runtime: str
    task_count: int
    created_at: str
    status: str
    title: str
    last_heartbeat: str | None = None
    current_task_id: str | None = None


class RuntimeErrorResponse(RuntimeError):
    def __init__(
        self,
        message: str,
        output: str = "",
        *,
        session_id: str = "",
        kind: str = "",
        returncode: int | None = None,
    ) -> None:
        self.output = output
        self.session_id = str(session_id or "").strip()
        self.kind = str(kind or "").strip()
        self.returncode = returncode
        super().__init__(message)


def _flatten_error_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, dict):
        items: list[str] = []
        for key, nested in value.items():
            items.append(str(key))
            items.extend(_flatten_error_values(nested))
        return items
    if isinstance(value, list):
        items: list[str] = []
        for nested in value:
            items.extend(_flatten_error_values(nested))
        return items
    return [str(value)]


def _classify_runtime_error(text: str) -> str:
    lowered = str(text or "").lower()
    if "pre-tool guard blocked" in lowered:
        return "guard_blocked"
    if re.search(r"outside (its )?declared scope|out-of-scope|scope error|declared output paths", lowered):
        return "scope_violation"
    if "shared-scope" in lowered:
        return "shared_scope_regression"
    if re.search(r"429|rate limit|too many requests|quota|usage limit|5.hour|5-hour|weekly|7.day|7-day|monthly|plan limit|usage cap", lowered):
        return "rate_limited"
    if re.search(r"\b(401|403)\b|unauthorized|forbidden|invalid (api )?key|api key (missing|invalid|expired)|token (missing|invalid|expired)|login required|not logged in|permission denied", lowered):
        return "auth_error"
    if re.search(r"network|connection|timeout|timed out|econnrefused|etimedout|enotfound|socket", lowered):
        return "network_error"
    if "stalled_runtime" in lowered or "runtime liveness stalled" in lowered:
        return "stalled_runtime"
    return "task_failed"


def _runtime_from_command(command: list[str]) -> str:
    if len(command) < 2:
        return ""
    target = Path(command[1]).name
    if target == "oc-exec.py":
        return "opencode"
    if target == "cc-exec.py":
        return "claude"
    return ""


def _extract_opencode_failure_metadata(output: str) -> dict[str, str]:
    session_id = ""
    details: list[str] = []
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            details.append(stripped)
            continue
        if not isinstance(payload, dict):
            continue
        session_id = str(payload.get("sessionID") or session_id)
        if payload.get("type") == "error":
            details.extend(_flatten_error_values(payload.get("error")))
        part = payload.get("part")
        if isinstance(part, dict) and part.get("type") == "tool":
            state = part.get("state")
            if isinstance(state, dict) and state.get("status") == "error":
                details.extend(_flatten_error_values(state.get("error")))
    text = "\n".join(part for part in details if part).strip() or str(output or "")
    return {"session_id": session_id, "kind": _classify_runtime_error(text)}


def _extract_claude_failure_metadata(output: str) -> dict[str, str]:
    session_id = ""
    text = str(output or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        session_id = str(payload.get("session_id") or "").strip()
        flattened = _flatten_error_values(payload)
        if flattened:
            text = "\n".join(flattened)
    return {"session_id": session_id, "kind": _classify_runtime_error(text)}


def _session_snapshot(session: RuntimeSession) -> dict[str, Any]:
    return {
        "session_id": str(getattr(session, "id", "") or "").strip() or None,
        "runtime": session.runtime,
        "task_count": session.task_count,
        "created_at": session.created_at,
        "status": session.status,
        "title": session.title,
        "last_heartbeat": getattr(session, "last_heartbeat", None),
        "current_task_id": getattr(session, "current_task_id", None),
        "session_kind": "implementation",
    }


def _append_session_history(
    project_root: Path | str,
    session: RuntimeSession,
    event: str,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = load_session_state(project_root)
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    record = {
        "ts": utc_now_iso(),
        "event": event,
        **_session_snapshot(session),
    }
    if extra:
        record.update(extra)
    history.append(record)
    payload["history"] = history
    save_session_state(project_root, payload)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _wrapper_script(runtime: str) -> Path:
    mapping = {
        "claude": _repo_root() / "scripts" / "cc-exec.py",
        "opencode": _repo_root() / "scripts" / "oc-exec.py",
    }
    try:
        return mapping[runtime]
    except KeyError as exc:
        raise RuntimeErrorResponse(f"unsupported runtime wrapper: {runtime}") from exc


def _runtime_log_file(project_root: Path | str, session: "RuntimeSession") -> Path:
    paths = project_paths(project_root)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    safe_session_id = session.id or "new"
    return paths.logs_dir / f"{session.runtime}-{safe_session_id}-turn-{session.task_count + 1}.log"


def _build_runtime_command(
    project_root: Path | str,
    session: "RuntimeSession",
) -> tuple[list[str], Path]:
    project_dir = project_paths(project_root).project_root
    wrapper = _wrapper_script(session.runtime)
    log_file = _runtime_log_file(project_root, session)
    command = [
        sys.executable,
        str(wrapper),
        "--project-root",
        str(project_dir),
        "--log-file",
        str(log_file),
    ]
    if session.id:
        command.extend(["--session-id", session.id])
    if session.current_task_id:
        command.extend(["--task-id", session.current_task_id])
    if session.title:
        command.extend(["--task-title", session.title])
    if session.runtime == "claude" and session.task_count > 0 and session.id:
        command.append("--resume")
    return command, log_file


def _run(command: list[str], *, cwd: Path, input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        runtime = _runtime_from_command(command)
        output = completed.stdout or ""
        metadata = (
            _extract_opencode_failure_metadata(output)
            if runtime == "opencode"
            else _extract_claude_failure_metadata(output)
            if runtime == "claude"
            else {"session_id": "", "kind": _classify_runtime_error(output)}
        )
        raise RuntimeErrorResponse(
            f"runtime command failed: {' '.join(command)}",
            output,
            session_id=str(metadata.get("session_id") or ""),
            kind=str(metadata.get("kind") or ""),
            returncode=completed.returncode,
        )
    return completed.stdout


def _parse_claude_result(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorResponse(f"claude did not return valid JSON: {exc}", output) from exc
    if not isinstance(payload, dict):
        raise RuntimeErrorResponse("claude returned non-object JSON payload", output)
    return payload


def _parse_opencode_result(output: str) -> dict[str, Any]:
    session_id = ""
    text_parts: list[str] = []
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        events.append(payload)
        session_id = str(payload.get("sessionID") or session_id)
        if payload.get("type") == "text":
            part = payload.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    if not session_id:
        raise RuntimeErrorResponse("opencode did not return a session ID", output)
    return {"session_id": session_id, "result": "\n".join(text_parts).strip(), "events": events}


def create_session(project_root: Path | str, runtime: str, *, title: str = "") -> RuntimeSession:
    runtime_name = runtime.strip().lower() or "claude"
    session_id = str(uuid.uuid4()) if runtime_name == "claude" else ""
    created_at = utc_now_iso()
    session = RuntimeSession(
        id=session_id,
        runtime=runtime_name,
        task_count=0,
        created_at=created_at,
        status="active",
        title=title or runtime_name,
        last_heartbeat=created_at,
        current_task_id=None,
    )
    _append_session_history(project_root, session, "created")
    return session


def current_session(project_root: Path | str) -> RuntimeSession | None:
    payload = load_session_state(project_root)
    active = payload.get("active")
    if not isinstance(active, dict):
        return None
    session_id = str(active.get("id") or "").strip()
    runtime = str(active.get("runtime") or "").strip()
    if not runtime:
        return None
    return RuntimeSession(
        id=session_id,
        runtime=runtime,
        task_count=int(active.get("task_count") or 0),
        created_at=str(active.get("created_at") or utc_now_iso()),
        status=str(active.get("status") or "active"),
        title=str(active.get("title") or runtime),
        last_heartbeat=str(active.get("last_heartbeat") or "").strip() or None,
        current_task_id=str(active.get("current_task_id") or "").strip() or None,
    )


def save_current_session(project_root: Path | str, session: RuntimeSession, *, retire_previous: bool = False) -> None:
    payload = load_session_state(project_root)
    retired = payload.get("retired") if isinstance(payload.get("retired"), list) else []
    if retire_previous and isinstance(payload.get("active"), dict):
        previous = dict(payload["active"])
        previous["status"] = "retired"
        retired.append(previous)
    payload["active"] = {
        "id": session.id,
        "runtime": session.runtime,
        "task_count": session.task_count,
        "created_at": session.created_at,
        "status": session.status,
        "title": session.title,
        "last_heartbeat": session.last_heartbeat or utc_now_iso(),
        "current_task_id": getattr(session, "current_task_id", None),
    }
    payload["retired"] = retired
    save_session_state(project_root, payload)


def start_task_session(project_root: Path | str, runtime: str, *, task_id: str, title: str = "") -> RuntimeSession:
    active = current_session(project_root)
    normalized_task_id = str(task_id or "").strip() or None
    if (
        active
        and active.runtime == runtime
        and active.status == "active"
        and str(active.current_task_id or "").strip() == str(normalized_task_id or "")
    ):
        return active
    if active is not None:
        retire_session(project_root, active)
    session = create_session(project_root, runtime, title=title)
    session.current_task_id = normalized_task_id
    save_current_session(project_root, session)
    _append_session_history(project_root, session, "task_session_started")
    return session


def touch_session(project_root: Path | str, session: RuntimeSession, *, task_id: str | None = None, title: str | None = None) -> None:
    if task_id is not None:
        session.current_task_id = task_id
    if title is not None:
        session.title = title
    session.last_heartbeat = utc_now_iso()
    save_current_session(project_root, session)
    _append_session_history(project_root, session, "attached")


def retire_session(project_root: Path | str, session: RuntimeSession) -> None:
    payload = load_session_state(project_root)
    retired = payload.get("retired") if isinstance(payload.get("retired"), list) else []
    retired.append(
        {
            "id": session.id,
            "runtime": session.runtime,
            "task_count": session.task_count,
            "created_at": session.created_at,
            "status": "retired",
            "title": session.title,
            "last_heartbeat": getattr(session, "last_heartbeat", None),
            "current_task_id": getattr(session, "current_task_id", None),
        }
    )
    payload["retired"] = retired
    active = payload.get("active")
    if isinstance(active, dict):
        active_id = str(active.get("id") or "").strip()
        active_runtime = str(active.get("runtime") or "").strip()
        if active_id == session.id and active_runtime == session.runtime:
            payload["active"] = None
    save_session_state(project_root, payload)
    _append_session_history(project_root, session, "retired")


def session_context_for(session: RuntimeSession, task_title: str, prompt: str) -> str:
    return prompt


def execute_in_session(
    project_root: Path | str,
    session: RuntimeSession,
    prompt: str,
    persist: bool = True,
) -> dict[str, Any]:
    project_dir = project_paths(project_root).project_root
    project_dir.mkdir(parents=True, exist_ok=True)
    rendered_prompt = session_context_for(session, session.title, prompt)
    previous_session_id = str(session.id or "").strip()

    def persist_current_session_state() -> None:
        if persist:
            save_current_session(project_root, session)

    if session.runtime == "claude":
        command, _ = _build_runtime_command(project_root, session)
        output = _run(command, cwd=project_dir, input_text=rendered_prompt)
        payload = _parse_claude_result(output)
        session.id = str(payload.get("session_id") or session.id)
        session.task_count += 1
        session.last_heartbeat = utc_now_iso()
        persist_current_session_state()
        if str(session.id or "").strip() != previous_session_id:
            persist_current_session_state()
            _append_session_history(project_root, session, "session_id_resolved")
        _append_session_history(project_root, session, "turn_completed", extra={"persisted": persist})
        text = str(payload.get("result") or "").strip()
        return {"session_id": session.id, "text": text, "raw": payload}
    if session.runtime == "opencode":
        sync_runtime_support(project_dir, runtime="opencode")
        command, _ = _build_runtime_command(project_root, session)
        output = _run(command, cwd=project_dir, input_text=rendered_prompt)
        payload = _parse_opencode_result(output)
        session.id = str(payload.get("session_id") or session.id)
        session.task_count += 1
        session.last_heartbeat = utc_now_iso()
        persist_current_session_state()
        if str(session.id or "").strip() != previous_session_id:
            persist_current_session_state()
            _append_session_history(project_root, session, "session_id_resolved")
        _append_session_history(project_root, session, "turn_completed", extra={"persisted": persist})
        return {"session_id": session.id, "text": str(payload.get("result") or "").strip(), "raw": payload}
    raise RuntimeErrorResponse(f"unsupported runtime: {session.runtime}")
