from __future__ import annotations

import json
from pathlib import Path

from .runtime_config import load_project_metadata
from .stack_contracts import project_stack_guidance_markdown, project_stack_id, stack_guidance_markdown


SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

PROMPT_SOURCES = {
    "spec-review.md": SKILLS_DIR / "spec-review" / "references" / "stage-contract.md",
    "arch-design.md": SKILLS_DIR / "arch-design" / "references" / "stage-contract.md",
    "ui-design.md": SKILLS_DIR / "ui-design" / "references" / "stage-contract.md",
    "context-sync.md": SKILLS_DIR / "project-context-sync" / "references" / "stage-contract.md",
    "decompose.md": SKILLS_DIR / "task-decompose" / "references" / "stage-contract.md",
}


def render_skill_prompt(name: str, **values: str) -> str:
    path = PROMPT_SOURCES.get(name)
    if path is None:
        raise FileNotFoundError(f"skill prompt mapping not found for: {name}")
    if not path.exists():
        raise FileNotFoundError(f"skill prompt source not found: {path}")
    text = path.read_text(encoding="utf-8").strip() + "\n"
    if "dependency_hints_json" not in values and "{{dependency_hints_json}}" in text:
        metadata = load_project_metadata(str(values.get("project") or "")) if values.get("project") else {}
        dependency_hints = metadata.get("dependency_hints") if isinstance(metadata.get("dependency_hints"), list) else []
        values["dependency_hints_json"] = json.dumps(dependency_hints, indent=2, ensure_ascii=False)
    if "stack_guidance_md" not in values and "{{stack_guidance_md}}" in text:
        project = str(values.get("project") or "").strip()
        values["stack_guidance_md"] = project_stack_guidance_markdown(project) if project else stack_guidance_markdown("python-react")
    if "stack_id" not in values and "{{stack_id}}" in text:
        project = str(values.get("project") or "").strip()
        values["stack_id"] = project_stack_id(project) if project else "python-react"
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return text