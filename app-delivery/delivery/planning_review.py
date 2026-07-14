from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .errors import DeliveryError
from .state import ensure_runtime_dirs, utc_now_iso, write_json

PLANNING_REVIEW_SCHEMA_VERSION = "1"
PLANNING_REVIEW_DIR = Path(".app-delivery-runtime") / "planning-reviews"
PLANNING_REVIEW_HISTORY_DIR = Path(".app-delivery-runtime") / "planning-review-history"
PLANNING_REVIEW_RUN_DIR = Path(".app-delivery-runtime") / "planning-review-runs"
PLANNING_REVIEW_STAGES = {"spec-review", "task-decompose"}
PLANNING_REVIEW_STATUSES = {"pass", "revise"}


def planning_review_path(project_root: Path | str, stage: str) -> Path:
    normalized_stage = str(stage or "").strip()
    if normalized_stage not in PLANNING_REVIEW_STAGES:
        raise ValueError(f"unsupported planning review stage: {stage}")
    paths = ensure_runtime_dirs(project_root)
    review_dir = paths.runtime_dir / "planning-reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir / f"{normalized_stage}.json"


def planning_review_run_path(project_root: Path | str, stage: str) -> Path:
    paths = ensure_runtime_dirs(project_root)
    run_dir = paths.runtime_dir / "planning-review-runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / f"{stage}.json"


def planning_review_history_dir(project_root: Path | str, stage: str) -> Path:
    paths = ensure_runtime_dirs(project_root)
    history_dir = paths.runtime_dir / PLANNING_REVIEW_HISTORY_DIR.name / stage
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def planning_review_history_count(project_root: Path | str, stage: str) -> int:
    return sum(1 for path in planning_review_history_dir(project_root, stage).glob("*.json") if path.is_file())


