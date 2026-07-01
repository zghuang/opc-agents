from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .bootstrap import archive_requirements_source, save_project_dependency_hints
from .errors import DeliveryError
from .runtime_config import load_project_metadata, load_project_runtime, root_context_filename
from .scaffold import write_project_structure_snapshot
from .gates import sync_gates
from .state import ensure_runtime_dirs, load_architecture_meta, project_paths, save_architecture_meta, save_test_plan, save_work_items, utc_now_iso
from .task import Task, decompose_tasks, lint_task_contracts, parse_task_json


STAGE_INPUT_DIRNAME = "stage-inputs"

STAGE_METADATA: dict[str, dict[str, Any]] = {
    "spec-review": {
        "missing_code": "requirements_missing",
        "required_skill": "spec-review",
        "expected_outputs": ["docs/requirements.json", "docs/clarification-needed.md"],
    },
    "arch-design": {
        "missing_code": "architecture_missing",
        "required_skill": "arch-design",
        "expected_outputs": ["docs/architecture.md", "docs/shared-components.md", "docs/architecture-meta.json"],
    },
    "ui-design": {
        "missing_code": "ui_design_missing",
        "required_skill": "ui-design",
        "expected_outputs": ["docs/ui/template-selection.md", "docs/ui/design-system.md", "docs/ui/page-archetypes.md", "docs/ui/states.md"],
    },
    "project-context-sync": {
        "missing_code": "context_missing",
        "required_skill": "project-context-sync",
        "expected_outputs": ["docs/test-plan.json"],
    },
    "task-decompose": {
        "missing_code": "work_items_missing",
        "required_skill": "task-decompose",
        "expected_outputs": ["docs/work-items.json", "docs/work-items.md"],
    },
}

STAGE_CLI_COMMANDS: dict[str, str] = {
    "spec-review": "spec-review",
    "arch-design": "arch-design",
    "ui-design": "ui-design",
    "project-context-sync": "context-sync",
    "task-decompose": "decompose",
}

MODULE_ARCHITECTURE_HEADING_RE = re.compile(r"^##+\s+(?:\d+\.\s*)?(?:Module Architecture|模块架构)\s*$", re.IGNORECASE | re.MULTILINE)
FENCED_BLOCK_RE = re.compile(r"```(?:text|plaintext|txt)?\n(?P<body>.*?)\n```", re.DOTALL | re.IGNORECASE)
NON_CANONICAL_BACKEND_PACKAGE_RE = re.compile(
    r"\bbackend/(?:api|agents|orchestration|mcp|simulation|models|core|services|actions|infra|knowledge)(?:/|\b)",
    re.IGNORECASE,
)
NON_CANONICAL_BACKEND_TREE_CHILD_RE = re.compile(
    r"^(?:│   |    )(?:├──|└──)\s*(?:api|agents|orchestration|mcp|simulation|models|core|services|actions|infra|knowledge)/",
    re.IGNORECASE | re.MULTILINE,
)
UNSUPPORTED_TOP_LEVEL_MCP_SERVER_RE = re.compile(r"\bmcp-server(?:/|\b)", re.IGNORECASE)
TREE_FILE_ENTRY_RE = re.compile(
    r"^(?:[│ ]+)?(?:├──|└──)\s*(?P<name>[^\s#]+)(?:\s|$)",
    re.IGNORECASE | re.MULTILINE,
)
ALLOWED_TREE_PLACEHOLDER_FILES = {"__init__.py", ".gitkeep"}
ALLOWED_SCAFFOLD_TREE_FILES = {
    ".env",
    ".env.example",
    ".gitignore",
    ".worktreeinclude",
    "Dockerfile",
    "README.md",
    "docker-compose.yml",
    "docker-compose.yaml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "poetry.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "tsconfig.json",
    "vite.config.ts",
    "next.config.js",
    "next.config.mjs",
    "tailwind.config.js",
    "postcss.config.js",
    "playwright.config.ts",
}
IMPLEMENTATION_TREE_FILE_RE = re.compile(
    r"\.(?:py|pyi|ipynb|ts|tsx|js|jsx|mjs|cjs|java|kt|kts|scala|go|rs|cs|fs|vb|cpp|cc|cxx|c|h|hpp|swift|rb|php|dart|sql|graphql|proto)$",
    re.IGNORECASE,
)
MAX_MODULE_TREE_IMPLEMENTATION_FILE_ENTRIES = 20


