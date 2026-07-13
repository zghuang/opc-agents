from __future__ import annotations

import json
from pathlib import Path

from delivery.loop_review import import_final_review
from delivery.release_assessment import (
    RELEASE_ASSESSMENT_PATH,
    RELEASE_ASSESSMENT_SCHEMA_VERSION,
    RELEASE_DIMENSIONS,
    RELEASE_SCORE_THRESHOLD,
    REQUIRED_HARD_GATES,
)
from delivery.state import load_work_items, save_task_runtime_state, save_work_items


def _write_assessment(project_root: Path, *, score: int, gate_status: str) -> None:
    remaining = score
    dimensions = []
    for dimension_id, weight in RELEASE_DIMENSIONS:
        dimension_score = min(weight, remaining)
        remaining -= dimension_score
        dimensions.append(
            {
                "id": dimension_id,
                "weight": weight,
                "score": dimension_score,
                "notes": "Evidence reviewed.",
                "evidence": ["docs/reviews/release-evidence.md"],
            }
        )
    path = project_root / RELEASE_ASSESSMENT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": RELEASE_ASSESSMENT_SCHEMA_VERSION,
                "score": score,
                "threshold": RELEASE_SCORE_THRESHOLD,
                "dimensions": dimensions,
                "hard_gates": [
                    {"id": gate_id, "status": gate_status, "evidence": ["framework validation"]}
                    for gate_id in REQUIRED_HARD_GATES
                ],
            }
        ),
        encoding="utf-8",
    )


def _prepare_final_review(project_root: Path) -> Path:
    save_work_items(
        project_root,
        {
            "schema_version": "2",
            "project": "demo",
            "generated_at": "2026-06-24T00:00:00Z",
            "last_updated_commit": "",
            "items": [
                {
                    "id": "T-FINAL",
                    "title": "Final verification",
                    "status": "review_pending",
                    "requirements": [],
                    "acceptance_scenarios": [],
                    "dependencies": [],
                    "output_tests": [],
                    "output_paths": ["docs/release-evidence.md"],
                }
            ],
        },
    )
    input_path = project_root / "final-review.json"
    input_path.write_text(json.dumps({"status": "pass", "summary": "Ready.", "findings": []}), encoding="utf-8")
    ledger_path = project_root / "docs" / "reviews" / "system-gap-fix.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"schema_version": "1", "audit_status": "ready_for_final", "gaps": []}),
        encoding="utf-8",
    )
    save_task_runtime_state(
        project_root,
        "T-FINAL",
        {
            "final_release_hard_gates": [
                {"id": gate_id, "status": "pass", "evidence": ["framework validation"]}
                for gate_id in REQUIRED_HARD_GATES
            ]
        },
    )
    return input_path


def test_final_review_pass_allows_score_below_reference_threshold(tmp_path: Path) -> None:
    input_path = _prepare_final_review(tmp_path)
    _write_assessment(tmp_path, score=79, gate_status="pass")

    result = import_final_review(tmp_path, json.loads(input_path.read_text(encoding="utf-8")), input_path)

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"


def test_final_review_pass_allows_informational_failed_hard_gate_row(tmp_path: Path) -> None:
    input_path = _prepare_final_review(tmp_path)
    _write_assessment(tmp_path, score=RELEASE_SCORE_THRESHOLD, gate_status="fail")

    result = import_final_review(tmp_path, json.loads(input_path.read_text(encoding="utf-8")), input_path)

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"


def test_final_review_pass_allows_missing_informational_attestation(tmp_path: Path) -> None:
    input_path = _prepare_final_review(tmp_path)
    _write_assessment(tmp_path, score=RELEASE_SCORE_THRESHOLD, gate_status="pass")
    (tmp_path / ".app-delivery-runtime" / "task-runtime" / "T-FINAL.json").unlink()

    result = import_final_review(tmp_path, json.loads(input_path.read_text(encoding="utf-8")), input_path)

    assert result == 0
    payload = json.loads((tmp_path / "docs" / "work-items.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] == "verified"


def test_final_review_pass_creates_repair_for_invalid_verified_task_evidence(tmp_path: Path) -> None:
    input_path = _prepare_final_review(tmp_path)
    payload = load_work_items(tmp_path)
    payload["items"].insert(
        0,
        {
            "id": "T002",
            "title": "Feature",
            "status": "verified",
            "requirements": ["REQ-001"],
            "acceptance_scenarios": ["AS-001"],
            "dependencies": [],
            "output_tests": ["backend/tests/test_feature.py"],
            "output_paths": ["backend/app/feature.py"],
            "review_status": "changes_requested",
            "review_artifact": "docs/reviews/code-review-T002.md",
        },
    )
    save_work_items(tmp_path, payload)

    result = import_final_review(tmp_path, json.loads(input_path.read_text(encoding="utf-8")), input_path)

    assert result == 2
    updated = load_work_items(tmp_path)["items"]
    by_id = {item["id"]: item for item in updated}
    repair = next(item for item in updated if item.get("task_kind") == "repair")
    assert by_id["T002"]["status"] == "verified"
    assert repair["requirements"] == ["REQ-001"]
    assert repair["acceptance_scenarios"] == ["AS-001"]
    assert by_id["T-FINAL"]["status"] == "blocked"
