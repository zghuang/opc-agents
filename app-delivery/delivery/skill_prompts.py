from __future__ import annotations

from pathlib import Path


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
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return text