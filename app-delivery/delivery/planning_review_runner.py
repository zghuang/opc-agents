from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .errors import DeliveryError
from .planning_review import (
    candidate_sha256,
    planning_review_history_count,
    planning_review_run_path,
    write_planning_review,
)
from .state import ensure_runtime_dirs, utc_now_iso, write_json

RUNNER_MODE = "hermes-foreground-oneshot"
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_PLANNING_REVISIONS = 2
MAX_PLANNING_ATTEMPTS = MAX_PLANNING_REVISIONS + 1


def review_input_path(project_root: Path | str, stage: str) -> Path:
    paths = ensure_runtime_dirs(project_root)
    path = paths.runtime_dir / "planning-review-inputs" / f"{stage}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_hermes_bin() -> str:
    for candidate in (
        os.environ.get("APP_DELIVERY_HERMES_BIN"),
        os.environ.get("HERMES_BIN"),
        shutil.which("hermes"),
        str(Path.home() / ".local" / "bin" / "hermes"),
    ):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    raise DeliveryError(
        code="planning_review_runner_unavailable",
        message="Hermes executable is unavailable for the synchronous planning review",
        exit_code=2,
        suggested_action="Install Hermes or set APP_DELIVERY_HERMES_BIN, then rerun the planning stage.",
    )


def _review_prompt(project_root: Path, stage: str, candidate: Path, output: Path) -> str:
    return (
        "Run the planning-review skill exactly once in this fresh foreground one-shot session.\n"
        f"Project: {project_root}\n"
        f"Stage: {stage}\n"
        f"Candidate JSON: {candidate}\n"
        f"Raw review output JSON: {output}\n\n"
        "Rules:\n"
        "- Read the candidate and the source evidence required by the planning-review skill.\n"
        "- Do not rely on any parent conversation or prior review.\n"
        "- Do not regenerate or edit the candidate.\n"
        "- Do not edit canonical planning documents.\n"
        "- Write only the raw review JSON to the requested output path.\n"
        "- Do not import the review; the framework imports it after this process exits.\n"
        "- Stop after writing the review JSON.\n"
    )


