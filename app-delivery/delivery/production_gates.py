from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .stack_contracts import PYTHON_REACT_CONTRACT, backend_test_root


BACKEND_TEST_ROOT = backend_test_root(PYTHON_REACT_CONTRACT)


@dataclass(frozen=True)
class ProductionGateSpec:
    key: str
    title: str
    reason: str
    keywords: tuple[str, ...]
    output_tests: tuple[str, ...]
    output_paths: tuple[str, ...]
    path_keywords: tuple[str, ...] = ()


PRODUCTION_GATE_TITLE_PREFIX = "Production Gate:"


REAL_BACKEND_E2E_GATE = ProductionGateSpec(
    key="real-backend-e2e",
    title="Production Gate: Real backend E2E validation",
    reason="Project has both frontend and backend/API surfaces.",
    keywords=("frontend", "browser", "ui", "react", "e2e", "api", "backend"),
    output_tests=(
        "frontend/e2e/real-backend.spec.ts",
        "bash -lc 'test -x scripts/e2e-backend.sh && test -x scripts/seed-backend.sh'",
    ),
    output_paths=(
        "frontend/e2e/real-backend.spec.ts",
        "frontend/playwright.config.ts",
        "scripts/e2e-backend.sh",
        "scripts/seed-backend.sh",
        "docs/reviews/production-gate-real-backend-e2e.md",
    ),
)


SECURITY_GATE = ProductionGateSpec(
    key="security-rbac",
    title="Production Gate: Security and RBAC enforcement",
    reason="Requirements mention roles, permissions, authentication, authorization, or data isolation.",
    keywords=("rbac", "auth", "authentication", "authorization", "security", "permission", "role", "角色", "权限", "认证", "授权", "安全", "数据隔离", "脱敏"),
    output_tests=(f"{BACKEND_TEST_ROOT}/security/",),
    output_paths=(
        f"{PYTHON_REACT_CONTRACT.backend_source_root}/",
        f"{BACKEND_TEST_ROOT}/security/",
        "docs/reviews/production-gate-security-rbac.md",
    ),
)


AGENT_REALITY_GATE = ProductionGateSpec(
    key="agent-reality",
    title="Production Gate: Real agent integration",
    reason="Requirements mention AI agents, orchestration, model gateway, skills, or MCP/tools.",
    keywords=("agent", "ai agent", "orchestrator", "langgraph", "llm", "model gateway", "mcp", "skill", "智能体", "模型网关", "编排", "工具调用"),
    output_tests=(f"{BACKEND_TEST_ROOT}/integration/agents/test_real_agent_inputs.py",),
    output_paths=(
        f"{PYTHON_REACT_CONTRACT.backend_source_root}/",
        f"{PYTHON_REACT_CONTRACT.mock_server_root}/mcp_servers/",
        f"{BACKEND_TEST_ROOT}/integration/agents/test_real_agent_inputs.py",
        "docs/reviews/production-gate-agent-reality.md",
    ),
)


EXECUTION_LOOP_GATE = ProductionGateSpec(
    key="execution-loop",
    title="Production Gate: Approval and execution loop",
    reason="Requirements mention approvals, action dispatch, execution tracking, rollback, or manual override.",
    keywords=("approval", "approve", "execution", "dispatch", "rollback", "manual override", "action", "审批", "执行", "派发", "回滚", "人工覆盖", "动作"),
    output_tests=(f"{BACKEND_TEST_ROOT}/scenarios/test_execution_dispatch_to_external_system.py",),
    output_paths=(
        f"{PYTHON_REACT_CONTRACT.backend_source_root}/",
        f"{BACKEND_TEST_ROOT}/scenarios/test_execution_dispatch_to_external_system.py",
        "docs/reviews/production-gate-execution-loop.md",
    ),
)


ALL_PRODUCTION_GATES = (
    REAL_BACKEND_E2E_GATE,
    SECURITY_GATE,
    AGENT_REALITY_GATE,
    EXECUTION_LOOP_GATE,
)


