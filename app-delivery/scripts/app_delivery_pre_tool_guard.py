#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


LEDGER_FILES = {
    "docs/work-items.json",
    "docs/work-items.md",
    "docs/project-summary.json",
    "docs/project-summary.md",
    "docs/test-results.json",
}

FRAMEWORK_REVIEW_ARTIFACT_FILES = {
    "docs/reviews/final-review.md",
    "docs/reviews/final-repair-report.md",
}

FRAMEWORK_REVIEW_ARTIFACT_PREFIXES = (
    "docs/reviews/code-review-",
    "docs/reviews/exception-report-",
)

FRAMEWORK_TASK_TEST_REPORT_RE = re.compile(r"^docs/reviews/test-report-T[A-Za-z0-9_-]+\.md$")

RUNTIME_STATE_PREFIXES = (
    ".app-delivery-runtime/",
    "app-delivery-runtime/",
)

PROTECTED_OPENCODE_FILES = {
    ".opencode/opc-guard.js",
    ".opencode/opencode.json",
    ".opencode/package.json",
}

TERMINAL_MUTATION = "__TERMINAL_MUTATION__"
COMMAND_CHAIN_SPLIT = re.compile(r"\s*(?:&&|\|\||;)\s*")
ENV_ASSIGNMENT_PREFIX = re.compile(r"^(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+))\s+)+")
ENV_ONLY_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s*)+$")

SETUP_SEGMENT_PREFIXES = (
    "cd ",
    "source ",
    ". ",
    "export ",
    "set ",
)

READONLY_COMMAND_PREFIXES = (
    "echo ",
    "cat ",
    "sed ",
    "awk ",
    "grep ",
    "rg ",
    "ls",
    "find ",
    "pwd",
    "which ",
    "command -v ",
    "test ",
    "[ ",
    "git status",
    "git diff",
    "git log",
    "git show",
    "head ",
    "tail ",
    "stat ",
    "wc ",
    "ps ",
    "pgrep ",
    "python --version",
    "python3 --version",
    "python -c ",
    "python3 -c ",
    "uv --version",
    "uv run python -c ",
    "uv run python3 -c ",
    "pytest ",
    "python -m pytest",
    "python3 -m pytest",
    "uv run pytest",
    "uv run python -m pytest",
    "uv run python3 -m pytest",
    "node --version",
    "npm --version",
    "npm ls",
    "npm run --list",
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run typecheck",
    "npm run build",
    "npm run e2e",
    "vitest",
    "pnpm test",
    "pnpm lint",
    "pnpm typecheck",
    "pnpm build",
    "pnpm e2e",
    "pnpm exec vitest",
    "pnpm exec playwright test",
    "pnpm exec eslint",
    "pnpm exec tsc",
    "playwright test",
    "./node_modules/.bin/tsc",
    "node_modules/.bin/tsc",
    "./node_modules/.bin/eslint",
    "node_modules/.bin/eslint",
    "./node_modules/.bin/vitest",
    "node_modules/.bin/vitest",
    "./node_modules/.bin/playwright",
    "node_modules/.bin/playwright",
    "npx playwright test",
    "npx vitest",
    "npx eslint",
    "npx tsc",
    "npx -y playwright test",
    "npx -y vitest",
    "npx -y eslint",
    "npx -y tsc",
    "ruff ",
    "mypy ",
    "tsc ",
)

READONLY_PIPE_FILTER_PREFIXES = (
    "echo",
    "tail",
    "head",
    "grep",
    "rg",
    "cat",
    "awk",
    "wc",
    "sed -n",
)

TEST_COMMAND_MARKERS = (
    "playwright test",
    "npx playwright test",
    "npx -y playwright test",
    "pnpm exec playwright test",
    "npm run e2e",
    "pnpm e2e",
    "npm test",
    "npm run test",
    "pnpm test",
    "vitest",
    "npx vitest",
    "pnpm exec vitest",
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "uv run pytest",
    "uv run python -m pytest",
    "uv run python3 -m pytest",
)

