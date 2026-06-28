from __future__ import annotations

from pathlib import Path
from typing import Any

from .loop_review import final_review_input_path, review_input_path
from .stage_harness import stage_import_command, stage_input_path


def _generate_only_prompt(prefix: str, expected_input_path: str) -> str:
    return (
        f"{prefix} "
        f"Write the canonical JSON artifact to {expected_input_path}. "
        "Do not run the app-delivery import command yourself; the framework will import it automatically. "
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
    payload: dict[str, Any] = {
        "kind": "host_skill",
        "owner": "host",
        "skill": skill,
        "action": action,
        "message": message,
        "prompt": prompt,
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
        return _host_skill_step(
            project_root=project_root,
            skill="spec-review",
            action="repair_spec_review",
            message="Blocking clarifications remain; regenerate the canonical spec-review output so the clarified interpretation is embedded in the imported JSON.",
            expected_input_path=str(stage_input_path(project_root, "spec-review")),
            import_command=stage_import_command(project_root, "spec-review"),
            prompt=_generate_only_prompt(
                f"Use the spec-review skill for project {project_root} with requirements_path {req_path}. The project currently has blocking clarifications in docs/clarification-needed.md. Regenerate a corrected spec-review.json using the current project evidence.",
                str(stage_input_path(project_root, "spec-review")),
            ),
            extra={"requirements_path": req_path},
        )
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
        import_command = f'app-delivery final-review --project "{project_root}" --input "{input_path}"'
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
    import_command = f'app-delivery code-review --project "{project_root}" --task-id "{task_id}" --input "{input_path}"'
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
