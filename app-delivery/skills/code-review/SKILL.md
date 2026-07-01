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
   - `findings`: array of finding objects
   - `requirement_assessment`: array of per-requirement verdicts for declared task requirements
   - `acceptance_assessment`: array of per-scenario verdicts for declared task acceptance scenarios
   - `task_contract_assessment`: object that says whether changes are an implementation repair, a local task-contract repair, or an unsafe decomposition-level repair
   - `intent_assessment`: object assessing task intent completion when task intent is present in the review request
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
- Do not approve demo-only behavior for production-scope requirements: state must survive as required, protected operations must use the project's access-control pattern, and claimed integrations must be wired into the owning runtime path unless the task explicitly scopes them as prototype/stub.
- When the task declares requirements, emit one `requirement_assessment` row per declared requirement with `id`, `status`, and `notes`.
- When the task declares acceptance scenarios, emit one `acceptance_assessment` row per declared acceptance scenario with `id`, `status`, and `notes`.
- Do not reject a task only because it touched files outside the original output_paths when those edits are necessary support work or regression fixes for the current task. Judge those edits on correctness and necessity.
- When a task owns a user-visible frontend acceptance scenario, expect browser/e2e evidence in this task unless an explicitly declared downstream validation task owns that exact browser journey.
- If any declared requirement or acceptance scenario is incomplete, contradicted, or only partially implemented, return `status=changes_requested`.
- If a declared acceptance scenario cannot be completed by this task because it belongs to another task or contradicts this task's non-goals, set `task_contract_assessment.recommended_action=task_contract_repair` and provide concrete `operations` instead of asking the implementation task to keep patching.
- Use `repair_task_decompose` only when the work-item graph is structurally invalid. If the target task and follow-up contract are unclear for a single acceptance scenario, omit `to_task_id`/`followup_task`; the framework will record a deferred acceptance scenario and continue delivery instead of re-decomposing the project.
- Use `task_contract_assessment.recommended_action=implementation_repair` for ordinary missing implementation, weak tests, regressions, or other fixable defects inside the task's rightful scope.
- If any finding is blocking, return `status=changes_requested` and set that finding's `severity` to `blocking`.
- If task intent is present and any `done_when` item is missing, set `intent_assessment.status=changes_requested` and return `status=changes_requested`.
- Do not write `status=pass` while also documenting missing behavior, placeholders, stubs, absent UI/API behavior, or unmet done_when items as findings or assessment notes.
- Scope deviations are advisory, not automatic failures. Necessary shared infrastructure, support wiring, or contract-aligned fixes may still be acceptable when they are genuinely required to complete the current task correctly.
- Do not approve changes that are really premature implementation of future tasks, even if the current task's tests pass.
- Do not mutate ledger files directly.
- If the review requests changes, let the core return the task to `pending`; do not hand-edit the task state.

Finding object shape:
```json
{
   "severity": "blocking" | "non_blocking",
   "requirement_ids": ["REQ-001"],
   "acceptance_ids": ["AS-001"],
   "message": "Concrete review finding."
}
```

`requirement_ids` and `acceptance_ids` may be empty arrays when a finding is not tied to a specific ID.

Intent assessment shape:
```json
{
   "status": "pass" | "changes_requested",
   "missing_done_when": ["done_when item not satisfied"],
   "violated_non_goals": ["non-goal that was violated"],
   "notes": "Short explanation"
}
```

Task contract assessment shape:
```json
{
   "status": "pass" | "changes_requested",
   "issue_type": "implementation" | "task_contract",
   "recommended_action": "implementation_repair" | "task_contract_repair" | "repair_task_decompose",
   "operations": [
      {
         "op": "move_acceptance_scenario",
         "id": "AS-001",
         "from_task": "T004",
         "to_task_id": "T009",
         "to_task_hint": "task owning logistics planner agent orchestration",
         "followup_task": {
            "title": "Acceptance Scenario Follow-up: AS-001",
            "requirements": ["REQ-001"],
            "output_tests": ["backend/tests/scenarios/test_as_001.py"],
            "output_paths": ["backend/src/scenarios/as_001.py"]
         },
         "rationale": "Why this AS belongs outside the current task"
      }
   ],
   "affected_requirement_ids": ["REQ-001"],
   "affected_acceptance_ids": ["AS-001"],
   "notes": "Short explanation"
}
```

Operation rules:
- Prefer `to_task_id` only when the target task is explicitly identifiable and not already verified.
- Use `to_task_hint` only as explanatory context; the framework will not guess a target task from it.
- Include `followup_task` with non-empty `output_tests` and `output_paths` when no existing unverified task safely owns the scenario but a minimal follow-up task is clear.
- Do not use task-contract repair for missing code, missing tests, placeholder implementation, or weak validation inside the current task's rightful scope; use `implementation_repair` for those.

Use empty arrays for `findings`, `requirement_assessment`, and `acceptance_assessment` when there are no rows. Always include `task_contract_assessment`; omit `intent_assessment` only when the review request has no task intent section.
