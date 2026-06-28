---
name: code-review
description: Perform an external independent review for a review-pending work item and import the result into app-delivery.
---

Use this skill when `app-delivery status` reports `review_pending_task` for a normal task.

Inputs:
- `project`: app-delivery project root
- `task_id`: review-pending task id

Procedure:
1. Read `{project}/.app-delivery-runtime/review-requests/code-review-{task_id}.md`.
2. Inspect the current repository state and the task-scoped diff.
3. Produce raw JSON with fields:
   - `status`: `pass` or `changes_requested`
   - `summary`: short review verdict
   - `findings`: array of strings
   - `requirement_assessment`: array of per-requirement verdicts for declared task requirements
   - `acceptance_assessment`: array of per-scenario verdicts for declared task acceptance scenarios
4. Write that JSON to `{project}/.app-delivery-runtime/review-inputs/code-review-{task_id}.json`.
5. Import it through the core:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery code-review --project {project} --task-id {task_id} --input {project}/.app-delivery-runtime/review-inputs/code-review-{task_id}.json
```

6. Re-run status:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery status --project {project}
```

Rules:
- This is an external independent review. Do not reuse the implementation rationale as the review verdict.
- Treat the review-request file as the primary review brief. Apply its requirement, acceptance, and scope guidance during the review itself.
- Passing tests are necessary but not sufficient. Judge whether the declared requirements and acceptance scenarios are actually complete in behavior.
- When the task declares requirements, emit one `requirement_assessment` row per declared requirement with `id`, `status`, and `notes`.
- When the task declares acceptance scenarios, emit one `acceptance_assessment` row per declared acceptance scenario with `id`, `status`, and `notes`.
- Do not reject a task only because it touched files outside the original output_paths when those edits are necessary support work or regression fixes for the current task. Judge those edits on correctness and necessity.
- When a task owns a user-visible frontend acceptance scenario, expect browser/e2e evidence in this task unless an explicitly declared downstream validation task owns that exact browser journey.
- If any declared requirement or acceptance scenario is incomplete, contradicted, or only partially implemented, return `status=changes_requested`.
- Scope deviations are advisory, not automatic failures. Necessary shared infrastructure, support wiring, or contract-aligned fixes may still be acceptable when they are genuinely required to complete the current task correctly.
- Do not approve changes that are really premature implementation of future tasks, even if the current task's tests pass.
- Do not mutate ledger files directly.
- If the review requests changes, let the core return the task to `pending`; do not hand-edit the task state.