TEST_PIPE_FILTER_RE = re.compile(r"\|\s*(?:tail|head|grep|rg|sed|awk)\b", re.IGNORECASE)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _process_alive(pid: int) -> bool:
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


def _normalize_scope_path(path: str) -> str:
    normalized = str(path or "").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _path_overlaps(scope: str, candidate: str) -> bool:
    normalized_scope = _normalize_scope_path(scope)
    normalized_candidate = _normalize_scope_path(candidate)
    if not normalized_scope or not normalized_candidate:
        return False
    return (
        normalized_candidate == normalized_scope
        or normalized_candidate.startswith(normalized_scope + "/")
        or normalized_scope.startswith(normalized_candidate + "/")
    )


def _current_task_id(project_root: Path) -> str:
    active_tasks_dir = project_root / ".app-delivery-runtime" / "active-tasks"
    live_records: list[dict[str, Any]] = []
    if active_tasks_dir.exists():
        for path in sorted(active_tasks_dir.glob("*.json")):
            payload = _load_json(path)
            try:
                pid = int(payload.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if _process_alive(pid):
                live_records.append(payload)
        if live_records:
            live_records.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
            return str(live_records[0].get("task_id") or "").strip()
    session_state = _load_json(project_root / ".app-delivery-runtime" / "session-state.json")
    active = session_state.get("active") if isinstance(session_state.get("active"), dict) else {}
    return str(active.get("current_task_id") or "").strip()


def _task_delete_scopes(project_root: Path) -> list[str]:
    task_id = _current_task_id(project_root)
    if not task_id:
        return []
    payload = _load_json(project_root / "docs" / "work-items.json")
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict) or str(item.get("id") or "").strip() != task_id:
            continue
        scopes: list[str] = []
        for raw_path in [*(item.get("output_paths") or []), *(item.get("output_tests") or [])]:
            path_text = str(raw_path or "").strip()
            if not path_text or " " in path_text:
                continue
            normalized = _normalize_scope_path(path_text)
            if not normalized:
                continue
            path = Path(normalized)
            if str(raw_path).strip().endswith("/"):
                scopes.append(normalized)
                continue
            scopes.append(str(path.parent).rstrip("/"))
            scopes.append(normalized)
        return [scope for scope in scopes if scope]
    return []


def _task_output_scopes(project_root: Path) -> list[str]:
    task_id = _current_task_id(project_root)
    if not task_id:
        return []
    payload = _load_json(project_root / "docs" / "work-items.json")
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict) or str(item.get("id") or "").strip() != task_id:
            continue
        scopes = [_normalize_scope_path(str(raw_path or "").strip()) for raw_path in item.get("output_paths", []) or []]
        return [scope for scope in scopes if scope]
    return []


def _segment_working_dir(segment: str, working_dir: Path) -> Path:
    normalized = segment.strip()
    if not normalized.lower().startswith("cd "):
        return working_dir
    target = normalized[3:].strip()
    if not target:
        return working_dir
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = working_dir / path
    return path.resolve(strict=False)


def _allowed_delete_shell_segment(segment: str, *, working_dir: Path, project_root: Path) -> bool:
    normalized = ENV_ASSIGNMENT_PREFIX.sub("", segment).strip()
    if not normalized:
        return False
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        return False
    if not tokens:
        return False
    command = tokens[0]
    if command not in {"rm", "rmdir"}:
        return False
    targets: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        if any(char in token for char in "*?["):
            return False
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = working_dir / candidate
        relative = _relative_to_project(candidate, project_root)
        if not relative:
            return False
        targets.append(relative)
    if not targets:
        return False
    scopes = _task_delete_scopes(project_root)
    if not scopes:
        return False
    return all(any(_path_overlaps(scope, target) for scope in scopes) for target in targets)