def _looks_like_adr_doc(path: str, content: str) -> bool:
    path_text = str(path or "").strip().casefold()
    content_text = str(content or "").strip().casefold()
    filename = Path(path_text).name
    if filename.startswith("adr-") or "/adr" in path_text or "/adrs" in path_text:
        return True
    if re.search(r"^#\s*adr[-\s:]", content_text, flags=re.MULTILINE):
        return True
    if "## decision" in content_text and "## consequences" in content_text and "## context" in content_text:
        return True
    return False


def _invalid_module_docs(payload: dict[str, Any]) -> list[str]:
    invalid: list[str] = []
    for index, row in enumerate(payload.get("modules", []) or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip()
        content = str(row.get("content") or "").strip()
        if _looks_like_adr_doc(path, content):
            invalid.append(path or f"modules[{index}]")
    return invalid


def _dependency_hint_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", str(name or "").casefold()) if len(token) >= 3]


def _architecture_missing_dependency_hints(project_root: Path | str, payload: dict[str, Any]) -> list[str]:
    metadata = load_project_metadata(project_root)
    dependency_hints = metadata.get("dependency_hints") if isinstance(metadata.get("dependency_hints"), list) else []
    if not dependency_hints:
        return []
    docs = [str(payload.get("architecture_md") or ""), str(payload.get("shared_components_md") or "")]
    for collection_name in ["modules", "adrs"]:
        for row in payload.get(collection_name, []) or []:
            if isinstance(row, dict):
                docs.append(str(row.get("content") or ""))
                docs.append(str(row.get("path") or ""))
    haystack = "\n".join(docs).casefold()
    missing: list[str] = []
    for hint in dependency_hints:
        if not isinstance(hint, dict):
            continue
        name = str(hint.get("name") or "").strip()
        if not name:
            continue
        if name.casefold() in haystack:
            continue
        tokens = _dependency_hint_tokens(name)
        if tokens and all(token in haystack for token in tokens):
            continue
        missing.append(name)
    return missing

def _runtime_context_baseline(runtime: str) -> str:
    context_name = "CLAUDE.md" if runtime == "claude" else "AGENTS.md"
    return "\n".join(
        [
            "## Runtime Baseline",
            "",
            f"Read {context_name} before each implementation or review session.",
            "",
            "### Session Startup Order",
            "1. Read this file for project-level rules that always apply.",
            "2. Read only the relevant source docs for the current task.",
            "",
            "### Source of Truth",
            "- docs/requirements-source.md is the raw requirements record and the business source of truth.",
            "- docs/requirements.json, docs/work-items.json, and docs/test-plan.json are framework artifacts for planning and traceability; do not treat them as the business source of truth.",
            "- docs/architecture.md, docs/shared-components.md, docs/modules/*, docs/adr/*, and docs/ui/* (when present) are the design reference docs.",
            "- docs/work-items.md is a readable view only; do not treat it as the canonical ledger.",
            "",
            "### Universal Rules",
            "- Prefer the smallest maintainable change that fully solves the task.",
            "- Do not turn a straightforward fix into unnecessary abstraction or indirection.",
            "- Extract shared code only when the current task or clear duplication justifies it.",
            "- Prefer task-local ownership, but if completing the current requirement or fixing regressions needs adjacent shared/support-file edits, make the smallest necessary change and validate it in the current task instead of stopping on scope alone.",
            "- When a task depends on an unfamiliar or recently released library/framework, do not guess APIs from memory. First confirm usage from the official docs, GitHub repository, or the installed package source before coding against it.",
            "- Keep controlled records, approvals, audit trails, and permission boundaries intact.",
            "- Treat AI outputs as assistive, never authoritative.",
            "",
            "### Dependency And Environment Rules",
            "- Backend Python commands must use the project environment from `backend/`, preferably `uv run ...` or an explicit `backend/.venv/bin/python` path. Do not run project code with shell-default `python`, `python3`, or `pip`.",
            "- Add backend dependencies through the project manifest workflow so `backend/pyproject.toml` and `backend/uv.lock` stay in sync; do not install packages into a global or user Python environment.",
            "- Frontend commands must use the package manager implied by the project lockfile when one exists, and dependency changes must keep `frontend/package.json` and the active lockfile in sync.",
            "- Add dependencies only when they are required for the current task or a documented project-wide foundation; do not preinstall speculative packages for future tasks.",
            "",
            "### Validation Rules",
            "- Do not delete or weaken tests to make the task pass.",
            "- Run the task-declared validation and required review/QA steps before considering the task complete.",
            "- Prefer reusing project-shared test services and local app processes; do not rebuild app Docker images or perform destructive environment resets unless explicitly required.",
            "- Before treating a browser/e2e or integration failure as a code bug, first confirm the required services and health probes are actually ready.",
            "- Do not run git history-changing commands; the framework owns commits.",
        ]
    )


def _merge_runtime_context(runtime: str, project_specific_text: str) -> str:
    project_text = str(project_specific_text or "").strip()
    baseline = _runtime_context_baseline(runtime).strip()
    if not project_text:
        return baseline + "\n"
    if "## Runtime Baseline" in project_text:
        return project_text.rstrip() + "\n"
    return project_text.rstrip() + "\n\n---\n\n" + baseline + "\n"


def _validate_architecture_markdown(input_path: Path, architecture_md: str) -> None:
    text = str(architecture_md or "")
    match = MODULE_ARCHITECTURE_HEADING_RE.search(text)
    if not match:
        raise _shape_error(
            "arch-design",
            input_path,
            "arch-design architecture_md must include a 'Module Architecture' section with the intended repository/module structure",
        )
    section = text[match.end() :]
    fenced = FENCED_BLOCK_RE.search(section)
    if not fenced:
        raise _shape_error(
            "arch-design",
            input_path,
            "arch-design Module Architecture section must include a fenced code block showing the repository/module tree",
        )
    tree_body = str(fenced.group("body") or "")
    invalid_backend_paths = sorted(set(NON_CANONICAL_BACKEND_PACKAGE_RE.findall(tree_body)))
    invalid_backend_tree_children = NON_CANONICAL_BACKEND_TREE_CHILD_RE.findall(tree_body)
    if invalid_backend_paths or invalid_backend_tree_children:
        raise _shape_error(
            "arch-design",
            input_path,
            "python-react architecture must place backend Python packages under backend/src/...; found unsupported backend package roots in the Module Architecture tree",
        )
    if UNSUPPORTED_TOP_LEVEL_MCP_SERVER_RE.search(tree_body):
        raise _shape_error(
            "arch-design",
            input_path,
            "python-react architecture must use the selected stack's canonical service roots; found an unsupported extra service root in the Module Architecture tree",
        )
    implementation_files = []
    for match in TREE_FILE_ENTRY_RE.finditer(tree_body):
        name = match.group("name").rstrip("/")
        if name in ALLOWED_TREE_PLACEHOLDER_FILES or name in ALLOWED_SCAFFOLD_TREE_FILES:
            continue
        if IMPLEMENTATION_TREE_FILE_RE.search(name):
            implementation_files.append(name)
    if len(implementation_files) > MAX_MODULE_TREE_IMPLEMENTATION_FILE_ENTRIES:
        raise _shape_error(
            "arch-design",
            input_path,
            "arch-design Module Architecture tree must be a scaffold skeleton, not an implementation file inventory; too many implementation source files were listed",
            details={
                "max_implementation_file_entries": MAX_MODULE_TREE_IMPLEMENTATION_FILE_ENTRIES,
                "implementation_file_entry_count": len(implementation_files),
                "implementation_file_examples": implementation_files[:30],
                "ignored_scaffold_files": sorted(ALLOWED_SCAFFOLD_TREE_FILES),
            },
        )


def _persist_stage_input(project_root: Path | str, stage_name: str, payload: Any) -> Path:
    target = stage_input_path(project_root, stage_name)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def stage_input_path(project_root: Path | str, stage_name: str) -> Path:
    paths = ensure_runtime_dirs(project_root)
    stage_dir = paths.runtime_dir / STAGE_INPUT_DIRNAME
    stage_dir.mkdir(parents=True, exist_ok=True)
    return stage_dir / f"{stage_name}.json"


def stage_import_command(project_root: Path | str, stage_name: str, input_path: Path | None = None) -> str:
    stage_file = input_path or stage_input_path(project_root, stage_name)
    command_name = STAGE_CLI_COMMANDS.get(stage_name, stage_name)
    return f"${{OPC_HOME:-$HOME/opc}}/bin/app-delivery {command_name} --project {Path(project_root).expanduser().resolve()} --input {stage_file}"


def stage_missing_error(stage_name: str, project_root: Path | str, *, requirements_path: str | None = None) -> DeliveryError:
    metadata = STAGE_METADATA[stage_name]
    input_path = stage_input_path(project_root, stage_name)
    details = {
        "stage": stage_name,
        "required_skill": metadata["required_skill"],
        "project": str(Path(project_root).expanduser().resolve()),
        "expected_input_path": str(input_path),
        "expected_output_files": [str(Path(project_root).expanduser().resolve() / value) for value in metadata["expected_outputs"]],
        "import_command": stage_import_command(project_root, stage_name, input_path),
    }
    if requirements_path:
        details["requirements_path"] = str(Path(requirements_path).expanduser().resolve())
    return DeliveryError(
        code=str(metadata["missing_code"]),
        message=f"required stage artifacts are missing; run the {metadata['required_skill']} stage skill first",
        exit_code=2,
        details=details,
        suggested_action=f"Use the {metadata['required_skill']} stage skill to create {input_path.name}, then import it with: {details['import_command']}",
    )


def _shape_error(stage_name: str, input_path: Path, message: str, *, details: dict[str, Any] | None = None) -> DeliveryError:
    payload_details = {"stage": stage_name, "input_path": str(input_path)}
    if details:
        payload_details.update(details)
    project_hint = input_path.parents[2] if len(input_path.parents) > 2 else input_path.parent
    return DeliveryError(
        code="input_invalid_shape",
        message=message,
        exit_code=2,
        details=payload_details,
        suggested_action=f"Ask the stage skill to repair the JSON for {stage_name} and rerun: {stage_import_command(project_hint, stage_name, input_path)}",
    )


def load_stage_payload(
    project_root: Path | str,
    stage_name: str,
    input_value: str | None,
    *,
    expected_type: type[dict] | type[list],
    required_fields: list[str] | None = None,
) -> tuple[dict[str, Any] | list[Any], Path]:
    input_path = Path(input_value).expanduser().resolve() if input_value else stage_input_path(project_root, stage_name)
    if not input_path.exists():
        raise DeliveryError(
            code="input_missing",
            message=f"stage input payload does not exist: {input_path}",
            exit_code=2,
            details={
                "stage": stage_name,
                "input_path": str(input_path),
                "import_command": stage_import_command(project_root, stage_name, input_path),
            },
            suggested_action="Have the stage skill write the stage result JSON to the expected path, then rerun the same stage import command",
        )
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeliveryError(
            code="input_invalid_json",
            message=f"stage input payload is not valid JSON: {input_path}",
            exit_code=2,
            details={"stage": stage_name, "input_path": str(input_path), "error": str(exc)},
            suggested_action="Ask the stage skill to rewrite the file as raw JSON without prose, markdown fences, or trailing commentary",
        ) from exc
    if expected_type is dict and not isinstance(payload, dict):
        raise _shape_error(stage_name, input_path, f"stage input payload must be a JSON object: {input_path}", details={"expected": "object"})
    if expected_type is list and not isinstance(payload, list):
        raise _shape_error(stage_name, input_path, f"stage input payload must be a JSON array: {input_path}", details={"expected": "array"})
    if required_fields and isinstance(payload, dict):
        missing = [field for field in required_fields if field not in payload]
        if missing:
            raise DeliveryError(
                code="input_missing_fields",
                message=f"stage input payload is missing required fields: {', '.join(missing)}",
                exit_code=2,
                details={"stage": stage_name, "input_path": str(input_path), "missing_fields": missing},
                suggested_action="Ask the stage skill to regenerate the stage JSON with all required top-level fields present",
            )
    return payload, input_path


def import_spec_review(project_root: Path | str, payload: dict[str, Any], input_path: Path) -> int:
    requirements = payload.get("requirements")
    acceptance_scenarios = payload.get("acceptance_scenarios")
    clarifications = payload.get("clarifications") if isinstance(payload.get("clarifications"), list) else []
    source_requirements_path = str(payload.get("source_requirements_path") or "").strip()
    if not isinstance(requirements, list) or not isinstance(acceptance_scenarios, list):
        raise _shape_error("spec-review", input_path, "spec-review payload must contain requirements[] and acceptance_scenarios[] arrays")
    project_root = Path(project_root).expanduser().resolve()
    clarifications = [item for item in clarifications if isinstance(item, dict)]
    _persist_stage_input(project_root, "spec-review", payload)
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    if source_requirements_path:
        archive_requirements_source(project_root, source_requirements_path)
    requirements_payload = {
        "requirements": requirements,
        "acceptance_scenarios": acceptance_scenarios,
    }
    (docs_dir / "requirements.json").write_text(json.dumps(requirements_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    save_project_dependency_hints(project_root, payload.get("technology_hints"))
    blocking = [
        item
        for item in clarifications
        if isinstance(item, dict) and str(item.get("severity") or "").strip().upper() == "C1"
    ]
    clarification_path = docs_dir / "clarification-needed.md"
    if clarifications:
        lines = [f"blocking_count: {len(blocking)}", "# Clarification Needed", ""]
        for item in clarifications:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity") or "C2").strip()
            question = str(item.get("question") or "").strip()
            rationale = str(item.get("rationale") or "").strip()
            affected = ", ".join(str(value).strip() for value in item.get("affected_requirement_ids", []) if str(value).strip()) or "none"
            recommended = str(item.get("recommended_answer") or "").strip()
            options = [str(value).strip() for value in item.get("answer_options", []) if str(value).strip()]
            lines.append(f"## {severity} - {question}")
            lines.append("")
            if rationale:
                lines.append(rationale)
                lines.append("")
            if recommended:
                lines.append(f"Recommended answer: {recommended}")
                lines.append("")
            if options:
                lines.append("Suggested options:")
                lines.extend(f"- {value}" for value in options)
                lines.append("")
            lines.append(f"Affected requirements: {affected}")
            lines.append("")
        clarification_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif clarification_path.exists():
        clarification_path.unlink()
    return 2 if blocking else 0


def import_arch_design(project_root: Path | str, payload: dict[str, Any], input_path: Path) -> int:
    if not isinstance(payload.get("architecture_md"), str) or not isinstance(payload.get("shared_components_md"), str):
        raise _shape_error("arch-design", input_path, "arch-design payload must contain string fields architecture_md and shared_components_md")
    if not isinstance(payload.get("ui_required"), bool):
        raise _shape_error("arch-design", input_path, "arch-design payload field ui_required must be a boolean")
    _validate_architecture_markdown(input_path, str(payload.get("architecture_md") or ""))
    invalid_module_docs = _invalid_module_docs(payload)
    if invalid_module_docs:
        raise _shape_error(
            "arch-design",
            input_path,
            "arch-design modules[] must contain module design docs, not ADR documents",
            details={"invalid_module_docs": invalid_module_docs},
        )
    project_root = Path(project_root).expanduser().resolve()
    missing_dependency_hints = _architecture_missing_dependency_hints(project_root, payload)
    if missing_dependency_hints:
        raise _shape_error(
            "arch-design",
            input_path,
            "arch-design output must preserve explicit project technology constraints from dependency_hints",
            details={"missing_dependency_hints": missing_dependency_hints},
        )
    _persist_stage_input(project_root, "arch-design", payload)
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "architecture.md").write_text(str(payload.get("architecture_md") or ""), encoding="utf-8")
    (docs_dir / "shared-components.md").write_text(str(payload.get("shared_components_md") or ""), encoding="utf-8")

    def _normalize_arch_doc_path(raw_value: str, *, target: str) -> str:
        raw_path = str(raw_value or "").strip().lstrip("/")
        if raw_path.startswith("docs/"):
            raw_path = raw_path[len("docs/") :]
        parts = Path(raw_path).parts
        filename = Path(raw_path).name
        if target == "module":
            return f"modules/{filename}"
        return f"adr/{filename}"

    for row in payload.get("modules", []) or []:
        if not isinstance(row, dict):
            continue
        raw_path = _normalize_arch_doc_path(str(row.get("path") or ""), target="module")
        path = docs_dir / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(row.get("content") or ""), encoding="utf-8")
    for row in payload.get("adrs", []) or []:
        if not isinstance(row, dict):
            continue
        raw_path = _normalize_arch_doc_path(str(row.get("path") or ""), target="adr")
        path = docs_dir / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(row.get("content") or ""), encoding="utf-8")
    save_architecture_meta(project_root, {"schema_version": "1", "ui_required": bool(payload.get("ui_required"))})
    return 0


