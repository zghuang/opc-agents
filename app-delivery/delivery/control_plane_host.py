from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .loop_review import final_review_input_path, review_input_path
from .stage_harness import stage_import_command, stage_input_path


APP_DELIVERY_BIN = "${OPC_HOME:-$HOME/opc}/bin/app-delivery"


def _generate_only_prompt(prefix: str, expected_input_path: str) -> str:
    return (
        f"{prefix} "
        f"Write the canonical JSON artifact to {expected_input_path}. "
        "Return only a concise final summary."
    )


def _host_skill_step(
    *,
    project_root: Path,
    skill: str,
    action: str,
    message: str,
    prompt: str,
    task_id: str | None = None,
    expected_input_path: str | None = None,
    import_command: str | None = None,
    blocking: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_text = prompt
    if import_command:
        prompt_text = (
            f"{prompt_text.rstrip()} "
            f"After writing the artifact, run this import command to hand it back to the framework: {import_command}. "
            "If the import command fails, leave the artifact at the expected path and report the import failure in the final summary."
        )
    payload: dict[str, Any] = {
        "kind": "host_skill",
        "owner": "host",
        "skill": skill,
        "action": action,
        "message": message,
        "prompt": prompt_text,
        "project": str(project_root),
        "blocking": blocking,
    }
    if task_id:
        payload["task_id"] = task_id
    if expected_input_path:
        payload["expected_input_path"] = expected_input_path
    if import_command:
        payload["import_command"] = import_command
    if extra:
        payload.update(extra)
    return payload


def _blocking_clarifications(project_root: Path) -> list[dict[str, Any]]:
    input_path = stage_input_path(project_root, "spec-review")
    if not input_path.exists():
        return []
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    clarifications = payload.get("clarifications") if isinstance(payload, dict) else []
    if not isinstance(clarifications, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in clarifications:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").strip().upper()
        blocking = bool(item.get("blocking")) or severity == "C1"
        if not blocking:
            continue
        options = [str(value).strip() for value in item.get("answer_options", []) if str(value).strip()]
        rows.append(
            {
                "severity": severity or "C1",
                "question": str(item.get("question") or "").strip(),
                "rationale": str(item.get("rationale") or "").strip(),
                "affected_requirement_ids": [str(value).strip() for value in item.get("affected_requirement_ids", []) if str(value).strip()],
                "recommended_answer": str(item.get("recommended_answer") or "").strip() or None,
                "answer_options": options,
            }
        )
    return rows


def _clarification_answers_template(clarifications: list[dict[str, Any]]) -> str:
    lines = ["# Clarification Answers", ""]
    for item in clarifications:
        question = str(item.get("question") or "").strip() or "Unspecified clarification"
        rationale = str(item.get("rationale") or "").strip()
        recommended = str(item.get("recommended_answer") or "").strip()
        options = [str(value).strip() for value in item.get("answer_options", []) if str(value).strip()]
        lines.append(f"## {question}")
        lines.append("")
        if rationale:
            lines.append(f"Context: {rationale}")
            lines.append("")
        if recommended:
            lines.append(f"Recommended answer: {recommended}")
            lines.append("")
        if options:
            lines.append("Suggested options:")
            lines.extend(f"- {value}" for value in options)
            lines.append("")
        lines.append("Answer: ")
        lines.append("")
        lines.append("Decision notes: ")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _clarification_answers_ready(project_root: Path) -> bool:
    path = project_root / "docs" / "clarification-answers.md"
    if not path.exists():
        return False
    return bool(path.read_text(encoding="utf-8").strip())


def build_planning_host_step(
    *,
    code: str,
    project_root: Path,
    requirements_path: str | None,
    contract_errors: dict[str, list[str]] | None = None,
) -> dict[str, Any] | None:
    if code == "requirements_missing":
        req_path = requirements_path or ""
        return _host_skill_step(
            project_root=project_root,
            skill="spec-review",
            action="run_spec_review",
            message="Requirements normalization is missing; run spec-review and import the canonical stage output.",
            expected_input_path=str(stage_input_path(project_root, "spec-review")),
            import_command=stage_import_command(project_root, "spec-review"),
            prompt=_generate_only_prompt(
                f"Use the spec-review skill for project {project_root} with requirements_path {req_path}.",
                str(stage_input_path(project_root, "spec-review")),
            ),
            extra={"requirements_path": req_path},
        )
    if code == "clarification_blocking":
        req_path = requirements_path or ""
        clarifications = _blocking_clarifications(project_root)
        if _clarification_answers_ready(project_root):
            return _host_skill_step(
                project_root=project_root,
                skill="spec-review",
                action="repair_spec_review",
                message="Blocking clarifications have user answers; regenerate the canonical spec-review output so the clarified interpretation is embedded in the imported JSON.",
                expected_input_path=str(stage_input_path(project_root, "spec-review")),
                import_command=stage_import_command(project_root, "spec-review"),
                prompt=_generate_only_prompt(
                    f"Use the spec-review skill for project {project_root} with requirements_path {req_path}. The project currently has blocking clarifications in docs/clarification-needed.md, and the user has answered them in docs/clarification-answers.md. Treat that file as source evidence, fold the answered clarifications back into the normalized requirements, and only keep a clarification blocking when it still genuinely requires a new external decision from the user. Regenerate a corrected spec-review.json using the current project evidence.",
                    str(stage_input_path(project_root, "spec-review")),
                ),
                extra={"requirements_path": req_path},
            )
        return {
            "kind": "host_interaction",
            "owner": "host",
            "action": "collect_clarification_answers",
            "message": "Blocking clarifications require user answers before spec-review can continue.",
            "project": str(project_root),
            "blocking": True,
            "requires_user_input": True,
            "requirements_path": req_path,
            "clarification_path": str(project_root / "docs" / "clarification-needed.md"),
            "answers_path": str(project_root / "docs" / "clarification-answers.md"),
            "expected_input_path": str(stage_input_path(project_root, "spec-review")),
            "import_command": stage_import_command(project_root, "spec-review"),
            "clarifications": clarifications,
            "answers_markdown_template": _clarification_answers_template(clarifications),
            "resume_prompt": _generate_only_prompt(
                f"Use the spec-review skill for project {project_root} with requirements_path {req_path}. The project currently has blocking clarifications in docs/clarification-needed.md. Read docs/clarification-answers.md after the user answers the blocking questions, treat that file as source evidence, and fold the answered clarifications back into the normalized requirements. Only keep a clarification blocking when it still genuinely requires a new external decision from the user. Regenerate a corrected spec-review.json using the current project evidence.",
                str(stage_input_path(project_root, "spec-review")),
            ),
        }
    if code == "architecture_missing":
        return _host_skill_step(
            project_root=project_root,
            skill="arch-design",
            action="run_arch_design",
            message="Architecture design artifacts are missing; run arch-design and import the canonical stage output.",
            expected_input_path=str(stage_input_path(project_root, "arch-design")),
            import_command=stage_import_command(project_root, "arch-design"),
            prompt=_generate_only_prompt(
                f"Use the arch-design skill for project {project_root}.",
                str(stage_input_path(project_root, "arch-design")),
            ),
        )
    if code == "ui_design_missing":
        return _host_skill_step(
            project_root=project_root,
            skill="ui-design",
            action="run_ui_design",
            message="UI design artifacts are required and missing; run ui-design and import the canonical stage output.",
            expected_input_path=str(stage_input_path(project_root, "ui-design")),
            import_command=stage_import_command(project_root, "ui-design"),
            prompt=_generate_only_prompt(
                f"Use the ui-design skill for project {project_root}.",
                str(stage_input_path(project_root, "ui-design")),
            ),
        )
    if code == "context_missing":
        return _host_skill_step(
            project_root=project_root,
            skill="project-context-sync",
            action="run_project_context_sync",
            message="Runtime context artifacts are missing; run project-context-sync and import the canonical stage output.",
            expected_input_path=str(stage_input_path(project_root, "project-context-sync")),
            import_command=stage_import_command(project_root, "project-context-sync"),
            prompt=_generate_only_prompt(
                f"Use the project-context-sync skill for project {project_root}.",
                str(stage_input_path(project_root, "project-context-sync")),
            ),
        )
    if code == "work_items_missing":
        return _host_skill_step(
            project_root=project_root,
            skill="task-decompose",
            action="run_task_decompose",
            message="Canonical work items are missing; run task-decompose and import the canonical task graph.",
            expected_input_path=str(stage_input_path(project_root, "task-decompose")),
            import_command=stage_import_command(project_root, "task-decompose"),
            prompt=_generate_only_prompt(
                f"Use the task-decompose skill for project {project_root}.",
                str(stage_input_path(project_root, "task-decompose")),
            ),
        )
    if code == "work_items_contract_invalid":
        errors = contract_errors or {}
        return _host_skill_step(
            project_root=project_root,
            skill="task-decompose",
            action="repair_task_decompose",
            message="The current task graph violates the canonical work-item contract; regenerate and import a corrected task graph.",
            expected_input_path=str(stage_input_path(project_root, "task-decompose")),
            import_command=stage_import_command(project_root, "task-decompose"),
            prompt=_generate_only_prompt(
                f"Use the task-decompose skill for project {project_root}. The current task graph is invalid with these contract errors: {errors}. Regenerate a corrected task-decompose.json.",
                str(stage_input_path(project_root, "task-decompose")),
            ),
            extra={"contract_errors": errors},
        )
    return None


def build_review_host_step(*, project_root: Path, task_id: str, title: str) -> dict[str, Any]:
    if task_id == "T-FINAL":
        input_path = final_review_input_path(project_root)
        import_command = f'{APP_DELIVERY_BIN} final-review --project "{project_root}" --input "{input_path}"'
        return _host_skill_step(
            project_root=project_root,
            skill="final-review",
            action="run_final_review",
            message="Final verification is waiting for an independent final review artifact.",
            prompt=_generate_only_prompt(
                f"Use the final-review skill for project {project_root}.",
                str(input_path),
            ),
            task_id=task_id,
            expected_input_path=str(input_path),
            import_command=import_command,
        )

    input_path = review_input_path(project_root, task_id)
    import_command = f'{APP_DELIVERY_BIN} code-review --project "{project_root}" --task-id "{task_id}" --input "{input_path}"'
    return _host_skill_step(
        project_root=project_root,
        skill="code-review",
        action="run_code_review",
        message=f"Task {task_id} is waiting for an independent code-review artifact.",
        prompt=_generate_only_prompt(
            f"Use the code-review skill for project {project_root} and task_id {task_id}.",
            str(input_path),
        ),
        task_id=task_id,
        expected_input_path=str(input_path),
        import_command=import_command,
        extra={"task_title": title},
    )