def candidate_sha256(candidate_path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(candidate_path).expanduser().resolve().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _planning_review_error(
    *,
    code: str,
    message: str,
    project_root: Path,
    stage: str,
    candidate_path: Path,
    review_path: Path,
    details: dict[str, Any] | None = None,
) -> DeliveryError:
    review_input_path = (
        project_root / ".app-delivery-runtime" / "planning-review-inputs" / f"{stage}.json"
    )
    payload_details: dict[str, Any] = {
        "stage": stage,
        "candidate_path": str(candidate_path),
        "review_path": str(review_path),
        "review_input_path": str(review_input_path),
        "review_runner": "hermes-foreground-oneshot",
    }
    if details:
        payload_details.update(details)
    return DeliveryError(
        code=code,
        message=message,
        exit_code=2,
        details=payload_details,
        suggested_action=(
            f"Run the synchronous foreground Hermes planning reviewer for {stage}, then rerun the stage import."
        ),
    )


def require_planning_review_pass(
    project_root: Path | str, stage: str, candidate_path: Path | str
) -> dict[str, Any]:
    project_dir = Path(project_root).expanduser().resolve()
    candidate = Path(candidate_path).expanduser().resolve()
    review_path = planning_review_path(project_dir, stage)
    if not review_path.exists():
        raise _planning_review_error(
            code="planning_review_required",
            message=f"{stage} candidate requires a synchronous foreground planning review before import",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
        )
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _planning_review_error(
            code="planning_review_invalid",
            message=f"planning review artifact is not valid JSON: {review_path}",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={"error": str(exc)},
        ) from exc
    if not isinstance(review, dict):
        raise _planning_review_error(
            code="planning_review_invalid",
            message="planning review artifact must be a JSON object",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
        )
    expected_hash = candidate_sha256(candidate)
    if str(review.get("schema_version") or "").strip() != PLANNING_REVIEW_SCHEMA_VERSION:
        raise _planning_review_error(
            code="planning_review_invalid",
            message="planning review artifact has an unsupported schema_version",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={"expected_schema_version": PLANNING_REVIEW_SCHEMA_VERSION},
        )
    if str(review.get("stage") or "").strip() != stage:
        raise _planning_review_error(
            code="planning_review_invalid",
            message="planning review stage does not match the candidate stage",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={"review_stage": review.get("stage")},
        )
    recorded_candidate = Path(str(review.get("candidate_path") or "")).expanduser().resolve()
    if recorded_candidate != candidate:
        raise _planning_review_error(
            code="planning_review_invalid",
            message="planning review candidate_path does not match the imported candidate",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={"review_candidate_path": str(recorded_candidate)},
        )
    if str(review.get("candidate_sha256") or "").strip() != expected_hash:
        raise _planning_review_error(
            code="planning_review_stale",
            message="planning review was produced for a different candidate artifact",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={
                "review_candidate_sha256": review.get("candidate_sha256"),
                "candidate_sha256": expected_hash,
            },
        )
    status = str(review.get("status") or "").strip().casefold()
    if status != "pass":
        raise _planning_review_error(
            code="planning_review_not_passed",
            message=f"{stage} planning review returned status={status or 'missing'}",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={"review": review},
        )
    run_id = str(review.get("review_run_id") or "").strip()
    run_state = {}
    run_path = planning_review_run_path(project_dir, stage)
    if run_path.is_file():
        try:
            run_state = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_state = {}
    if (
        str(review.get("review_runner_mode") or "").strip() != "hermes-foreground-oneshot"
        or not run_id
        or not isinstance(run_state, dict)
        or str(run_state.get("status") or "").strip() != "completed"
        or str(run_state.get("run_id") or "").strip() != run_id
        or str(run_state.get("candidate_sha256") or "").strip() != expected_hash
    ):
        raise _planning_review_error(
            code="planning_review_runner_required",
            message="planning review pass must come from the synchronous foreground Hermes runner",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={
                "review_runner_mode": review.get("review_runner_mode"),
                "review_run_id": run_id,
                "run_path": str(run_path),
            },
        )
    return review


def write_planning_review(
    project_root: Path | str,
    *,
    stage: str,
    candidate_path: Path | str,
    review: dict[str, Any],
) -> tuple[Path, bool]:
    project_dir = Path(project_root).expanduser().resolve()
    candidate = Path(candidate_path).expanduser().resolve()
    review_path = planning_review_path(project_dir, stage)
    if not isinstance(review, dict):
        raise ValueError("planning review must be a JSON object")
    payload = {
        **review,
        "schema_version": PLANNING_REVIEW_SCHEMA_VERSION,
        "stage": stage,
        "candidate_path": str(candidate),
        "candidate_sha256": candidate_sha256(candidate),
        "reviewed_at": str(review.get("reviewed_at") or utc_now_iso()),
        "findings": review.get("findings") if isinstance(review.get("findings"), list) else [],
    }
    status = str(payload.get("status") or "").strip().casefold()
    if status not in PLANNING_REVIEW_STATUSES:
        raise _planning_review_error(
            code="planning_review_invalid",
            message=f"planning review status must be one of: {', '.join(sorted(PLANNING_REVIEW_STATUSES))}",
            project_root=project_dir,
            stage=stage,
            candidate_path=candidate,
            review_path=review_path,
            details={"status": review.get("status")},
        )
    attempt = str(review.get("review_attempt") or "0").strip()
    try:
        attempt_number = max(0, int(attempt))
    except ValueError:
        attempt_number = 0
    run_id = str(review.get("review_run_id") or "").strip() or uuid.uuid4().hex
    history_payload = {
        **payload,
        "review_history_id": run_id,
        "review_attempt": attempt_number,
    }
    history_dir = planning_review_history_dir(project_dir, stage)
    history_name = f"attempt-{attempt_number:03d}-{run_id}-{payload['candidate_sha256'][:16]}"
    history_path = history_dir / f"{history_name}.json"
    while history_path.exists():
        history_path = history_dir / f"{history_name}-{uuid.uuid4().hex}.json"
    write_json(history_path, history_payload)
    write_json(review_path, payload)
    return review_path, status == "pass"