def import_ui_design(project_root: Path | str, payload: dict[str, Any], input_path: Path) -> int:
    required = ["template_selection_md", "design_system_md", "page_archetypes_md", "states_md"]
    if any(not isinstance(payload.get(field), str) for field in required):
        raise _shape_error("ui-design", input_path, "ui-design payload must contain string markdown fields for template_selection_md, design_system_md, page_archetypes_md, and states_md")
    project_root = Path(project_root).expanduser().resolve()
    _persist_stage_input(project_root, "ui-design", payload)
    ui_dir = project_root / "docs" / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "template-selection.md").write_text(str(payload.get("template_selection_md") or ""), encoding="utf-8")
    (ui_dir / "design-system.md").write_text(str(payload.get("design_system_md") or ""), encoding="utf-8")
    (ui_dir / "page-archetypes.md").write_text(str(payload.get("page_archetypes_md") or ""), encoding="utf-8")
    (ui_dir / "states.md").write_text(str(payload.get("states_md") or ""), encoding="utf-8")
    return 0


def import_context_sync(project_root: Path | str, payload: dict[str, Any], input_path: Path) -> int:
    if not isinstance(payload.get("test_plan"), dict):
        raise _shape_error("project-context-sync", input_path, "context-sync payload must contain a test_plan object")
    project_root = Path(project_root).expanduser().resolve()
    _persist_stage_input(project_root, "project-context-sync", payload)
    write_project_structure_snapshot(project_root)
    runtime = load_project_runtime(project_root) or "claude"
    context_file = root_context_filename(runtime)
    context_key = "claude_md" if context_file == "CLAUDE.md" else "agents_md"
    context_text = payload.get(context_key)
    if not isinstance(context_text, str):
        raise _shape_error("project-context-sync", input_path, f"context-sync payload must contain string field {context_key} for runtime {runtime}")
    merged_context = _merge_runtime_context(runtime, context_text)
    project_root.joinpath(context_file).write_text(merged_context, encoding="utf-8")
    save_test_plan(project_root, payload.get("test_plan") or {"schema_version": "1", "generated_at": utc_now_iso(), "coverage": []})
    return 0


