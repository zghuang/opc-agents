from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import ensure_runtime_dirs, load_json, utc_now_iso


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def token_usage_log_path(project_root: Path | str) -> Path:
    return ensure_runtime_dirs(project_root).token_usage_log


def _step_finish_usage_candidate(payload: dict[str, Any]) -> dict[str, Any] | None:
    if str(payload.get("type") or "") != "step_finish":
        return None
    part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
    tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
    if not tokens:
        return None
    return {
        "prompt_tokens": safe_int(tokens.get("input")),
        "completion_tokens": safe_int(tokens.get("output")),
        "reasoning_tokens": safe_int(tokens.get("reasoning")),
        "cache_read_tokens": safe_int((tokens.get("cache") or {}).get("read") if isinstance(tokens.get("cache"), dict) else None),
        "cache_write_tokens": safe_int((tokens.get("cache") or {}).get("write") if isinstance(tokens.get("cache"), dict) else None),
        "total_tokens": safe_int(tokens.get("total")),
        "estimated_cost_usd": part.get("cost"),
    }


def extract_opencode_usage(output_text: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_total = -1
    for raw_line in str(output_text or "").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        candidate = _step_finish_usage_candidate(payload)
        if candidate is None:
            continue
        total = safe_int(candidate.get("total_tokens"))
        if total >= best_total:
            best_total = total
            best = candidate
    if best is None or best_total <= 0:
        return None
    return best


def append_token_usage_record(
    project_root: Path | str,
    *,
    runtime: str,
    task_id: str,
    task_title: str,
    session_id: str,
    usage: dict[str, Any],
    log_file: str | None = None,
) -> None:
    path = token_usage_log_path(project_root)
    record = {
        "ts": utc_now_iso(),
        "runtime": runtime,
        "task_id": str(task_id or "").strip() or None,
        "task_title": str(task_title or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "log_file": str(log_file or "").strip() or None,
        "prompt_tokens": safe_int(usage.get("prompt_tokens")) or None,
        "completion_tokens": safe_int(usage.get("completion_tokens")) or None,
        "reasoning_tokens": safe_int(usage.get("reasoning_tokens")) or None,
        "cache_read_tokens": safe_int(usage.get("cache_read_tokens")) or None,
        "cache_write_tokens": safe_int(usage.get("cache_write_tokens")) or None,
        "total_tokens": safe_int(usage.get("total_tokens")) or None,
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def execution_token_records_for_project(project_root: Path | str) -> list[dict[str, Any]]:
    path = token_usage_log_path(project_root)
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def execution_token_records_for_task(project_root: Path | str, task_id: str) -> list[dict[str, Any]]:
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return []
    return [
        record
        for record in execution_token_records_for_project(project_root)
        if str(record.get("task_id") or "").strip() == normalized_task_id
    ]


__all__ = [
    "append_token_usage_record",
    "execution_token_records_for_project",
    "execution_token_records_for_task",
    "extract_opencode_usage",
    "safe_int",
    "token_usage_log_path",
]