from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .release_assessment import RELEASE_ASSESSMENT_PATH
from .system_gap_ledger import SYSTEM_GAP_LEDGER_PATH

SCAFFOLD_TASK_ID = "T000"
SHARED_FOUNDATION_TASK_ID = "T001"
FRONTEND_API_AUDIT_TASK_ID = "T-FRONTEND-API-AUDIT"
PREFINAL_AUDIT_TASK_ID = "T-SYSTEM-AUDIT"
FINAL_VERIFY_TASK_ID = "T-FINAL"

FRONTEND_API_AUDIT_REPORT_PATH = "docs/reviews/frontend-api-integration-audit.md"
PREFINAL_AUDIT_REPORT_PATH = "docs/reviews/system-audit.md"
PREFINAL_AUDIT_TITLE = "System Gap Fix"
FINAL_VERIFY_OUTPUT_PATHS = [
    "docs/release-evidence.md",
    "docs/reviews/final-review.md",
    RELEASE_ASSESSMENT_PATH,
]

FRONTEND_API_AUDIT_REQUIRED_SECTIONS = [
    "# Frontend API Integration Audit",
    "## Audit Scope",
    "## API Surface Mapping",
    "## Mocked Browser Test Assessment",
    "## Real Backend E2E Readiness",
    "## Test Data / Environment Readiness",
    "## Fixed Issues",
    "## Remaining Gaps / Blockers",
    "## Validation Summary",
    "## Final Recommendation",
]

PREFINAL_AUDIT_REQUIRED_SECTIONS = [
    "# System Gap Fix",
    "## Scan Scope",
    "## Gap Summary",
    "## Fixed Gaps",
    "## Remaining Gaps / Blockers",
    "## Requirement Gap Matrix",
    "## Validation Summary",
    "## Changed Files",
    "## Final Recommendation",
]

PREFINAL_AUDIT_OUTPUT_PATHS = [
    "backend/",
    "frontend/",
    "mock-server/",
    "scripts/",
    "docker-compose.yml",
    "README.md",
    PREFINAL_AUDIT_REPORT_PATH,
    SYSTEM_GAP_LEDGER_PATH,
]
PREFINAL_AUDIT_OUTPUT_TESTS = [
    "bash -lc 'test -s docs/reviews/system-audit.md && test -s docs/reviews/system-gap-fix.json'",
]

FRONTEND_API_AUDIT_OUTPUT_PATHS = [
    "frontend/",
    "backend/",
    "mock-server/",
    "docker-compose.yml",
    "docs/test-plan.json",
    FRONTEND_API_AUDIT_REPORT_PATH,
]
FRONTEND_API_AUDIT_OUTPUT_TESTS = [
    "bash -lc 'test -s docs/reviews/frontend-api-integration-audit.md && grep -q \"## API Surface Mapping\" docs/reviews/frontend-api-integration-audit.md && grep -q \"## Real Backend E2E Readiness\" docs/reviews/frontend-api-integration-audit.md && grep -q \"## Mocked Browser Test Assessment\" docs/reviews/frontend-api-integration-audit.md'",
]

BUILTIN_TASK_IDS = {
    SCAFFOLD_TASK_ID,
    SHARED_FOUNDATION_TASK_ID,
    FRONTEND_API_AUDIT_TASK_ID,
    PREFINAL_AUDIT_TASK_ID,
    FINAL_VERIFY_TASK_ID,
}
FULL_STACK_FRONTEND_API_STACKS = {"python-react"}


def is_builtin_task_id(task_id: str) -> bool:
    return str(task_id or "").strip() in BUILTIN_TASK_IDS


def has_frontend_surface(project_root: Path | str, tasks: list[Any]) -> bool:
    project_dir = Path(project_root).expanduser().resolve()
    if (project_dir / "frontend" / "package.json").exists():
        return True
    meta_path = project_dir / "docs" / "architecture-meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        if bool(meta.get("ui_required")):
            return True
    return any(_task_has_frontend_surface(task) for task in tasks if not is_builtin_task_id(getattr(task, "id", "")))


def has_backend_api_surface(project_root: Path | str, tasks: list[Any]) -> bool:
    project_dir = Path(project_root).expanduser().resolve()
    backend_markers = (
        project_dir / "backend" / "src" / "api",
        project_dir / "backend" / "app" / "api",
        project_dir / "backend" / "routes",
        project_dir / "mock-server",
    )
    if any(path.exists() for path in backend_markers):
        return True
    test_plan = _load_test_plan(project_dir)
    if _test_plan_mentions_api_backend(test_plan):
        return True
    return any(_task_has_backend_api_surface(task) for task in tasks if not is_builtin_task_id(getattr(task, "id", "")))


def needs_frontend_api_audit(project_root: Path | str, tasks: list[Any]) -> bool:
    if project_stack(project_root) in FULL_STACK_FRONTEND_API_STACKS:
        return True
    return has_frontend_surface(project_root, tasks) and has_backend_api_surface(project_root, tasks)


def project_stack(project_root: Path | str) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    path = project_dir / "docs" / "project-bootstrap.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("stack") or "").strip().casefold()


def _load_test_plan(project_dir: Path) -> dict[str, Any]:
    path = project_dir / "docs" / "test-plan.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _test_plan_mentions_api_backend(payload: dict[str, Any]) -> bool:
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), list) else []
    for row in coverage:
        if not isinstance(row, dict):
            continue
        test_types = {str(value).strip().casefold() for value in row.get("test_types", []) if str(value).strip()}
        suite = str(row.get("suite") or "").casefold()
        if test_types.intersection({"api", "integration", "contract"}) or any(marker in suite for marker in ("api", "integration", "backend", "contract")):
            return True
    return False


def _task_values(task: Any) -> list[str]:
    values: list[str] = []
    for attr in ("output_paths", "output_tests"):
        raw = getattr(task, attr, [])
        if isinstance(raw, list):
            values.extend(str(value).strip() for value in raw if str(value).strip())
    return values


def _task_has_frontend_surface(task: Any) -> bool:
    values = _task_values(task)
    return any(value.startswith("frontend/") for value in values) or any(
        value.casefold() in {"npm run e2e", "npm run test", "pnpm run e2e", "pnpm run test", "yarn e2e", "yarn test"}
        for value in values
    )


def _task_has_backend_api_surface(task: Any) -> bool:
    values = _task_values(task)
    backend_prefixes = ("backend/", "mock-server/")
    if any(value.startswith(backend_prefixes) for value in values):
        return True
    return any(value.casefold().startswith("curl ") and "/api/" in value.casefold() for value in values)
