from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builtin_tasks import (
    FINAL_VERIFY_TASK_ID,
    FRONTEND_API_AUDIT_OUTPUT_PATHS,
    FRONTEND_API_AUDIT_OUTPUT_TESTS,
    FRONTEND_API_AUDIT_REPORT_PATH,
    FRONTEND_API_AUDIT_TASK_ID,
    PREFINAL_AUDIT_OUTPUT_PATHS,
    PREFINAL_AUDIT_OUTPUT_TESTS,
    PREFINAL_AUDIT_REPORT_PATH,
    PREFINAL_AUDIT_TASK_ID,
    SCAFFOLD_TASK_ID,
    SHARED_FOUNDATION_TASK_ID,
    needs_frontend_api_audit,
)
from .production_gates import PRODUCTION_GATE_TITLE_PREFIX, next_production_gate_task_id, production_gate_task_dict, required_production_gates
from .state import load_gates, load_test_plan, load_work_items, project_paths, save_work_items, utc_now_iso


REQ_ID_RE = re.compile(r"\b((?:REQ|NFR)-\d{3,})\b")
REQ_RANGE_RE = re.compile(
    r"\b(?P<prefix>(?:REQ|NFR)-)(?P<start>\d{3,})\b\s*(?:to|through|thru|[-–—~]|至|到)\s*(?:(?P<end_prefix>(?:REQ|NFR)-))?(?P<end>\d{3,})\b",
    re.IGNORECASE,
)
TASK_ID_RE = re.compile(r"^T\d{3,}$")
VALID_STATUSES = {"pending", "active", "review_pending", "done", "verified", "blocked", "exception", "cancelled"}
NON_CANONICAL_BACKEND_ROOT_DIRS = {
    "api",
    "agents",
    "orchestration",
    "mcp",
    "simulation",
    "models",
    "core",
    "services",
    "actions",
    "infra",
    "knowledge",
}
FOUNDATION_SCOPE_PREFIXES = (
    "backend/",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/src/main.py",
    "backend/src/runtime/",
    "backend/src/tests/",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/src/lib/",
    "frontend/src/styles/",
    "frontend/src/App.tsx",
    "frontend/src/App.test.tsx",
    "frontend/e2e/",
)
SHARED_FOUNDATION_OUTPUT_PATHS = [
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/src/main.py",
    "backend/src/runtime/",
    "backend/src/tests/",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/pnpm-lock.yaml",
    "frontend/yarn.lock",
    "frontend/src/lib/",
    "frontend/src/styles/",
    "frontend/src/App.tsx",
    "frontend/src/App.test.tsx",
    "frontend/e2e/",
]
SHARED_FOUNDATION_OUTPUT_TESTS = [
    "backend/src/tests/test_health.py",
    "backend/src/tests/test_database.py",
    "frontend/src/App.test.tsx",
]
FOUNDATION_TITLE_MARKERS = (
    "基础设施",
    "shared infrastructure",
    "foundation",
    "shared services",
    "shared components",
    "app shell",
    "seed data",
)
SCAFFOLD_OUTPUT_PATHS = [
    "backend/",
    "frontend/",
    "mock-server/",
    "docs/requirements-source.md",
    "docs/project-bootstrap.json",
    "docs/work-items.json",
    "docs/work-items.md",
    "docs/project-structure.md",
    "docs/project-summary.json",
    "docs/project-summary.md",
    "docker-compose.yml",
    ".github/",
    ".githooks/",
    ".gitignore",
    ".worktreeinclude",
    ".env",
    "README.md",
]
FINAL_VERIFY_REQUIREMENT_PREFIXES = (
    "NFR-",
)
RELEASE_ADVISORY_TEST_TYPES = {"accessibility", "performance"}


def _is_final_verify_requirement(requirement_id: str) -> bool:
    normalized = str(requirement_id or "").strip().upper()
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in FINAL_VERIFY_REQUIREMENT_PREFIXES)


