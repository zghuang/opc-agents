from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from delivery import __main__ as cli
from delivery.errors import DeliveryError
from delivery.planning_review import (
    candidate_sha256,
    planning_review_run_path,
    require_planning_review_pass,
    write_planning_review,
)
from delivery.planning_review_runner import run_planning_review
from delivery.stage_harness import import_spec_review
from delivery.state import write_json


def _candidate(tmp_path: Path) -> Path:
    path = tmp_path / ".app-delivery-runtime" / "stage-inputs" / "spec-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": []}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def _mark_completed_run(tmp_path: Path, stage: str, candidate: Path) -> str:
    run_id = "foreground-unit-test"
    write_json(
        planning_review_run_path(tmp_path, stage),
        {
            "schema_version": "1",
            "status": "completed",
            "run_id": run_id,
            "stage": stage,
            "candidate_path": str(candidate.resolve()),
            "candidate_sha256": candidate_sha256(candidate),
            "completed_at": "2026-07-14T00:00:00Z",
        },
    )
    return run_id


def test_foreground_runner_waits_and_imports_review_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _candidate(tmp_path)

    def fake_run(command, *, cwd, stdin, stdout, stderr, env, check, timeout):
        output = tmp_path / ".app-delivery-runtime" / "planning-review-inputs" / "spec-review.json"
        output.write_text(json.dumps({"status": "pass", "summary": "foreground review"}) + "\n", encoding="utf-8")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("delivery.planning_review_runner._resolve_hermes_bin", lambda: "/usr/bin/hermes-test")
    monkeypatch.setattr("delivery.planning_review_runner.subprocess.run", fake_run)

    result = run_planning_review(tmp_path, "spec-review", candidate)

    assert result["passed"] is True
    assert result["review"]["review_runner_mode"] == "hermes-foreground-oneshot"
    assert require_planning_review_pass(tmp_path, "spec-review", candidate)["status"] == "pass"


def test_foreground_runner_rejects_missing_review_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _candidate(tmp_path)
    monkeypatch.setattr("delivery.planning_review_runner._resolve_hermes_bin", lambda: "/usr/bin/hermes-test")
    monkeypatch.setattr(
        "delivery.planning_review_runner.subprocess.run",
        lambda command, **kwargs: type("Completed", (), {"returncode": 0})(),
    )

    with pytest.raises(DeliveryError) as exc_info:
        run_planning_review(tmp_path, "spec-review", candidate)
    assert exc_info.value.code == "planning_review_output_missing"


