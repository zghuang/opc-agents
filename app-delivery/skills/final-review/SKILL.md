---
name: final-review
description: Perform the external final release review for T-FINAL and import the result into app-delivery.
---

Use this skill when `app-delivery verify` or `app-delivery status` reports `review_pending_task.id == T-FINAL`.

Inputs:
- `project`: app-delivery project root

Procedure:
1. Read `{project}/.app-delivery-runtime/review-requests/final-review.md`.
2. If `{project}/.app-delivery-runtime/host-handoff.json` exists and has matching `skill="final-review"` and `status="waiting_for_host"`, update only these fields before long review work: `status="running"`, `running_at=<current UTC ISO time>`, and `updated_at=<same timestamp>`. Do not change task state or import fields; the core will mark it `imported` after import.
3. Inspect the repository state, release evidence, and current final verification outputs.
4. Produce raw JSON with fields:
   - `status`: `pass` or `changes_requested`
   - `summary`: short release verdict
   - `findings`: array of finding objects
5. Write that JSON to `{project}/.app-delivery-runtime/review-inputs/final-review.json`.
6. Import it through the core:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery final-review --project {project} --input {project}/.app-delivery-runtime/review-inputs/final-review.json
```

7. Re-run verification/status if needed:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery verify --project {project}
${OPC_HOME:-$HOME/opc}/bin/app-delivery status --project {project}
```

Rules:
- Treat this as an independent release gate, not as implementation continuation.
- Do not edit ledger files directly.
- If the review fails, let the core keep `T-FINAL` blocked/review-driven; do not bypass the gate manually.

Finding object shape:
```json
{
   "severity": "blocking" | "non_blocking",
   "requirement_ids": ["REQ-001"],
   "acceptance_ids": ["AS-001"],
   "message": "Concrete release finding."
}
```

Use an empty `findings` array when there are no findings. `requirement_ids` and `acceptance_ids` may be empty arrays when a finding is not tied to a specific ID.