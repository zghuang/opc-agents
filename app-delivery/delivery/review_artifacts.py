from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runtime_config import resolve_project_root
from .state import ensure_runtime_dirs, project_paths, utc_now_iso
from .task import Task


REVIEW_INPUT_DIRNAME = "review-inputs"
REVIEW_REQUEST_DIRNAME = "review-requests"
REVIEW_HISTORY_DIRNAME = "review-history"


def final_review_path(project_root: Path | str) -> Path:
    return project_paths(project_root).reviews_dir / "final-review.md"


def review_input_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    review_dir = paths.runtime_dir / REVIEW_INPUT_DIRNAME
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir / f"code-review-{task_id}.json"


def final_review_input_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    review_dir = paths.runtime_dir / REVIEW_INPUT_DIRNAME
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir / "final-review.json"


def code_review_request_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    request_dir = paths.runtime_dir / REVIEW_REQUEST_DIRNAME
    request_dir.mkdir(parents=True, exist_ok=True)
    return request_dir / f"code-review-{task_id}.md"


def final_review_request_path(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    request_dir = paths.runtime_dir / REVIEW_REQUEST_DIRNAME
    request_dir.mkdir(parents=True, exist_ok=True)
    return request_dir / "final-review.md"


def write_request_if_changed(path: Path, content: str) -> None:
    normalized = str(content or "")
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == normalized:
            return
    path.write_text(normalized, encoding="utf-8")


def review_history_dir(project_root: Path | str) -> Path:
    project_dir = resolve_project_root(project_root)
    paths = ensure_runtime_dirs(project_dir)
    history_dir = paths.runtime_dir / REVIEW_HISTORY_DIRNAME
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def review_round_stamp(reviewed_at: str | None = None) -> str:
    value = str(reviewed_at or utc_now_iso()).strip() or utc_now_iso()
    return value.replace("-", "").replace(":", "").replace(".", "")


def archive_review_history_entry(
    project_root: Path | str,
    *,
    review_name: str,
    variant: str,
    extension: str,
    content: str,
    reviewed_at: str | None = None,
) -> str:
    history_dir = review_history_dir(project_root)
    target = history_dir / f"{review_name}-{variant}-{review_round_stamp(reviewed_at)}.{extension.lstrip('.')}"
    target.write_text(str(content or ""), encoding="utf-8")
    return str(target.relative_to(resolve_project_root(project_root)))


def archive_review_round(
    project_root: Path | str,
    *,
    review_name: str,
    request_path: Path | None,
    input_extension: str,
    input_content: str,
    artifact_path: Path,
    reviewed_at: str,
) -> list[str]:
    archived: list[str] = []
    if request_path is not None and request_path.exists():
        archived.append(
            archive_review_history_entry(
                project_root,
                review_name=review_name,
                variant="request",
                extension=request_path.suffix or ".md",
                content=request_path.read_text(encoding="utf-8"),
                reviewed_at=reviewed_at,
            )
        )
    archived.append(
        archive_review_history_entry(
            project_root,
            review_name=review_name,
            variant="input",
            extension=input_extension,
            content=input_content,
            reviewed_at=reviewed_at,
        )
    )
    archived.append(
        archive_review_history_entry(
            project_root,
            review_name=review_name,
            variant="artifact",
            extension=artifact_path.suffix or ".md",
            content=artifact_path.read_text(encoding="utf-8"),
            reviewed_at=reviewed_at,
        )
    )
    return archived


def write_review_artifact(project_root: Path | str, task: Task, review_payload: dict[str, Any]) -> str:
    paths = project_paths(project_root)
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    review_path = paths.reviews_dir / f"code-review-{task.id}.md"
    status = str(review_payload.get("status") or "changes_requested").strip()
    summary = str(review_payload.get("summary") or "").strip()
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    requirement_assessment = review_payload.get("requirement_assessment") if isinstance(review_payload.get("requirement_assessment"), list) else []
    acceptance_assessment = review_payload.get("acceptance_assessment") if isinstance(review_payload.get("acceptance_assessment"), list) else []
    task_contract_assessment = review_payload.get("task_contract_assessment") if isinstance(review_payload.get("task_contract_assessment"), dict) else None
    lines = [
        f"status: {status}",
        "review_type: code",
        f"work_item: {task.id}",
        "",
        "# Code Review",
        "",
        "## Summary",
        "",
        summary or "No summary provided.",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            if isinstance(finding, dict):
                severity = str(finding.get("severity") or "").strip() or "finding"
                message = str(finding.get("message") or "").strip()
                refs = [*finding.get("requirement_ids", []), *finding.get("acceptance_ids", [])]
                suffix = f" ({', '.join(refs)})" if refs else ""
                lines.append(f"- [{severity}] {message}{suffix}")
            else:
                lines.append(f"- {finding}")
    else:
        lines.append("- No blocking findings.")
    if task_contract_assessment:
        lines.extend(["", "## Task Contract Assessment", ""])
        lines.append(f"- status: {task_contract_assessment.get('status', '-')}")
        lines.append(f"- issue_type: {task_contract_assessment.get('issue_type', '-')}")
        lines.append(f"- recommended_action: {task_contract_assessment.get('recommended_action', '-')}")
        operations = task_contract_assessment.get("operations") if isinstance(task_contract_assessment.get("operations"), list) else []
        if operations:
            lines.append(f"- operations: {len(operations)}")
        affected_requirements = ", ".join(task_contract_assessment.get("affected_requirement_ids", []) or []) or "none"
        affected_acceptance = ", ".join(task_contract_assessment.get("affected_acceptance_ids", []) or []) or "none"
        lines.append(f"- affected_requirements: {affected_requirements}")
        lines.append(f"- affected_acceptance: {affected_acceptance}")
        notes = str(task_contract_assessment.get("notes") or "").strip()
        if notes:
            lines.append(f"- notes: {notes}")
    if requirement_assessment:
        lines.extend(["", "## Requirement Assessment", ""])
        for row in requirement_assessment:
            if not isinstance(row, dict):
                continue
            lines.append(f"- {row.get('id', '-')}: {row.get('status', '-')} — {row.get('notes', '')}")
    if acceptance_assessment:
        lines.extend(["", "## Acceptance Assessment", ""])
        for row in acceptance_assessment:
            if not isinstance(row, dict):
                continue
            lines.append(f"- {row.get('id', '-')}: {row.get('status', '-')} — {row.get('notes', '')}")
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(review_path.relative_to(paths.project_root))


def write_review_repair_limit_report(
    project_root: Path | str,
    task: Task,
    *,
    review_payload: dict[str, Any],
    review_artifact: str,
    review_count: int,
    review_limit: int,
) -> str:
    paths = project_paths(project_root)
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    report_path = paths.reviews_dir / f"exception-report-{task.id}.md"
    summary = str(review_payload.get("summary") or "code review changes requested").strip()
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    lines = [
        "status: exception",
        "report_type: exception",
        f"work_item: {task.id}",
        "",
        "# Review Repair Limit Reached",
        "",
        "## Summary",
        "",
        f"Independent code review requested changes {review_count} time(s), reaching the configured limit of {review_limit}.",
        "",
        "## Latest Review",
        "",
        f"- review_artifact: {review_artifact}",
        f"- review_summary: {summary or 'No summary provided.'}",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            if not isinstance(finding, dict):
                lines.append(f"- {finding}")
                continue
            severity = str(finding.get("severity") or "finding").strip()
            message = str(finding.get("message") or "").strip()
            refs = [*finding.get("requirement_ids", []), *finding.get("acceptance_ids", [])]
            suffix = f" ({', '.join(refs)})" if refs else ""
            lines.append(f"- [{severity}] {message}{suffix}")
    else:
        lines.append("- none recorded")
    lines.extend(
        [
            "",
            "## Next Handling",
            "",
            "- Stop automatic review-repair looping for this task.",
            "- Inspect the latest review artifact and review history before deciding whether to run `app-delivery fix --task-id <id>`, repair the task contract, or escalate manually.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(paths.project_root))


def write_task_contract_deferral_artifacts(project_root: Path | str, deferral_result: dict[str, Any]) -> tuple[str, str]:
    paths = project_paths(project_root)
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.reviews_dir / "task-contract-deferrals.json"
    md_path = paths.reviews_dir / "task-contract-deferrals.md"
    existing_records: list[dict[str, Any]] = []
    if json_path.exists():
        try:
            existing_payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_payload = {}
        if isinstance(existing_payload, dict) and isinstance(existing_payload.get("deferred_acceptance_scenarios"), list):
            existing_records = [record for record in existing_payload["deferred_acceptance_scenarios"] if isinstance(record, dict)]
    new_records = [record for record in deferral_result.get("deferred_acceptance_scenarios", []) if isinstance(record, dict)]
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for record in [*existing_records, *new_records]:
        key = (str(record.get("from_task") or "").strip(), str(record.get("id") or "").strip())
        if not key[0] or not key[1]:
            continue
        by_key[key] = record
    records = list(by_key.values())
    json_path.write_text(json.dumps({"deferred_acceptance_scenarios": records}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = ["# Task Contract Deferrals", ""]
    if records:
        for record in records:
            lines.extend(
                [
                    f"## {record.get('id', '-')}",
                    "",
                    f"- From task: {record.get('from_task', '-')}",
                    f"- Policy: {record.get('policy', '-')}",
                    f"- Review artifact: {record.get('review_artifact', '-')}",
                    f"- Reason: {record.get('reason', '')}",
                    "",
                ]
            )
    else:
        lines.append("- none")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return str(json_path.relative_to(paths.project_root)), str(md_path.relative_to(paths.project_root))


def deferred_review_payload(parsed: dict[str, Any], deferral_result: dict[str, Any], deferral_artifacts: tuple[str, str]) -> dict[str, Any]:
    records = [record for record in deferral_result.get("deferred_acceptance_scenarios", []) if isinstance(record, dict)]
    deferred_ids = [str(record.get("id") or "").strip() for record in records if str(record.get("id") or "").strip()]
    existing_findings = parsed.get("findings") if isinstance(parsed.get("findings"), list) else []
    non_deferred_findings = [
        finding
        for finding in existing_findings
        if not isinstance(finding, dict) or not set(str(value).strip() for value in finding.get("acceptance_ids", []) if str(value).strip()).intersection(deferred_ids)
    ]
    non_deferred_acceptance = [
        row
        for row in parsed.get("acceptance_assessment", [])
        if isinstance(row, dict) and str(row.get("id") or "").strip() not in set(deferred_ids)
    ]
    deferral_finding = {
        "severity": "non_blocking",
        "requirement_ids": [],
        "acceptance_ids": deferred_ids,
        "message": f"Deferred acceptance scenario assignment for delivery continuity; see {deferral_artifacts[1]}",
    }
    summary = str(parsed.get("summary") or "").strip()
    deferred_summary = f"{summary} Framework accepted the current task with deferred acceptance scenario assignment recorded in {deferral_artifacts[1]}.".strip()
    payload = {
        key: value
        for key, value in parsed.items()
        if key != "task_contract_assessment"
    }
    return {
        **payload,
        "status": "pass",
        "summary": deferred_summary,
        "findings": [*non_deferred_findings, deferral_finding],
        "acceptance_assessment": non_deferred_acceptance,
    }


def write_final_review_artifact(project_root: Path | str, review_payload: dict[str, Any]) -> str:
    review_path = final_review_path(project_root)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    status = str(review_payload.get("status") or "changes_requested").strip()
    summary = str(review_payload.get("summary") or "").strip()
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    lines = [
        f"status: {status}",
        "review_type: final",
        "work_item: T-FINAL",
        "",
        "# Final Review",
        "",
        "## Summary",
        "",
        summary or "No summary provided.",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            if isinstance(finding, dict):
                severity = str(finding.get("severity") or "").strip() or "finding"
                message = str(finding.get("message") or "").strip()
                refs = [*finding.get("requirement_ids", []), *finding.get("acceptance_ids", [])]
                suffix = f" ({', '.join(refs)})" if refs else ""
                lines.append(f"- [{severity}] {message}{suffix}")
            else:
                lines.append(f"- {finding}")
    else:
        lines.append("- No blocking findings.")
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(review_path.relative_to(project_paths(project_root).project_root))


# Backward-compatible aliases for older imports and tests.
_final_review_path = final_review_path
_write_request_if_changed = write_request_if_changed
_review_history_dir = review_history_dir
_review_round_stamp = review_round_stamp
_archive_review_history_entry = archive_review_history_entry
_archive_review_round = archive_review_round
_write_review_artifact = write_review_artifact
_write_review_repair_limit_report = write_review_repair_limit_report
_write_task_contract_deferral_artifacts = write_task_contract_deferral_artifacts
_deferred_review_payload = deferred_review_payload
_write_final_review_artifact = write_final_review_artifact


__all__ = [
    "archive_review_round",
    "code_review_request_path",
    "deferred_review_payload",
    "final_review_input_path",
    "final_review_path",
    "final_review_request_path",
    "review_input_path",
    "write_final_review_artifact",
    "write_request_if_changed",
    "write_review_artifact",
    "write_review_repair_limit_report",
    "write_task_contract_deferral_artifacts",
    "_archive_review_round",
    "_deferred_review_payload",
    "_final_review_path",
    "_write_final_review_artifact",
    "_write_request_if_changed",
    "_write_review_artifact",
    "_write_review_repair_limit_report",
    "_write_task_contract_deferral_artifacts",
]
