from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .task import FINAL_VERIFY_TASK_ID, Task, next_generated_task_id


ENVIRONMENT_REPAIR_TASK_PREFIX = "Validation Environment Repair Bundle"


@dataclass(frozen=True)
class EnvironmentRepairPlan:
    tasks: list[Task]
    repair_task_id: str
    repair_candidates: list[str]


def result_environment_failures(result: Any) -> list[Any]:
    failures = getattr(result, "failures", []) or []
    matched: list[Any] = []
    for failure in failures:
        kind = str(getattr(failure, "failure_kind", "") or "").strip()
        message = str(getattr(failure, "message", "") or "").strip().casefold()
        if kind == "environment_not_ready" or message.startswith("test environment not ready"):
            matched.append(failure)
    return matched


def has_environment_failures(results: list[Any]) -> bool:
    return any(result_environment_failures(result) for result in results if not getattr(result, "passed", False))


def environment_failed_test_specs(results: list[Any]) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for result in results:
        if getattr(result, "passed", False):
            continue
        for failure in result_environment_failures(result):
            normalized = str(getattr(failure, "test", "") or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            specs.append(normalized)
    return specs[:10]


def default_python_react_environment_repair_scope_paths(project_root: Path | str) -> list[str]:
    """Heuristic repair scope for the default python-react stack.

    This is intentionally named as a stack-specific/default heuristic. Other
    stack adapters can provide their own repair scope planner without changing
    the final verification control flow.
    """
    project_dir = Path(project_root).expanduser().resolve()
    candidates = [
        "docker-compose.yml",
        "docker/",
        "backend/.env",
        "backend/src/main.py",
        "backend/src/runtime/",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/pnpm-lock.yaml",
        "frontend/yarn.lock",
        "frontend/playwright.config.ts",
        "frontend/vite.config.ts",
        "frontend/src/api/",
        "mock-server/",
        "scripts/",
        "docs/reviews/final-repair-report.md",
    ]
    existing = [path for path in candidates if (project_dir / path.rstrip("/")).exists()]
    return existing or ["docker-compose.yml", "docs/reviews/final-repair-report.md"]


def build_environment_repair_plan(
    project_root: Path | str,
    tasks: list[Task],
    *,
    results: list[Any],
) -> EnvironmentRepairPlan:
    current = next(
        (
            task
            for task in tasks
            if task.task_kind == "repair" and str(task.title or "").startswith(ENVIRONMENT_REPAIR_TASK_PREFIX)
        ),
        None,
    )
    if current is not None and current.status in {"verified", "exception"}:
        return EnvironmentRepairPlan(tasks=tasks, repair_task_id=current.id, repair_candidates=[current.id])

    repair_task_id = current.id if current is not None else next_generated_task_id(tasks)
    output_tests = environment_failed_test_specs(results)
    if not output_tests:
        output_tests = _failed_test_specs(results)
    output_paths = default_python_react_environment_repair_scope_paths(project_root)
    dependencies = [
        task.id
        for task in tasks
        if task.id not in {FINAL_VERIFY_TASK_ID, repair_task_id}
        and task.status == "verified"
    ]
    replacement = Task.from_dict(
        {
            "id": repair_task_id,
            "title": ENVIRONMENT_REPAIR_TASK_PREFIX,
            "status": current.status if current is not None and current.status != "verified" else "pending",
            "task_kind": "repair",
            "requirements": [],
            "acceptance_scenarios": [],
            "dependencies": dependencies,
            "output_tests": output_tests,
            "output_paths": output_paths,
            "blocked_reason": "final verification could not run because the validation environment or app services were not ready",
            "attempts": current.attempts if current is not None and current.status != "verified" else 0,
        }
    )

    updated: list[Task] = []
    replaced = False
    for task in tasks:
        if task.id == repair_task_id:
            updated.append(replacement)
            replaced = True
            continue
        updated.append(task)
    if not replaced:
        insert_at = len(updated)
        for index, task in enumerate(updated):
            if task.id == FINAL_VERIFY_TASK_ID:
                insert_at = index
                break
        updated.insert(insert_at, replacement)
    return EnvironmentRepairPlan(tasks=updated, repair_task_id=repair_task_id, repair_candidates=[repair_task_id])


def _failed_test_specs(results: list[Any]) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()
    for result in results:
        if getattr(result, "passed", False):
            continue
        for test_file in getattr(result, "test_files", []) or []:
            normalized = str(test_file or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            specs.append(normalized)
    return specs[:10]