def test_spec_review_allows_two_revisions_then_forces_third_review_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate(tmp_path)

    def fake_run(command, *, cwd, stdin, stdout, stderr, env, check, timeout):
        output = tmp_path / ".app-delivery-runtime" / "planning-review-inputs" / "spec-review.json"
        output.write_text(
            json.dumps(
                {
                    "status": "revise",
                    "summary": "split the candidate",
                    "findings": [{"severity": "major", "code": "overmerged_requirement"}],
                    "proposed_corrections": ["split the workflow"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("delivery.planning_review_runner._resolve_hermes_bin", lambda: "/usr/bin/hermes-test")
    monkeypatch.setattr("delivery.planning_review_runner.subprocess.run", fake_run)

    for attempt in (1, 2):
        with pytest.raises(DeliveryError) as exc_info:
            cli.cmd_spec_review(argparse.Namespace(project=str(tmp_path), input=str(candidate)))
        assert exc_info.value.code == "planning_review_not_passed"
        candidate.write_text(
            json.dumps({"requirements": [], "acceptance_scenarios": [], "revision": attempt}) + "\n",
            encoding="utf-8",
        )

    assert cli.cmd_spec_review(argparse.Namespace(project=str(tmp_path), input=str(candidate))) == 0
    review = json.loads((tmp_path / ".app-delivery-runtime" / "planning-reviews" / "spec-review.json").read_text(encoding="utf-8"))
    run_state = json.loads((tmp_path / ".app-delivery-runtime" / "planning-review-runs" / "spec-review.json").read_text(encoding="utf-8"))
    assert review["status"] == "pass"
    assert review["forced_pass"] is True
    assert review["review_attempt"] == 3
    assert run_state["cycle_status"] == "forced_pass_after_revision_limit"
    assert len(run_state["review_attempts"]) == 3
    assert (tmp_path / "docs" / "requirements.json").exists()
    history = sorted((tmp_path / ".app-delivery-runtime" / "planning-review-history" / "spec-review").glob("*.json"))
    assert len(history) == 3
    history_statuses = [json.loads(path.read_text(encoding="utf-8"))["original_status"] if "original_status" in json.loads(path.read_text(encoding="utf-8")) else json.loads(path.read_text(encoding="utf-8"))["status"] for path in history]
    assert history_statuses == ["revise", "revise", "revise"]


def test_planning_review_budget_spans_completed_cycles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _candidate(tmp_path)

    def fake_run(command, *, cwd, stdin, stdout, stderr, env, check, timeout):
        output = tmp_path / ".app-delivery-runtime" / "planning-review-inputs" / "spec-review.json"
        output.write_text(json.dumps({"status": "pass", "summary": "cycle pass"}) + "\n", encoding="utf-8")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("delivery.planning_review_runner._resolve_hermes_bin", lambda: "/usr/bin/hermes-test")
    monkeypatch.setattr("delivery.planning_review_runner.subprocess.run", fake_run)

    for revision in range(3):
        candidate.write_text(
            json.dumps({"requirements": [], "acceptance_scenarios": [], "revision": revision}) + "\n",
            encoding="utf-8",
        )
        result = run_planning_review(tmp_path, "spec-review", candidate)
        assert result["passed"] is True

    candidate.write_text(
        json.dumps({"requirements": [], "acceptance_scenarios": [], "revision": 3}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DeliveryError) as exc_info:
        run_planning_review(tmp_path, "spec-review", candidate)

    assert exc_info.value.code == "planning_review_limit_reached"
    assert exc_info.value.details["completed_review_count"] == 3
    assert exc_info.value.details["max_reviews"] == 3


def test_planning_review_keeps_previous_review_json_when_latest_is_replaced(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    first = {
        "status": "revise",
        "summary": "first review",
        "review_attempt": 1,
        "review_run_id": "run-one",
        "review_runner_mode": "hermes-foreground-oneshot",
    }
    second = {
        "status": "pass",
        "summary": "second review",
        "review_attempt": 2,
        "review_run_id": "run-two",
        "review_runner_mode": "hermes-foreground-oneshot",
    }

    write_planning_review(tmp_path, stage="spec-review", candidate_path=candidate, review=first)
    write_planning_review(tmp_path, stage="spec-review", candidate_path=candidate, review=second)

    latest = json.loads((tmp_path / ".app-delivery-runtime" / "planning-reviews" / "spec-review.json").read_text(encoding="utf-8"))
    history = sorted((tmp_path / ".app-delivery-runtime" / "planning-review-history" / "spec-review").glob("*.json"))
    assert latest["summary"] == "second review"
    assert len(history) == 2
    assert [json.loads(path.read_text(encoding="utf-8"))["summary"] for path in history] == ["first review", "second review"]


def test_planning_review_keeps_duplicate_run_writes_as_separate_history_files(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    review = {
        "status": "revise",
        "summary": "duplicate review",
        "review_attempt": 1,
        "review_run_id": "same-run",
        "review_runner_mode": "hermes-foreground-oneshot",
    }

    write_planning_review(tmp_path, stage="spec-review", candidate_path=candidate, review=review)
    write_planning_review(tmp_path, stage="spec-review", candidate_path=candidate, review=review)

    history = sorted((tmp_path / ".app-delivery-runtime" / "planning-review-history" / "spec-review").glob("*.json"))
    assert len(history) == 2
    assert all(json.loads(path.read_text(encoding="utf-8"))["summary"] == "duplicate review" for path in history)


def test_planning_review_requires_pass_before_canonical_import(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    with pytest.raises(DeliveryError) as exc_info:
        require_planning_review_pass(tmp_path, "spec-review", candidate)

    assert exc_info.value.code == "planning_review_required"


def test_spec_review_does_not_write_canonical_docs_without_review(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    with pytest.raises(DeliveryError) as exc_info:
        import_spec_review(
            tmp_path,
            {"requirements": [], "acceptance_scenarios": []},
            candidate,
        )

    assert exc_info.value.code == "planning_review_required"
    assert not (tmp_path / "docs" / "requirements.json").exists()
    assert "planning-review-inputs/spec-review.json" in str(
        exc_info.value.details["review_input_path"]
    )
    assert "synchronous foreground Hermes" in str(exc_info.value.suggested_action)


def test_planning_review_binds_pass_to_exact_candidate_bytes(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    run_id = _mark_completed_run(tmp_path, "spec-review", candidate)
    write_planning_review(
        tmp_path,
        stage="spec-review",
        candidate_path=candidate,
        review={
            "status": "pass",
            "summary": "ok",
            "review_runner_mode": "hermes-foreground-oneshot",
            "review_run_id": run_id,
        },
    )

    assert require_planning_review_pass(tmp_path, "spec-review", candidate)["status"] == "pass"
    candidate.write_text(
        json.dumps({"requirements": [{"id": "REQ-001"}], "acceptance_scenarios": []}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DeliveryError) as exc_info:
        require_planning_review_pass(tmp_path, "spec-review", candidate)
    assert exc_info.value.code == "planning_review_stale"


def test_planning_review_revise_is_not_a_pass(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    write_planning_review(
        tmp_path,
        stage="task-decompose",
        candidate_path=candidate,
        review={"status": "revise", "findings": [{"code": "oversized_task"}]},
    )

    with pytest.raises(DeliveryError) as exc_info:
        require_planning_review_pass(tmp_path, "task-decompose", candidate)
    assert exc_info.value.code == "planning_review_not_passed"


def test_planning_review_rejects_pass_without_foreground_runner_provenance(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    write_planning_review(
        tmp_path,
        stage="spec-review",
        candidate_path=candidate,
        review={"status": "pass", "summary": "manual pass"},
    )

    with pytest.raises(DeliveryError) as exc_info:
        require_planning_review_pass(tmp_path, "spec-review", candidate)
    assert exc_info.value.code == "planning_review_runner_required"


def test_planning_review_rejects_blocked_as_a_verdict(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    with pytest.raises(DeliveryError) as exc_info:
        write_planning_review(
            tmp_path,
            stage="spec-review",
            candidate_path=candidate,
            review={"status": "blocked", "summary": "needs a user decision"},
        )
    assert exc_info.value.code == "planning_review_invalid"


def test_planning_review_rejects_candidate_stage_mismatch(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    run_id = _mark_completed_run(tmp_path, "task-decompose", candidate)
    write_planning_review(
        tmp_path,
        stage="task-decompose",
        candidate_path=candidate,
        review={
            "status": "pass",
            "review_runner_mode": "hermes-foreground-oneshot",
            "review_run_id": run_id,
        },
    )
    review_path = tmp_path / ".app-delivery-runtime" / "planning-reviews" / "task-decompose.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["stage"] = "spec-review"
    review_path.write_text(json.dumps(review) + "\n", encoding="utf-8")

    with pytest.raises(DeliveryError) as exc_info:
        require_planning_review_pass(tmp_path, "task-decompose", candidate)
    assert exc_info.value.code == "planning_review_invalid"