def _allowed_package_manager_segment(segment: str, *, working_dir: Path, project_root: Path) -> bool:
    normalized = ENV_ASSIGNMENT_PREFIX.sub("", segment).strip()
    if not normalized:
        return False
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        return False
    if not tokens:
        return False

    scopes = _task_output_scopes(project_root)
    if not scopes:
        return False

    relative_cwd = _relative_to_project(working_dir, project_root) or ""
    is_frontend = relative_cwd == "frontend" or relative_cwd.startswith("frontend/")
    is_backend = relative_cwd == "backend" or relative_cwd.startswith("backend/")
    frontend_candidates = ["frontend/package.json", "frontend/package-lock.json", "frontend/pnpm-lock.yaml", "frontend/yarn.lock"]
    backend_candidates = ["backend/pyproject.toml", "backend/uv.lock"]
    frontend_in_scope = any(any(_path_overlaps(scope, candidate) for scope in scopes) for candidate in frontend_candidates)
    backend_in_scope = any(any(_path_overlaps(scope, candidate) for scope in scopes) for candidate in backend_candidates)

    if tokens[0] == "npm" and len(tokens) >= 2 and tokens[1] in {"install", "ci"}:
        if not (is_frontend or frontend_in_scope):
            return False
        return frontend_in_scope

    if tokens[0] == "pnpm" and len(tokens) >= 2 and tokens[1] == "install":
        if not (is_frontend or frontend_in_scope):
            return False
        return frontend_in_scope

    if tokens[0] == "yarn" and (len(tokens) == 1 or (len(tokens) >= 2 and tokens[1] == "install")):
        if not (is_frontend or frontend_in_scope):
            return False
        return frontend_in_scope

    if tokens[0] == "uv" and len(tokens) >= 2 and tokens[1] in {"lock", "sync", "add"}:
        if not (is_backend or backend_in_scope):
            return False
        return backend_in_scope

    return False


def _load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _emit_block(reason: str) -> int:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def _project_root_from_cwd(cwd: Path) -> Path | None:
    current = cwd.resolve(strict=False)
    for candidate in [current, *current.parents]:
        if (candidate / "docs" / "work-items.json").exists() and (candidate / ".app-delivery-runtime").exists():
            return candidate
    return None


