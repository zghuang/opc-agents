from __future__ import annotations

import pytest

from delivery.release_assessment import (
    RELEASE_ASSESSMENT_SCHEMA_VERSION,
    RELEASE_DIMENSIONS,
    RELEASE_SCORE_THRESHOLD,
    REQUIRED_HARD_GATES,
    validate_release_assessment,
)


def _assessment(*, score: int = 80, gate_status: str = "pass") -> dict[str, object]:
    dimensions = []
    remaining = score
    for dimension_id, weight in RELEASE_DIMENSIONS:
        dimension_score = min(weight, remaining)
        remaining -= dimension_score
        dimensions.append(
            {
                "id": dimension_id,
                "weight": weight,
                "score": dimension_score,
                "notes": "Evidence reviewed.",
                "evidence": ["docs/reviews/evidence.md"],
            }
        )
    return {
        "schema_version": RELEASE_ASSESSMENT_SCHEMA_VERSION,
        "score": score,
        "threshold": RELEASE_SCORE_THRESHOLD,
        "dimensions": dimensions,
        "hard_gates": [
            {"id": gate_id, "status": gate_status, "evidence": ["framework validation"]}
            for gate_id in REQUIRED_HARD_GATES
        ],
    }


def test_accepts_eligible_release_assessment() -> None:
    assessment = validate_release_assessment(_assessment())

    assert assessment["release_eligible"] is True


def test_rejects_score_that_does_not_match_dimensions() -> None:
    payload = _assessment()
    payload["score"] = 81

    with pytest.raises(ValueError, match="score must equal"):
        validate_release_assessment(payload)


def test_marks_failed_hard_gate_ineligible_without_rejecting_evidence() -> None:
    assessment = validate_release_assessment(_assessment(gate_status="fail"))

    assert assessment["release_eligible"] is False


def test_rejects_missing_required_hard_gate() -> None:
    payload = _assessment()
    payload["hard_gates"] = payload["hard_gates"][:-1]

    with pytest.raises(ValueError, match="missing hard gates"):
        validate_release_assessment(payload)