BUILTIN_TASK_IDS = {"T000", "T001", "T-FRONTEND-API-AUDIT", "T-SYSTEM-AUDIT", "T-FINAL"}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _requirements(project_root: Path | str) -> list[dict[str, Any]]:
    payload = _load_json(Path(project_root).expanduser().resolve() / "docs" / "requirements.json")
    rows = payload.get("requirements") if isinstance(payload.get("requirements"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _requirement_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("id", "title", "summary", "description")).casefold()


def _task_text(task: Any) -> str:
    parts = [
        getattr(task, "id", ""),
        getattr(task, "title", ""),
        " ".join(getattr(task, "requirements", []) or []),
        " ".join(getattr(task, "acceptance_scenarios", []) or []),
        " ".join(getattr(task, "output_paths", []) or []),
        " ".join(getattr(task, "output_tests", []) or []),
    ]
    return " ".join(str(part or "") for part in parts).casefold()


def _is_user_task(task: Any) -> bool:
    return str(getattr(task, "id", "") or "").strip() not in BUILTIN_TASK_IDS


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def _matching_requirement_ids(project_root: Path | str, spec: ProductionGateSpec) -> list[str]:
    result: list[str] = []
    for row in _requirements(project_root):
        req_id = str(row.get("id") or "").strip()
        if req_id and _matches_keywords(_requirement_text(row), spec.keywords):
            result.append(req_id)
    return result[:30]


def _has_frontend_surface(project_root: Path | str, tasks: list[Any]) -> bool:
    project_dir = Path(project_root).expanduser().resolve()
    return (project_dir / "frontend" / "package.json").exists() or any("frontend/" in _task_text(task) for task in tasks if _is_user_task(task))


def _has_backend_surface(project_root: Path | str, tasks: list[Any]) -> bool:
    project_dir = Path(project_root).expanduser().resolve()
    candidate_roots = [path for path in (PYTHON_REACT_CONTRACT.backend_source_root, PYTHON_REACT_CONTRACT.legacy_backend_app_root, PYTHON_REACT_CONTRACT.mock_server_root) if path]
    return any((project_dir / path).exists() for path in candidate_roots) or any(re.search(rf"\bbackend/|\bapi\b|{re.escape(PYTHON_REACT_CONTRACT.mock_server_root)}/", _task_text(task)) for task in tasks if _is_user_task(task))


def _project_text(project_root: Path | str, tasks: list[Any]) -> str:
    req_text = " ".join(_requirement_text(row) for row in _requirements(project_root))
    task_text = " ".join(_task_text(task) for task in tasks if _is_user_task(task))
    return f"{req_text} {task_text}"


def required_production_gates(project_root: Path | str, tasks: list[Any]) -> list[ProductionGateSpec]:
    text = _project_text(project_root, tasks)
    result: list[ProductionGateSpec] = []
    if _has_frontend_surface(project_root, tasks) and _has_backend_surface(project_root, tasks):
        result.append(REAL_BACKEND_E2E_GATE)
    if _has_backend_surface(project_root, tasks) and _matches_keywords(text, SECURITY_GATE.keywords):
        result.append(SECURITY_GATE)
    if _matches_keywords(text, AGENT_REALITY_GATE.keywords):
        result.append(AGENT_REALITY_GATE)
    if _matches_keywords(text, EXECUTION_LOOP_GATE.keywords):
        result.append(EXECUTION_LOOP_GATE)
    return result


def next_production_gate_task_id(existing_ids: set[str], start: int = 900) -> str:
    index = start
    while f"T{index:03d}" in existing_ids:
        index += 1
    return f"T{index:03d}"


def production_gate_task_dict(project_root: Path | str, spec: ProductionGateSpec, *, task_id: str, dependencies: list[str]) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": spec.title,
        "status": "pending",
        "task_kind": "validation",
        "requirements": _matching_requirement_ids(project_root, spec),
        "acceptance_scenarios": [],
        "dependencies": list(dependencies),
        "output_tests": list(spec.output_tests),
        "output_paths": list(spec.output_paths),
        "intent": {
            "objective": spec.reason,
            "done_when": [
                "The validation is backed by executable tests, not only a report.",
                "Any production-path mock, stub, hardcoded, or unauthenticated behavior found by this gate is either fixed or documented as a blocking finding.",
            ],
            "non_goals": ["Do not implement unrelated product features beyond the gate evidence needed for release confidence."],
        },
    }