def import_decompose(project_root: Path | str, payload: dict[str, Any], input_path: Path) -> int:
    project_root = Path(project_root).expanduser().resolve()
    _persist_stage_input(project_root, "task-decompose", payload)
    include_shared = (project_root / "docs" / "shared-components.md").exists()
    items = parse_task_json(json.dumps(payload, ensure_ascii=False))
    try:
        work_items_payload = decompose_tasks(project_root, items, include_shared_foundation=include_shared)
    except ValueError as exc:
        raise DeliveryError(
            code="stage_output_invalid",
            message=str(exc),
            exit_code=2,
            details={"stage": "task-decompose", "input_path": str(input_path), "project": str(Path(project_root).expanduser().resolve())},
            suggested_action="Ask the stage skill to repair the task graph using the validation error and rerun the same import command",
        ) from exc
    contract_errors = {
        task_id: row["errors"]
        for task_id, row in lint_task_contracts(
            [Task.from_dict(item) for item in work_items_payload.get("items", []) if isinstance(item, dict)]
        ).items()
        if row.get("errors")
    }
    if contract_errors:
        raise DeliveryError(
            code="stage_output_invalid",
            message="task-decompose produced one or more invalid task contracts",
            exit_code=2,
            details={
                "stage": "task-decompose",
                "input_path": str(input_path),
                "project": str(project_root),
                "contract_errors": contract_errors,
            },
            suggested_action="Ask the stage skill to repair the task graph using the reported contract errors and rerun the same import command",
        )
    save_work_items(project_root, work_items_payload)
    sync_gates(project_root, stage_payload=payload)
    return 0