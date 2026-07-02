from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


PYTHON_REACT_STACK_ID = "python-react"


@dataclass(frozen=True)
class StackContract:
    id: str
    backend_source_root: str
    legacy_backend_app_root: str
    backend_runtime_root: str
    backend_entrypoint: str
    backend_test_roots: tuple[str, ...]
    frontend_source_root: str
    frontend_e2e_root: str
    mock_server_root: str
    noncanonical_backend_root_dirs: tuple[str, ...]
    unsupported_top_level_roots: tuple[str, ...]
    guidance_reference: str


PYTHON_REACT_CONTRACT = StackContract(
    id=PYTHON_REACT_STACK_ID,
    backend_source_root="backend",
    legacy_backend_app_root="",
    backend_runtime_root="",
    backend_entrypoint="",
    backend_test_roots=("backend/tests",),
    frontend_source_root="frontend/src",
    frontend_e2e_root="frontend/e2e",
    mock_server_root="mock-server",
    noncanonical_backend_root_dirs=(),
    unsupported_top_level_roots=(),
    guidance_reference="skills/arch-design/references/python-react.md",
)


STACK_CONTRACTS = {PYTHON_REACT_STACK_ID: PYTHON_REACT_CONTRACT}


def normalize_stack_id(value: str | None) -> str:
    normalized = str(value or PYTHON_REACT_STACK_ID).strip().casefold()
    return normalized or PYTHON_REACT_STACK_ID


def stack_contract(stack_id: str | None) -> StackContract:
    normalized = normalize_stack_id(stack_id)
    return STACK_CONTRACTS.get(normalized, PYTHON_REACT_CONTRACT)


def project_stack_id(project_root: Path | str) -> str:
    metadata_path = Path(project_root).expanduser().resolve() / "docs" / "project-bootstrap.json"
    if not metadata_path.exists():
        return PYTHON_REACT_STACK_ID
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return PYTHON_REACT_STACK_ID
    if not isinstance(payload, dict):
        return PYTHON_REACT_STACK_ID
    return normalize_stack_id(str(payload.get("stack") or PYTHON_REACT_STACK_ID))


def project_stack_contract(project_root: Path | str) -> StackContract:
    return stack_contract(project_stack_id(project_root))


def stack_guidance_markdown(stack_id: str | None) -> str:
    contract = stack_contract(stack_id)
    reference_path = Path(__file__).resolve().parents[1] / contract.guidance_reference
    if reference_path.exists():
        return reference_path.read_text(encoding="utf-8").strip()
    return f"# {contract.id}\n\n- No stack guidance reference is installed for this stack."


def project_stack_guidance_markdown(project_root: Path | str) -> str:
    return stack_guidance_markdown(project_stack_id(project_root))


def backend_test_root(contract: StackContract | None = None) -> str:
    selected = contract or PYTHON_REACT_CONTRACT
    return selected.backend_test_roots[-1] if selected.backend_test_roots else "backend/tests"


def optional_stack_paths(*values: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in {"/", "./"}:
            continue
        result.append(text)
    return tuple(result)