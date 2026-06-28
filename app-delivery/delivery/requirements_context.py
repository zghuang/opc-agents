from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_requirement_details(project_root: Path | str) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    requirements_path = Path(project_root).expanduser().resolve() / "docs" / "requirements.json"
    if not requirements_path.exists():
        return {}, {}
    try:
        payload = json.loads(requirements_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    requirements = payload.get("requirements") if isinstance(payload.get("requirements"), list) else []
    scenarios = payload.get("acceptance_scenarios") if isinstance(payload.get("acceptance_scenarios"), list) else []
    requirement_map = {
        str(row.get("id") or "").strip(): {
            "title": str(row.get("title") or "").strip(),
            "summary": str(row.get("summary") or "").strip(),
        }
        for row in requirements
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    scenario_map = {
        str(row.get("id") or "").strip(): row
        for row in scenarios
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    return requirement_map, scenario_map


def format_requirement_context(project_root: Path | str, requirement_ids: list[str]) -> list[str]:
    requirement_map, _ = load_requirement_details(project_root)
    lines: list[str] = []
    for requirement_id in requirement_ids:
        row = requirement_map.get(requirement_id)
        if row is None:
            lines.append(f"- {requirement_id}")
            continue
        title = row.get("title") or ""
        summary = row.get("summary") or ""
        detail = " — ".join(part for part in [title, summary] if part)
        lines.append(f"- {requirement_id}: {detail}" if detail else f"- {requirement_id}")
    return lines


def format_acceptance_context(project_root: Path | str, acceptance_ids: list[str]) -> list[str]:
    _, scenario_map = load_requirement_details(project_root)
    lines: list[str] = []
    for acceptance_id in acceptance_ids:
        row = scenario_map.get(acceptance_id)
        if row is None:
            lines.append(f"- {acceptance_id}")
            continue
        title = str(row.get("title") or "").strip()
        summary = str(row.get("summary") or "").strip()
        detail = " — ".join(part for part in [title, summary] if part)
        lines.append(f"- {acceptance_id}: {detail}" if detail else f"- {acceptance_id}")
    return lines