def _relative_to_project(path: Path, project_root: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(project_root.resolve(strict=False)).as_posix()
    except Exception:
        return None


def _is_framework_review_artifact(path: str) -> bool:
    normalized = _normalize_scope_path(path)
    if normalized in FRAMEWORK_REVIEW_ARTIFACT_FILES:
        return True
    if any(normalized.startswith(prefix) and normalized.endswith(".md") for prefix in FRAMEWORK_REVIEW_ARTIFACT_PREFIXES):
        return True
    return bool(FRAMEWORK_TASK_TEST_REPORT_RE.fullmatch(normalized))


def _extract_paths(tool_name: str, tool_input: dict[str, Any], cwd: Path, project_root: Path) -> list[str]:
    if tool_name in {"write", "edit"}:
        raw_path = str(tool_input.get("filePath") or tool_input.get("path") or "").strip()
        if not raw_path:
            return []
        target = Path(raw_path)
        if not target.is_absolute():
            target = cwd / target
        relative = _relative_to_project(target, project_root)
        return [relative] if relative else []
    if tool_name == "bash":
        command = str(tool_input.get("command") or "")
        if not command.strip():
            return []
        return [TERMINAL_MUTATION]
    return []


def _is_allowed_terminal_command(command: str, *, cwd: Path, project_root: Path) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return True
    segments = [segment.strip() for segment in COMMAND_CHAIN_SPLIT.split(normalized) if segment.strip()]
    actionable_seen = False
    working_dir = cwd
    for segment in segments:
        lowered = segment.lower()
        if lowered.startswith("cd "):
            working_dir = _segment_working_dir(segment, working_dir)
            continue
        if lowered.startswith(SETUP_SEGMENT_PREFIXES) or ENV_ONLY_SEGMENT.fullmatch(segment):
            continue
        if _is_readonly_shell_segment(segment):
            actionable_seen = True
            continue
        if _allowed_package_manager_segment(segment, working_dir=working_dir, project_root=project_root):
            actionable_seen = True
            continue
        if _allowed_delete_shell_segment(segment, working_dir=working_dir, project_root=project_root):
            actionable_seen = True
            continue
        return False
    return actionable_seen


def _masks_test_exit_code_with_pipe(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().split()).lower()
    if "|" not in normalized:
        return False
    if "pipefail" in normalized or "pipestatus" in normalized:
        return False
    if not any(marker in normalized for marker in TEST_COMMAND_MARKERS):
        return False
    return bool(TEST_PIPE_FILTER_RE.search(normalized))


def _is_readonly_shell_segment(segment: str) -> bool:
    normalized = ENV_ASSIGNMENT_PREFIX.sub("", segment).strip().lower()
    if not normalized:
        return True
    normalized = normalized.replace("2>&1", " ").replace("1>/dev/null", " ").replace("2>/dev/null", " ")
    if ">" in normalized:
        return False
    parts = [part.strip() for part in normalized.split("|") if part.strip()]
    if not parts:
        return False
    if not parts[0].startswith(READONLY_COMMAND_PREFIXES):
        return False
    return all(part.startswith(READONLY_PIPE_FILTER_PREFIXES) for part in parts[1:])


def main() -> int:
    payload = _load_payload()
    tool_name = str(payload.get("tool_name") or "").strip().lower()
    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser()
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool_name == "bash":
        raw_workdir = str(tool_input.get("workdir") or "").strip()
        if raw_workdir:
            workdir = Path(raw_workdir).expanduser()
            cwd = workdir if workdir.is_absolute() else (cwd / workdir)

    project_root = _project_root_from_cwd(cwd)
    if project_root is None:
        return 0

    rel_paths = _extract_paths(tool_name, tool_input, cwd, project_root)
    if not rel_paths:
        return 0

    if any(path in LEDGER_FILES for path in rel_paths if path != TERMINAL_MUTATION):
        return _emit_block(
            "app-delivery pre-tool guard blocked a direct ledger edit. docs/work-items.json, docs/work-items.md, and summary/test ledgers are machine-owned and may only be changed through app-delivery commands."
        )

    if any(path != TERMINAL_MUTATION and _is_framework_review_artifact(path) for path in rel_paths):
        return _emit_block(
            "app-delivery pre-tool guard blocked a direct framework review artifact edit. code-review, exception, final-review, and task-id test-report artifacts are framework-owned and may only be changed through app-delivery review/verification commands."
        )

    if any(
        path != TERMINAL_MUTATION and (path in PROTECTED_OPENCODE_FILES or any(path.startswith(prefix) for prefix in RUNTIME_STATE_PREFIXES))
        for path in rel_paths
    ):
        return _emit_block(
            "app-delivery pre-tool guard blocked a direct runtime-state edit. .app-delivery-runtime and framework-owned .opencode support files may only be changed through app-delivery commands."
        )

    if tool_name == "bash":
        command = str(tool_input.get("command") or "")
        if _masks_test_exit_code_with_pipe(command):
            return _emit_block(
                "app-delivery pre-tool guard blocked a test command piped through an output filter without pipefail; this masks Playwright/pytest/vitest failures as exit 0. Run the test unpiped, or prefix the command with `set -o pipefail;` before piping to tail/head/grep."
            )
        if not _is_allowed_terminal_command(command, cwd=cwd, project_root=project_root):
            return _emit_block(
                "app-delivery pre-tool guard blocked a mutating shell command in the canonical project worktree. Use the framework-owned task loop instead of direct terminal mutations."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())