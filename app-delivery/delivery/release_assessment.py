from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RELEASE_ASSESSMENT_PATH = "docs/reviews/release-assessment.json"
RELEASE_ASSESSMENT_SCHEMA_VERSION = "1"
RELEASE_SCORE_THRESHOLD = 80
RELEASE_DIMENSIONS: tuple[tuple[str, int], ...] = (
    ("requirements_fulfillment", 30),
    ("behavioral_validation", 25),
    ("security_and_boundaries", 20),
    ("integration_and_recovery", 15),
    ("operational_readiness", 10),
)
REQUIRED_HARD_GATES = (
    "system_gap_fix",
    "full_suite",
    "requirement_coverage",
    "test_type_coverage",
    "validation_gates",
    "production_semantics",
)
ALLOWED_HARD_GATE_STATUSES = {"pass", "fail"}


def release_assessment_path(project_root: Path | str) -> Path:
    return Path(project_root).expanduser().resolve() / RELEASE_ASSESSMENT_PATH


def _string_list(value: Any, field_name: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be an array")
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def validate_release_assessment(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("release assessment must be a JSON object")
    errors: list[str] = []
    if str(payload.get("schema_version") or "").strip() != RELEASE_ASSESSMENT_SCHEMA_VERSION:
        errors.append(f"schema_version must be {RELEASE_ASSESSMENT_SCHEMA_VERSION}")
    score = payload.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        errors.append("score must be an integer from 0 through 100")
        score = 0
    threshold = payload.get("threshold", RELEASE_SCORE_THRESHOLD)
    if threshold != RELEASE_SCORE_THRESHOLD:
        errors.append(f"threshold must be {RELEASE_SCORE_THRESHOLD}")
    raw_dimensions = payload.get("dimensions")
    if not isinstance(raw_dimensions, list):
        errors.append("dimensions must be an array")
        raw_dimensions = []
    expected_weights = dict(RELEASE_DIMENSIONS)
    dimensions: list[dict[str, Any]] = []
    seen_dimensions: set[str] = set()
    for index, row in enumerate(raw_dimensions):
        if not isinstance(row, dict):
            errors.append(f"dimensions[{index}] must be an object")
            continue
        dimension_id = str(row.get("id") or "").strip()
        weight = row.get("weight")
        dimension_score = row.get("score")
        notes = str(row.get("notes") or "").strip()
        evidence = _string_list(row.get("evidence"), f"dimensions[{index}].evidence", errors)
        if dimension_id not in expected_weights:
            errors.append(f"dimensions[{index}].id is unknown: {dimension_id or '<empty>'}")
            continue
        if dimension_id in seen_dimensions:
            errors.append(f"dimensions contains duplicate id: {dimension_id}")
            continue
        seen_dimensions.add(dimension_id)
        if weight != expected_weights[dimension_id]:
            errors.append(f"dimensions[{index}].weight for {dimension_id} must be {expected_weights[dimension_id]}")
        if not isinstance(dimension_score, int) or isinstance(dimension_score, bool) or not 0 <= dimension_score <= expected_weights[dimension_id]:
            errors.append(f"dimensions[{index}].score for {dimension_id} must be an integer from 0 through {expected_weights[dimension_id]}")
            dimension_score = 0
        if not notes:
            errors.append(f"dimensions[{index}].notes must be non-empty")
        if not evidence:
            errors.append(f"dimensions[{index}].evidence must be non-empty")
        dimensions.append(
            {
                "id": dimension_id,
                "weight": expected_weights[dimension_id],
                "score": dimension_score,
                "notes": notes,
                "evidence": evidence,
            }
        )
    missing_dimensions = [dimension_id for dimension_id, _ in RELEASE_DIMENSIONS if dimension_id not in seen_dimensions]
    if missing_dimensions:
        errors.append("missing assessment dimensions: " + ", ".join(missing_dimensions))
    if sum(row["score"] for row in dimensions) != score:
        errors.append("score must equal the sum of dimension scores")

    raw_hard_gates = payload.get("hard_gates")
    if not isinstance(raw_hard_gates, list):
        errors.append("hard_gates must be an array")
        raw_hard_gates = []
    hard_gates: list[dict[str, Any]] = []
    seen_gates: set[str] = set()
    for index, row in enumerate(raw_hard_gates):
        if not isinstance(row, dict):
            errors.append(f"hard_gates[{index}] must be an object")
            continue
        gate_id = str(row.get("id") or "").strip()
        status = str(row.get("status") or "").strip().casefold()
        evidence = _string_list(row.get("evidence"), f"hard_gates[{index}].evidence", errors)
        if gate_id not in REQUIRED_HARD_GATES:
            errors.append(f"hard_gates[{index}].id is unknown: {gate_id or '<empty>'}")
            continue
        if gate_id in seen_gates:
            errors.append(f"hard_gates contains duplicate id: {gate_id}")
            continue
        seen_gates.add(gate_id)
        if status not in ALLOWED_HARD_GATE_STATUSES:
            errors.append(f"hard_gates[{index}].status must be one of: {', '.join(sorted(ALLOWED_HARD_GATE_STATUSES))}")
        if not evidence:
            errors.append(f"hard_gates[{index}].evidence must be non-empty")
        hard_gates.append({"id": gate_id, "status": status, "evidence": evidence})
    missing_gates = [gate_id for gate_id in REQUIRED_HARD_GATES if gate_id not in seen_gates]
    if missing_gates:
        errors.append("missing hard gates: " + ", ".join(missing_gates))
    if errors:
        raise ValueError("; ".join(errors))
    release_eligible = score >= RELEASE_SCORE_THRESHOLD and all(gate["status"] == "pass" for gate in hard_gates)
    return {
        "schema_version": RELEASE_ASSESSMENT_SCHEMA_VERSION,
        "score": score,
        "threshold": RELEASE_SCORE_THRESHOLD,
        "dimensions": dimensions,
        "hard_gates": hard_gates,
        "release_eligible": release_eligible,
    }


def write_release_assessment(project_root: Path | str, assessment: dict[str, Any]) -> str:
    path = release_assessment_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(assessment, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path.relative_to(Path(project_root).expanduser().resolve()))