def run_planning_review(
    project_root: Path | str,
    stage: str,
    candidate_path: Path | str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    project_dir = Path(project_root).expanduser().resolve()
    candidate = Path(candidate_path).expanduser().resolve()
    output = review_input_path(project_dir, stage)
    if not candidate.is_file():
        raise DeliveryError(
            code="planning_review_candidate_missing",
            message=f"planning review candidate does not exist: {candidate}",
            exit_code=2,
            details={"stage": stage, "candidate_path": str(candidate)},
        )
    if output.exists():
        output.unlink()
    run_id = uuid.uuid4().hex
    run_path = planning_review_run_path(project_dir, stage)
    candidate_hash = candidate_sha256(candidate)
    previous_state: dict[str, Any] = {}
    if run_path.is_file():
        try:
            loaded_state = json.loads(run_path.read_text(encoding="utf-8"))
            if isinstance(loaded_state, dict):
                previous_state = loaded_state
        except (OSError, json.JSONDecodeError):
            previous_state = {}
    previous_attempts = previous_state.get("review_attempts") if isinstance(previous_state.get("review_attempts"), list) else []
    cycle_status = str(previous_state.get("cycle_status") or "").strip()
    if cycle_status in {"passed", "forced_pass_after_revision_limit"}:
        previous_attempts = []
    completed_review_count = planning_review_history_count(project_dir, stage)
    if completed_review_count >= MAX_PLANNING_ATTEMPTS:
        raise DeliveryError(
            code="planning_review_limit_reached",
            message=f"{stage} planning review already used the stage-level budget of {MAX_PLANNING_ATTEMPTS} reviews",
            exit_code=2,
            details={
                "stage": stage,
                "candidate_path": str(candidate),
                "completed_review_count": completed_review_count,
                "max_reviews": MAX_PLANNING_ATTEMPTS,
                "max_revisions": MAX_PLANNING_REVISIONS,
            },
            suggested_action="Obtain an explicit host decision before starting another planning review cycle.",
        )
    review_attempt = len(previous_attempts) + 1
    if review_attempt > MAX_PLANNING_ATTEMPTS:
        raise DeliveryError(
            code="planning_review_limit_reached",
            message=f"{stage} planning review already used {MAX_PLANNING_REVISIONS} revision opportunities",
            exit_code=2,
            details={"stage": stage, "candidate_path": str(candidate), "review_attempt": review_attempt, "max_revisions": MAX_PLANNING_REVISIONS},
            suggested_action="Obtain an explicit host decision before starting another planning review cycle.",
        )
    write_json(
        run_path,
        {
            "schema_version": "1",
            "status": "running",
            "run_id": run_id,
            "stage": stage,
            "candidate_path": str(candidate),
            "candidate_sha256": candidate_hash,
            "review_attempt": review_attempt,
            "review_attempts": previous_attempts,
            "cycle_status": "running",
            "started_at": utc_now_iso(),
        },
    )

    logs_dir = ensure_runtime_dirs(project_dir).logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    stdout_log = logs_dir / f"planning-review-{stage}-{stamp}.out.log"
    stderr_log = logs_dir / f"planning-review-{stage}-{stamp}.err.log"
    hermes = _resolve_hermes_bin()
    command = [
        hermes,
        "--oneshot",
        _review_prompt(project_dir, stage, candidate, output),
        "--skills",
        "app-delivery",
        "--ignore-rules",
    ]
    env = os.environ.copy()
    env.setdefault("OPC_HOME", str(Path.home() / "opc"))
    env["APP_DELIVERY_PLANNING_REVIEW_RUNNER"] = RUNNER_MODE
    try:
        with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
            completed = subprocess.run(
                command,
                cwd=str(project_dir),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=env,
                check=False,
                timeout=max(1, int(timeout_seconds)),
            )
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(
            code="planning_review_runner_timeout",
            message=f"synchronous planning review exceeded {timeout_seconds} seconds",
            exit_code=2,
            details={"stage": stage, "candidate_path": str(candidate), "stdout_log": str(stdout_log), "stderr_log": str(stderr_log)},
        ) from exc
    if completed.returncode != 0:
        raise DeliveryError(
            code="planning_review_runner_failed",
            message=f"synchronous planning review exited with code {completed.returncode}",
            exit_code=2,
            details={"stage": stage, "candidate_path": str(candidate), "stdout_log": str(stdout_log), "stderr_log": str(stderr_log), "exit_code": completed.returncode},
        )
    if not output.is_file():
        raise DeliveryError(
            code="planning_review_output_missing",
            message=f"planning review runner completed without writing {output}",
            exit_code=2,
            details={"stage": stage, "candidate_path": str(candidate), "output_path": str(output), "stdout_log": str(stdout_log), "stderr_log": str(stderr_log)},
        )
    try:
        review = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(
            code="planning_review_invalid",
            message=f"planning review runner wrote invalid JSON: {output}",
            exit_code=2,
            details={"stage": stage, "output_path": str(output), "error": str(exc)},
        ) from exc
    if not isinstance(review, dict):
        raise DeliveryError(
            code="planning_review_invalid",
            message="planning review runner output must be a JSON object",
            exit_code=2,
            details={"stage": stage, "output_path": str(output)},
        )
    original_status = str(review.get("status") or "").strip().casefold()
    forced_pass = original_status == "revise" and review_attempt >= MAX_PLANNING_ATTEMPTS
    effective_status = "pass" if forced_pass else original_status
    review = {
        **review,
        "status": effective_status,
        "review_runner_mode": RUNNER_MODE,
        "review_run_id": run_id,
        "review_attempt": review_attempt,
        "review_runner_completed_at": utc_now_iso(),
    }
    if forced_pass:
        review.update(
            {
                "original_status": original_status,
                "forced_pass": True,
                "forced_pass_reason": "third_review_completed_after_two_revision_opportunities",
                "summary": f"{review.get('summary', 'Planning review requested revision')} Framework allowed continuation after {MAX_PLANNING_REVISIONS} revision opportunities; findings remain recorded.",
            }
        )
    attempts = [
        *previous_attempts,
        {
            "attempt": review_attempt,
            "candidate_sha256": candidate_hash,
            "run_id": run_id,
            "status": original_status,
            "forced_pass": forced_pass,
            "completed_at": utc_now_iso(),
        },
    ]
    review_path, passed = write_planning_review(
        project_dir,
        stage=stage,
        candidate_path=candidate,
        review=review,
    )
    write_json(
        run_path,
        {
            "schema_version": "1",
            "status": "completed",
            "run_id": run_id,
            "stage": stage,
            "candidate_path": str(candidate),
            "candidate_sha256": candidate_hash,
            "review_attempt": review_attempt,
            "review_attempts": attempts,
            "review_history_count": completed_review_count + 1,
            "cycle_status": "forced_pass_after_revision_limit" if forced_pass else ("passed" if effective_status == "pass" else "awaiting_revision"),
            "review_path": str(review_path),
            "completed_at": utc_now_iso(),
        },
    )
    return {
        "review": review,
        "review_path": str(review_path),
        "review_input_path": str(output),
        "candidate_sha256": candidate_hash,
        "run_id": run_id,
        "review_attempt": review_attempt,
        "forced_pass": forced_pass,
        "passed": passed,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
