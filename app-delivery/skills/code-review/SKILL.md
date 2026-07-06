---
name: code-review
description: Review-runner contract for an external independent review of a review-pending work item and import the result into app-delivery.
---

Use this skill when invoked by the app-delivery framework review runner, or for an explicit manual review override. During normal implementation flow the Hermes main session should not call this skill directly; the framework review runner starts an isolated oneshot and uses this contract.

Inputs:
- `project`: app-delivery project root
- `task_id`: review-pending task id

Procedure:
1. Read `{project}/.app-delivery-runtime/review-requests/code-review-{task_id}.md`.
2. Do not edit `{project}/.app-delivery-runtime/host-handoff.json` directly. The framework marks code-review handoffs as `review_running` before invoking the review runner and marks them `imported` after import.
3. Inspect the current repository state and the task-scoped diff.
4. Produce raw JSON that follows the review request and [references/review-contract.md](./references/review-contract.md).
5. Write that JSON to `{project}/.app-delivery-runtime/review-inputs/code-review-{task_id}.json`.
6. Import it through the core:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery code-review --project {project} --task-id {task_id} --input {project}/.app-delivery-runtime/review-inputs/code-review-{task_id}.json
```

7. Re-run status:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery status --project {project}
```

Rules:
- Treat the review-request file as the primary task brief.
- Treat [references/review-contract.md](./references/review-contract.md) as the single source of truth for verdicts, JSON shape, assessment rows, task-contract repair, and scope rules.
- Do not rely on Hermes main-session context. Review only the project state, the review request artifact, and task-scoped evidence.
- Do not mutate ledger files directly.
- If the review requests changes, let the core return the task to `pending`; do not hand-edit task state.
