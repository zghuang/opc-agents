from __future__ import annotations

from pathlib import Path

from delivery.planning_review import (
    candidate_sha256,
    planning_review_run_path,
    write_planning_review,
)
from delivery.state import write_json


def approve_candidate(project_root: Path, stage: str, candidate_path: Path) -> None:
    run_id = "foreground-unit-test"
    write_json(
        planning_review_run_path(project_root, stage),
        {
            "schema_version": "1",
            "status": "completed",
            "run_id": run_id,
            "stage": stage,
            "candidate_path": str(candidate_path.resolve()),
            "candidate_sha256": candidate_sha256(candidate_path),
            "completed_at": "2026-07-14T00:00:00Z",
        },
    )
    write_planning_review(
        project_root,
        stage=stage,
        candidate_path=candidate_path,
        review={
            "status": "pass",
            "summary": "Test candidate satisfies the planning review contract.",
            "findings": [],
            "reviewed_by": "unit-test",
            "review_runner_mode": "hermes-foreground-oneshot",
            "review_run_id": run_id,
        },
    )
