# Code Review Contract

Use this contract for every code-review artifact.

Verdict rules:
- Return raw JSON only: `status`, `summary`, `findings`, `requirement_assessment`, `acceptance_assessment`, `task_contract_assessment`, `technology_assessment` when technology constraints are present, and `intent_assessment` when task intent is present.
- Use `status=pass` only when the task is acceptable as-is: no blocking findings, all declared requirements pass, all declared acceptance scenarios pass, and intent passes when present.
- Green tests are not enough; implemented behavior and evidence must satisfy the declared IDs.
- Blocking findings require `status=changes_requested`.

Assessment rules:
- For every declared requirement, include exactly one `requirement_assessment` row with `id`, `status`, and `notes`.
- For every declared acceptance scenario, include exactly one `acceptance_assessment` row with `id`, `status`, and `notes`.
- Use empty arrays when no rows exist.
- Use task intent only for purpose, `done_when`, and `non_goals`; ID assessments stay tied to declared requirement and acceptance IDs.
- If task intent is present, include `intent_assessment`. Missing `done_when` items or violated `non_goals` require `changes_requested`.

Task-contract rules:
- Use `implementation_repair` for missing implementation, missing tests, weak validation, placeholders, stubs, regressions, or weak proof inside the task's rightful scope.
- Use `task_contract_repair` only when a declared acceptance scenario belongs to another task.
- Use `repair_task_decompose` only for structural task-graph defects that local operations cannot repair.
- `move_acceptance_scenario.id` must be one of: {{declared_acceptance_scenarios}}.
- Never use `done_when`, requirement, finding, or invented IDs as operation IDs.

Technology rules:
- If the review request lists technology constraints, include exactly one `technology_assessment` row per listed technology with `name`, `status`, `evidence`, and `notes`.
- For `must_use` constraints, `status=pass` requires concrete implementation evidence such as dependency manifest entries, source imports/API usage, configuration, generated artifacts, or behavior-specific implementation evidence.
- If a `must_use` technology is absent, hand-rolled instead, only mentioned in docs/tests, or replaced without a superseding ADR/clarification, return `status=changes_requested` and include a blocking finding.
- Do not accept functional similarity as technology compliance. A custom implementation satisfies a `must_use` constraint only when it uses the named technology or cites an explicit superseding ADR/clarification.

Scope rules:
- Extra paths are not failures when necessary for this task; judge correctness and necessity.
- Scope deviations are advisory, not automatic failures.
- Production-scope behavior must not rely on demo constants, hardcoded responses, in-memory state, placeholder tests, or mock-only browser proof unless explicitly allowed.