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
3. Produce raw JSON that follows the review request and [references/review-contract.md](./references/review-contract.md).
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
- Treat the review-request file as the primary task brief.
- Treat [references/review-contract.md](./references/review-contract.md) as the single source of truth for verdicts, JSON shape, assessment rows, task-contract repair, and scope rules.
- Do not mutate ledger files directly.
- If the review requests changes, let the core return the task to `pending`; do not hand-edit task state.