def _normalize_reference_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^((?:REQ|NFR|AS)-\d{3,})\b", text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else text


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _normalize_intent(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in ("objective", "journey"):
        value = str(payload.get(key) or "").strip()
        if value:
            normalized[key] = value
    for key in ("done_when", "non_goals"):
        values = [str(value).strip() for value in payload.get(key, []) if str(value).strip()]
        deduped = _dedupe_preserve(values)
        if deduped:
            normalized[key] = deduped
    return normalized


def _normalize_session_ids(values: Any, current_session_id: str | None = None) -> list[str]:
    raw_values = values if isinstance(values, list) else []
    return _dedupe_preserve(
        [
            *[str(value).strip() for value in raw_values if str(value).strip()],
            str(current_session_id or "").strip(),
        ]
    )


def _normalize_contract_path(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if normalized == "backend/app":
        return "backend/src"
    if normalized.startswith("backend/app/"):
        return f"backend/src/{normalized[len('backend/app/') :]}"
    if normalized == "backend/app/":
        return "backend/src/"
    return normalized


def _is_noncanonical_backend_root_path(path: str) -> bool:
    normalized = _strip_current_dir_prefix(path).rstrip("/")
    parts = normalized.split("/")
    return len(parts) >= 2 and parts[0] == "backend" and parts[1] in NON_CANONICAL_BACKEND_ROOT_DIRS


def _is_unsupported_top_level_mcp_server_path(path: str) -> bool:
    normalized = _strip_current_dir_prefix(path).rstrip("/")
    return normalized == "mcp-server" or normalized.startswith("mcp-server/")


def _strip_current_dir_prefix(value: str) -> str:
    normalized = str(value or "").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_foundation_scope_path(path: str) -> bool:
    normalized = _strip_current_dir_prefix(path)
    return bool(normalized) and any(normalized.startswith(prefix) for prefix in FOUNDATION_SCOPE_PREFIXES)


def _normalize_shared_foundation_output_tests(values: list[str]) -> list[str]:
    preserved: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        preserved.append(normalized)
    return _dedupe_preserve([*SHARED_FOUNDATION_OUTPUT_TESTS, *preserved])


def _is_shared_foundation_candidate(task: Task) -> bool:
    if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
        return False
    title = task.title.casefold()
    if any(req.startswith("REQ-") for req in task.requirements):
        return False
    normalized_paths = [str(path).strip() for path in task.output_paths if str(path).strip()]
    if not normalized_paths or not all(_is_foundation_scope_path(path) for path in normalized_paths):
        return False
    if any(marker in title for marker in FOUNDATION_TITLE_MARKERS):
        return True
    return not task.requirements and not task.acceptance_scenarios


def _is_project_root_relative_path(value: str) -> bool:
    normalized = _strip_current_dir_prefix(value)
    if not normalized:
        return False
    path = Path(normalized)
    if path.is_absolute():
        return False
    return all(part not in {"", ".", ".."} for part in path.parts)


def _collapse_shared_foundation_tasks(tasks: list[Task]) -> list[Task]:
    foundation = next((task for task in tasks if task.id == SHARED_FOUNDATION_TASK_ID), None)
    if foundation is None:
        return tasks
    merged_ids: set[str] = set()
    merged_payload = foundation.to_dict()
    for task in tasks:
        if _is_shared_foundation_candidate(task):
            merged_ids.add(task.id)
            merged_payload["requirements"] = _dedupe_preserve([*merged_payload["requirements"], *task.requirements])
            merged_payload["acceptance_scenarios"] = _dedupe_preserve([*merged_payload["acceptance_scenarios"], *task.acceptance_scenarios])
            merged_payload["output_tests"] = _dedupe_preserve([*merged_payload["output_tests"], *task.output_tests])
            merged_payload["output_paths"] = _dedupe_preserve([*merged_payload["output_paths"], *task.output_paths])
    if not merged_ids:
        return tasks
    merged_foundation = Task.from_dict(merged_payload)
    collapsed: list[Task] = []
    for task in tasks:
        if task.id == SHARED_FOUNDATION_TASK_ID:
            collapsed.append(merged_foundation)
            continue
        if task.id in merged_ids:
            continue
        data = task.to_dict()
        data["dependencies"] = _dedupe_preserve(
            [SHARED_FOUNDATION_TASK_ID if dep in merged_ids else dep for dep in task.dependencies]
        )
        collapsed.append(Task.from_dict(data))
    migrated_requirements = merged_foundation.requirements
    if migrated_requirements:
        for index, task in enumerate(collapsed):
            if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
                continue
            data = task.to_dict()
            data["requirements"] = _dedupe_preserve([*migrated_requirements, *task.requirements])
            collapsed[index] = Task.from_dict(data)
            break
    return collapsed


def _normalize_builtin_task_contract(task: "Task") -> "Task":
    data = task.to_dict()
    if task.id == SCAFFOLD_TASK_ID:
        data["output_paths"] = _dedupe_preserve([*SCAFFOLD_OUTPUT_PATHS, *task.output_paths])
        return Task.from_dict(data)
    if task.id == SHARED_FOUNDATION_TASK_ID:
        data["dependencies"] = [SCAFFOLD_TASK_ID]
        data["output_paths"] = _dedupe_preserve([*SHARED_FOUNDATION_OUTPUT_PATHS, *task.output_paths])
        data["output_tests"] = _normalize_shared_foundation_output_tests(task.output_tests)
        return Task.from_dict(data)
    if task.id == FRONTEND_API_AUDIT_TASK_ID:
        data["title"] = "Pre-final frontend/API integration repair pass"
        data["task_kind"] = "audit"
        data["requirements"] = []
        data["acceptance_scenarios"] = []
        data["output_paths"] = _dedupe_preserve([*FRONTEND_API_AUDIT_OUTPUT_PATHS, *task.output_paths])
        data["output_tests"] = _dedupe_preserve([*FRONTEND_API_AUDIT_OUTPUT_TESTS, *task.output_tests])
        return Task.from_dict(data)
    if task.id == PREFINAL_AUDIT_TASK_ID:
        data["title"] = "Pre-final full-system repair pass"
        data["task_kind"] = "audit"
        data["requirements"] = []
        data["acceptance_scenarios"] = []
        data["output_paths"] = _dedupe_preserve([*PREFINAL_AUDIT_OUTPUT_PATHS, *task.output_paths])
        data["output_tests"] = _dedupe_preserve([*PREFINAL_AUDIT_OUTPUT_TESTS, *task.output_tests])
        return Task.from_dict(data)
    return task


@dataclass
class Task:
    id: str
    title: str
    status: str
    requirements: list[str]
    acceptance_scenarios: list[str]
    dependencies: list[str]
    output_tests: list[str]
    output_paths: list[str]
    task_kind: str = "feature"
    intent: dict[str, Any] | None = None
    git_commit: str | None = None
    status_session_id: str | None = None
    session_ids: list[str] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    review_status: str | None = None
    review_artifact: str | None = None
    reviewed_at: str | None = None
    verified_at: str | None = None
    blocked_reason: str | None = None
    attempts: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Task":
        return cls(
            id=str(payload.get("id") or "").strip(),
            title=str(payload.get("title") or "").strip(),
            status=str(payload.get("status") or "pending").strip(),
            requirements=[_normalize_reference_id(value) for value in payload.get("requirements", []) if _normalize_reference_id(value)],
            acceptance_scenarios=[_normalize_reference_id(value) for value in payload.get("acceptance_scenarios", []) if _normalize_reference_id(value)],
            dependencies=[str(value).strip() for value in payload.get("dependencies", []) if str(value).strip()],
            output_tests=[_normalize_contract_path(value) for value in payload.get("output_tests", []) if _normalize_contract_path(value)],
            output_paths=[_normalize_contract_path(value) for value in payload.get("output_paths", []) if _normalize_contract_path(value)],
            task_kind=str(payload.get("task_kind") or "feature").strip() or "feature",
            intent=_normalize_intent(payload.get("intent")) or None,
            git_commit=str(payload.get("git_commit") or "").strip() or None,
            status_session_id=str(payload.get("status_session_id") or "").strip() or None,
            session_ids=_normalize_session_ids(payload.get("session_ids"), str(payload.get("status_session_id") or "").strip() or None),
            started_at=str(payload.get("started_at") or "").strip() or None,
            completed_at=str(payload.get("completed_at") or "").strip() or None,
            review_status=str(payload.get("review_status") or "").strip() or None,
            review_artifact=str(payload.get("review_artifact") or "").strip() or None,
            reviewed_at=str(payload.get("reviewed_at") or "").strip() or None,
            verified_at=str(payload.get("verified_at") or "").strip() or None,
            blocked_reason=str(payload.get("blocked_reason") or "").strip() or None,
            attempts=int(payload.get("attempts") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "task_kind": self.task_kind,
            "git_commit": self.git_commit,
            "status_session_id": self.status_session_id,
            "session_ids": self.session_ids or [],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "review_status": self.review_status,
            "review_artifact": self.review_artifact,
            "reviewed_at": self.reviewed_at,
            "verified_at": self.verified_at,
            "requirements": self.requirements,
            "acceptance_scenarios": self.acceptance_scenarios,
            "dependencies": self.dependencies,
            "output_tests": self.output_tests,
            "output_paths": self.output_paths,
            "intent": self.intent or None,
            "blocked_reason": self.blocked_reason,
            "attempts": self.attempts,
        }


def referenced_req_ids(text: str, known_ids: set[str] | None = None) -> list[str]:
    events: list[tuple[int, list[str]]] = []
    range_spans: list[tuple[int, int]] = []

    for match in REQ_RANGE_RE.finditer(text):
        prefix = match.group("prefix")
        end_prefix = match.group("end_prefix") or prefix
        if end_prefix != prefix:
            continue
        start_text = match.group("start")
        end_text = match.group("end")
        start = int(start_text)
        end = int(end_text)
        if end < start or end - start > 500:
            continue
        width = max(len(start_text), len(end_text))
        expanded: list[str] = []
        for value in range(start, end + 1):
            req_id = f"{prefix}{value:0{width}d}"
            if known_ids is not None and req_id not in known_ids:
                continue
            expanded.append(req_id)
        if expanded:
            events.append((match.start(), expanded))
            range_spans.append((match.start(), match.end()))

    for match in REQ_ID_RE.finditer(text):
        if any(start <= match.start() < end for start, end in range_spans):
            continue
        req_id = match.group(1)
        if known_ids is not None and req_id not in known_ids:
            continue
        events.append((match.start(), [req_id]))

    events.sort(key=lambda row: row[0])
    seen: set[str] = set()
    ordered: list[str] = []
    for _, values in events:
        for req_id in values:
            if req_id in seen:
                continue
            seen.add(req_id)
            ordered.append(req_id)
    return ordered


def all_tasks(project_root: Path | str) -> list[Task]:
    payload = load_work_items(project_root)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    return [_normalize_builtin_task_contract(Task.from_dict(item)) for item in items if isinstance(item, dict)]


def save_tasks(project_root: Path | str, tasks: list[Task]) -> None:
    payload = load_work_items(project_root)
    payload["items"] = [_normalize_builtin_task_contract(task).to_dict() for task in tasks]
    save_work_items(project_root, payload)


def index_tasks(tasks: list[Task]) -> dict[str, Task]:
    return {task.id: task for task in tasks}


def next_generated_task_id(tasks: list[Task], *, minimum: int = 2) -> str:
    next_index = max(2, int(minimum))
    for task in tasks:
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
            continue
        if not TASK_ID_RE.match(task.id):
            continue
        try:
            next_index = max(next_index, int(task.id[1:]) + 1)
        except ValueError:
            continue
    return f"T{next_index:03d}"


def _normalized_dependency_key(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _task_dependency_aliases(task: Task) -> set[str]:
    aliases: set[str] = {task.id, task.title}
    title = task.title.strip()
    shortened = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
    if shortened:
        aliases.add(shortened)
    for separator in (" — ", " - ", ": "):
        prefix = f"{task.id}{separator}"
        if title.startswith(prefix):
            suffix = title[len(prefix) :].strip()
            if suffix:
                aliases.add(suffix)
                normalized_suffix = re.sub(r"\s*\([^()]*\)\s*$", "", suffix).strip()
                if normalized_suffix:
                    aliases.add(f"{task.id}{separator}{normalized_suffix}")
    return {alias for alias in aliases if alias}


def pick_next_task(tasks: list[Task]) -> Task | None:
    by_id = index_tasks(tasks)
    ready: list[Task] = []
    for task in tasks:
        if task.status != "pending":
            continue
        dependencies_met = True
        for dependency_id in task.dependencies:
            dependency = by_id.get(dependency_id)
            if dependency is None:
                dependencies_met = False
                break
            if dependency.status == "blocked":
                dependencies_met = False
                break
            if dependency.status != "verified":
                dependencies_met = False
                break
        if dependencies_met:
            ready.append(task)
    if ready:
        return ready[0]
    return None


def mark_task(tasks: list[Task], task_id: str, status: str, **updates: Any) -> list[Task]:
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown task status: {status}")
    result: list[Task] = []
    updated = False
    for task in tasks:
        if task.id != task_id:
            result.append(task)
            continue
        data = task.to_dict()
        data["status"] = status
        data.update(updates)
        data["session_ids"] = _normalize_session_ids(data.get("session_ids"), data.get("status_session_id"))
        if status == "active" and not data.get("started_at"):
            data["started_at"] = utc_now_iso()
        if status == "done" and not data.get("completed_at"):
            data["completed_at"] = utc_now_iso()
        if status == "verified" and not data.get("verified_at"):
            data["verified_at"] = utc_now_iso()
        result.append(Task.from_dict(data))
        updated = True
    if not updated:
        raise KeyError(f"task not found: {task_id}")
    return result


def reset_task(tasks: list[Task], task_id: str, *, blocked_reason: str | None = None) -> list[Task]:
    result: list[Task] = []
    updated = False
    for task in tasks:
        if task.id != task_id:
            result.append(task)
            continue
        data = task.to_dict()
        data.update(
            {
                "status": "pending",
                "git_commit": None,
                "status_session_id": None,
                "started_at": None,
                "completed_at": None,
                "review_status": None,
                "review_artifact": None,
                "reviewed_at": None,
                "verified_at": None,
                "blocked_reason": blocked_reason,
                "attempts": 0,
            }
        )
        result.append(Task.from_dict(data))
        updated = True
    if not updated:
        raise KeyError(f"task not found: {task_id}")
    return result


def all_requirements(requirements_payload: dict[str, Any]) -> set[str]:
    requirements = requirements_payload.get("requirements") if isinstance(requirements_payload.get("requirements"), list) else []
    ids: set[str] = set()
    for row in requirements:
        if not isinstance(row, dict):
            continue
        req_id = str(row.get("id") or "").strip()
        if req_id:
            ids.add(req_id)
    return ids


def load_requirement_ids(project_root: Path | str) -> set[str]:
    requirements_path = project_paths(project_root).docs_dir / "requirements.json"
    if not requirements_path.exists():
        return set()
    payload = json.loads(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"requirements.json must be a JSON object: {requirements_path}")
    return all_requirements(payload)


def _validate_requirement_task_coverage(project_root: Path | str, tasks: list[Task]) -> None:
    requirement_ids = load_requirement_ids(project_root)
    if not requirement_ids:
        return
    covered: set[str] = set()
    for task in tasks:
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
            continue
        covered.update(task.requirements)
    covered.update(req_id for req_id in requirement_ids if _is_final_verify_requirement(req_id))
    uncovered = sorted(req_id for req_id in requirement_ids if req_id not in covered)
    if uncovered:
        raise ValueError(f"task decomposition left requirements uncovered: {', '.join(uncovered)}")


def _validate_dependency_graph(tasks: list[Task]) -> None:
    by_id = index_tasks(tasks)
    unresolved: list[str] = []
    for task in tasks:
        for dependency_id in task.dependencies:
            if dependency_id not in by_id:
                unresolved.append(f"{task.id}->{dependency_id}")
    if unresolved:
        raise ValueError(f"task decomposition produced unresolved dependencies: {', '.join(unresolved)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str, stack: list[str]) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            cycle = " -> ".join(stack + [task_id])
            raise ValueError(f"task decomposition produced a dependency cycle: {cycle}")
        visiting.add(task_id)
        stack.append(task_id)
        for dependency_id in by_id[task_id].dependencies:
            visit(dependency_id, stack)
        stack.pop()
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id, [])


def _validate_task_shape(tasks: list[Task]) -> None:
    oversized: list[str] = []
    for task in tasks:
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
            continue
        invalid_mock_paths = [
            path
            for path in [*task.output_paths, *task.output_tests]
            if path == "companion/mock-server" or path.startswith("companion/mock-server/")
        ]
        if invalid_mock_paths:
            oversized.append(
                f"{task.id} uses non-canonical mock-server paths: {', '.join(invalid_mock_paths)}; use project-root mock-server/..."
            )
        invalid_backend_paths = [path for path in [*task.output_paths, *task.output_tests] if _is_noncanonical_backend_root_path(path)]
        if invalid_backend_paths:
            oversized.append(
                f"{task.id} uses backend-root package paths not supported by python-react: {', '.join(invalid_backend_paths)}; use backend/src/..."
            )
        invalid_mcp_server_paths = [path for path in [*task.output_paths, *task.output_tests] if _is_unsupported_top_level_mcp_server_path(path)]
        if invalid_mcp_server_paths:
            oversized.append(
                f"{task.id} uses top-level mcp-server paths not supported by python-react: {', '.join(invalid_mcp_server_paths)}; use backend/src/... for production MCP code or mock-server/... for simulated tool services"
            )
        if len(task.output_paths) > 20 or len(task.output_tests) > 10:
            oversized.append(
                f"{task.id} (paths={len(task.output_paths)}, tests={len(task.output_tests)}; expected roughly paths<=20 tests<=10)"
            )
        if task.acceptance_scenarios and not task.output_tests:
            oversized.append(f"{task.id} (acceptance scenarios declared without output tests)")
    if oversized:
        raise ValueError("task decomposition produced oversized or under-specified tasks: " + ", ".join(oversized))


def lint_task_contract(task: Task) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
        return {"errors": errors, "warnings": warnings}

    if not task.output_paths:
        errors.append(f"{task.id} has no declared output_paths")
    if not task.output_tests:
        errors.append(f"{task.id} has no declared output_tests")

    invalid_output_paths = [path for path in task.output_paths if not _is_project_root_relative_path(path)]
    if invalid_output_paths:
        errors.append(
            f"{task.id} output_paths must be project-root-relative (for example backend/... or frontend/...): {', '.join(invalid_output_paths)}"
        )

    invalid_output_tests = [path for path in task.output_tests if "/" in str(path) and not _is_project_root_relative_path(path)]
    if invalid_output_tests:
        errors.append(
            f"{task.id} file-based output_tests must be project-root-relative (for example backend/... or frontend/...): {', '.join(invalid_output_tests)}"
        )

    if task.acceptance_scenarios and not task.output_tests:
        errors.append(f"{task.id} declares acceptance scenarios but no output_tests")

    has_frontend_paths = any(path.startswith("frontend/") for path in task.output_paths)
    has_frontend_tests = any(path.startswith("frontend/") for path in task.output_tests)
    if has_frontend_paths and not has_frontend_tests:
        warnings.append(f"{task.id} changes frontend paths but declares no frontend tests")

    has_backend_paths = any(path.startswith("backend/") for path in task.output_paths)
    has_backend_tests = any(path.startswith("backend/") for path in task.output_tests)
    if has_backend_paths and not has_backend_tests:
        warnings.append(f"{task.id} changes backend paths but declares no backend tests")

    return {"errors": errors, "warnings": warnings}


def lint_task_contracts(tasks: list[Task]) -> dict[str, dict[str, list[str]]]:
    return {task.id: lint_task_contract(task) for task in tasks}


def check_requirements_coverage(project_root: Path | str, requirements_payload: dict[str, Any]) -> dict[str, Any]:
    tasks = all_tasks(project_root)
    covered: set[str] = set()
    all_ids = all_requirements(requirements_payload)
    for task in tasks:
        if task.status != "verified":
            continue
        covered.update(task.requirements)
    covered.update(req_id for req_id in all_ids if _is_final_verify_requirement(req_id))
    uncovered = sorted(req_id for req_id in all_ids if req_id not in covered)
    return {
        "total": len(all_ids),
        "covered": len(covered),
        "uncovered": uncovered,
        "all_covered": not uncovered,
    }


def check_test_type_coverage(project_root: Path | str) -> list[tuple[str, str]]:
    plan = load_test_plan(project_root)
    from .state import load_test_results
    from .verify import infer_test_types

    results_payload = load_test_results(project_root)
    results = results_payload.get("results") if isinstance(results_payload.get("results"), list) else []
    gates_payload = load_gates(project_root)
    gates = gates_payload.get("gates") if isinstance(gates_payload.get("gates"), list) else []
    tasks = all_tasks(project_root)
    missing: list[tuple[str, str]] = []
    missing_set: set[tuple[str, str]] = set()

    def verified_gate_has_type(requirement_id: str, test_type: str) -> bool:
        for gate in gates:
            if not isinstance(gate, dict):
                continue
            if str(gate.get("status") or "").strip() != "verified":
                continue
            scope_requirements = {str(value).strip() for value in gate.get("scope_requirements", []) if str(value).strip()}
            observed_test_types = {str(value).strip() for value in gate.get("observed_test_types", []) if str(value).strip()}
            if requirement_id in scope_requirements and test_type in observed_test_types:
                return True
        return False

    def record_missing(reference_id: str, test_type: str) -> None:
        normalized = (str(reference_id or "").strip(), str(test_type or "").strip())
        if not normalized[0] or not normalized[1] or normalized in missing_set:
            return
        missing_set.add(normalized)
        missing.append(normalized)

    for row in plan.get("coverage", []):
        if not isinstance(row, dict):
            continue
        requirement_id = str(row.get("requirement_id") or "").strip()
        if not requirement_id:
            continue
        test_types = [str(value).strip() for value in row.get("test_types", []) if str(value).strip()]
        for test_type in test_types:
            if test_type == "review":
                matched = any(
                    task.status == "verified"
                    and str(task.review_status or "").strip().casefold() == "pass"
                    and requirement_id in task.requirements
                    for task in tasks
                )
                if not matched:
                    record_missing(requirement_id, test_type)
                continue
            if requirement_id.startswith("NFR-"):
                matched = any(
                    isinstance(result, dict)
                    and bool(result.get("passed"))
                    and test_type in result.get("test_types", [])
                    for result in results
                )
                if not matched:
                    record_missing(requirement_id, test_type)
                continue
            matched = any(
                isinstance(result, dict)
                and bool(result.get("passed"))
                and requirement_id in result.get("requirement_ids", [])
                and test_type in result.get("test_types", [])
                for result in results
            )
            if not matched:
                matched = verified_gate_has_type(requirement_id, test_type)
            if not matched and test_type in RELEASE_ADVISORY_TEST_TYPES:
                continue
            if not matched:
                record_missing(requirement_id, test_type)

    for task in tasks:
        if task.status != "verified":
            continue
        if not task.acceptance_scenarios:
            continue
        if not any(path.startswith("frontend/") for path in task.output_paths):
            continue

        has_browser_evidence = any(
            isinstance(result, dict)
            and bool(result.get("passed"))
            and str(result.get("task_id") or "").strip() == task.id
            and any(test_type in {"browser", "e2e"} for test_type in result.get("test_types", []))
            for result in results
        )
        if has_browser_evidence:
            continue

        explicit_downstream_browser_owner = any(
            candidate.task_kind == "validation"
            and candidate.id != task.id
            and bool(set(candidate.acceptance_scenarios).intersection(task.acceptance_scenarios))
            and any(test_type in {"browser", "e2e"} for test_type in infer_test_types(candidate.output_tests))
            for candidate in tasks
        )
        if explicit_downstream_browser_owner:
            continue

        for requirement_id in task.requirements:
            record_missing(requirement_id, "browser")
    return missing


def decompose_tasks(
    project_root: Path | str,
    items: list[dict[str, Any]],
    *,
    include_shared_foundation: bool,
) -> dict[str, Any]:
    normalized_items: list[Task] = []
    normalized_items.append(
        Task(
            id=SCAFFOLD_TASK_ID,
            title="Scaffold",
            status="pending",
            requirements=[],
            acceptance_scenarios=[],
            dependencies=[],
            output_tests=[],
            output_paths=list(SCAFFOLD_OUTPUT_PATHS),
        )
    )
    next_index = 2
    if include_shared_foundation:
        normalized_items.append(
            Task(
                id=SHARED_FOUNDATION_TASK_ID,
                title="Shared foundation",
                status="pending",
                requirements=[],
                acceptance_scenarios=[],
                dependencies=[SCAFFOLD_TASK_ID],
                output_tests=[],
                output_paths=[
                    *SHARED_FOUNDATION_OUTPUT_PATHS,
                ],
                intent={
                    "objective": "Create the minimal shared backend/frontend foundation and dependency manifest updates required before feature slices can run.",
                    "journey": "Later feature tasks start from a working FastAPI/React scaffold with runtime settings, health checks, lockfiles, and shared test harnesses.",
                    "done_when": [
                        "Backend and frontend manifests and lockfiles are consistent for packages used by foundation code implemented in this task",
                        "Backend health, database, and runtime smoke tests for the foundation pass",
                        "Frontend app-shell smoke test passes when frontend foundation files are in scope",
                    ],
                    "non_goals": [
                        "Do not implement domain workflows, agent graphs, mock contracts, or user-facing product features",
                        "Do not preinstall project-wide technology packages unless this task implements or imports them",
                    ],
                },
            )
        )
        next_index = 2
    task_ids: set[str] = {task.id for task in normalized_items}
    previous_id = SHARED_FOUNDATION_TASK_ID if include_shared_foundation else SCAFFOLD_TASK_ID
    raw_dependency_map: dict[str, list[str]] = {}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        task_id = str(raw_item.get("id") or "").strip()
        if task_id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}:
            continue
        if not TASK_ID_RE.match(task_id) or task_id in task_ids or task_id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID}:
            task_id = f"T{next_index:03d}"
        next_index += 1
        dependencies = [str(value).strip() for value in raw_item.get("dependencies", []) if str(value).strip()]
        if not dependencies:
            dependencies = [SHARED_FOUNDATION_TASK_ID if include_shared_foundation else SCAFFOLD_TASK_ID]
        raw_dependency_map[task_id] = dependencies
        task = Task.from_dict(
            {
                "id": task_id,
                "title": raw_item.get("title") or f"Task {task_id}",
                "status": "pending",
                "task_kind": raw_item.get("task_kind") or "feature",
                "requirements": raw_item.get("requirements", []),
                "acceptance_scenarios": raw_item.get("acceptance_scenarios", []),
                "dependencies": dependencies,
                "output_tests": raw_item.get("output_tests", []),
                "output_paths": raw_item.get("output_paths", []),
                "intent": raw_item.get("intent") or {},
            }
        )
        normalized_items.append(task)
        task_ids.add(task_id)
        previous_id = task_id

    aliases: dict[str, str] = {}
    for task in normalized_items:
        for alias in _task_dependency_aliases(task):
            aliases[alias] = task.id
            aliases[_normalized_dependency_key(alias)] = task.id

    resolved_items: list[Task] = []
    for task in normalized_items:
        resolved: list[str] = []
        seen: set[str] = set()
        for dependency in raw_dependency_map.get(task.id, task.dependencies):
            resolved_id = aliases.get(dependency) or aliases.get(_normalized_dependency_key(dependency))
            if not resolved_id or resolved_id == task.id or resolved_id in seen:
                continue
            seen.add(resolved_id)
            resolved.append(resolved_id)
        if task.id not in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID} and task.id != FINAL_VERIFY_TASK_ID and not resolved:
            resolved = list(task.dependencies)
        data = task.to_dict()
        data["dependencies"] = resolved
        resolved_items.append(Task.from_dict(data))
    normalized_items = resolved_items
    if include_shared_foundation:
        normalized_items = _collapse_shared_foundation_tasks(normalized_items)
    frontend_api_audit_needed = needs_frontend_api_audit(project_root, normalized_items)
    if frontend_api_audit_needed:
        frontend_api_audit_dependencies = [task.id for task in normalized_items if task.id not in {FRONTEND_API_AUDIT_TASK_ID, PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}]
        normalized_items.append(
            Task(
                id=FRONTEND_API_AUDIT_TASK_ID,
                    title="Pre-final frontend/API integration repair pass",
                status="pending",
                requirements=[],
                acceptance_scenarios=[],
                dependencies=frontend_api_audit_dependencies,
                output_tests=list(FRONTEND_API_AUDIT_OUTPUT_TESTS),
                output_paths=list(FRONTEND_API_AUDIT_OUTPUT_PATHS),
                task_kind="audit",
            )
        )
    production_gate_dependencies = [task.id for task in normalized_items if task.id not in {PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}]
    existing_ids = {task.id for task in normalized_items}
    for gate_spec in required_production_gates(project_root, normalized_items):
        gate_task_id = next_production_gate_task_id(existing_ids)
        existing_ids.add(gate_task_id)
        normalized_items.append(
            Task.from_dict(
                production_gate_task_dict(
                    project_root,
                    gate_spec,
                    task_id=gate_task_id,
                    dependencies=production_gate_dependencies,
                )
            )
        )
    audit_dependencies = [task.id for task in normalized_items if task.id not in {PREFINAL_AUDIT_TASK_ID, FINAL_VERIFY_TASK_ID}]
    normalized_items.append(
        Task(
            id=PREFINAL_AUDIT_TASK_ID,
            title="Pre-final full-system repair pass",
            status="pending",
            requirements=[],
            acceptance_scenarios=[],
            dependencies=audit_dependencies,
            output_tests=list(PREFINAL_AUDIT_OUTPUT_TESTS),
            output_paths=list(PREFINAL_AUDIT_OUTPUT_PATHS),
            task_kind="audit",
        )
    )
    _validate_dependency_graph(normalized_items)
    _validate_task_shape(normalized_items)
    _validate_requirement_task_coverage(project_root, normalized_items)

    final_dependencies = [PREFINAL_AUDIT_TASK_ID]
    final_requirements = sorted(req_id for req_id in load_requirement_ids(project_root) if _is_final_verify_requirement(req_id))
    normalized_items.append(
        Task(
            id=FINAL_VERIFY_TASK_ID,
            title="最终验证",
            status="pending",
            requirements=final_requirements,
            acceptance_scenarios=[],
            dependencies=final_dependencies,
            output_tests=[],
            output_paths=["docs/release-evidence.md", "docs/reviews/final-review.md"],
        )
    )
    _validate_dependency_graph(normalized_items)
    payload = load_work_items(project_root)
    payload["items"] = [task.to_dict() for task in normalized_items]
    return payload


def parse_task_json(text: str) -> list[dict[str, Any]]:
    payload = json.loads(text)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        raw_items = payload["items"]
    else:
        raise ValueError("task decomposition output must be a JSON object with items[]")
    items: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, dict):
            items.append(item)
    return items
