---
name: task-decompose
description: Generate canonical work-items for app-delivery execution. Use when start reports work_items_missing or when planning artifacts are ready for task graph generation.
---

Use this skill after requirements, architecture, UI design, and context sync are in place.

Inputs:
- `project`: app-delivery project root

Procedure:
1. Ensure `docs/requirements.json` and `docs/architecture.md` exist.
2. Preserve these decomposition rules:
	- start from whole product capability, then break into vertical slices
	- use the smallest coherent feature outcome; avoid layer-only fragments and oversized bundles
	- tasks may cross backend, frontend, mock-server, shared code, and tests
	- every requirement must be covered by at least one task
	- every explicit project-wide technology choice must be owned by at least one task
	- user-visible frontend scenarios should usually ship with browser evidence in the owning feature task unless a named validation task explicitly owns that same journey
	- dependencies should form a usable DAG, not an arbitrary serial list
	- T000, optional T001, and T-FINAL are inserted by the framework
3. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and draft the task graph.
4. Before writing JSON, do a task-graph self-review in the host reasoning layer:
	- each `feature` task should answer: what product capability is usable after this task is complete?
	- if a task is only scaffold, shared wiring, base contracts, generic models, app shell, or fixtures, do not let it claim full user-facing requirement coverage; attach that requirement to the first coherent feature that proves it
	- if a feature was split, each resulting task must still be implementable and useful
	- if merging two tasks would create a broad, unfocused implementation session, keep them separate; if splitting a task would scatter one feature across many handoff points, keep it together
	- if one task combines multiple independently testable capability surfaces, split by workflow, sub-capability, or bounded context unless `intent.split_justification` explains why it is one coherent vertical slice
	- each `done_when` item should be covered by task-owned `output_tests` or an explicit validation task dependency; if coverage is partial, refine the boundary or add focused tests before import
	- use `docs/requirements-source.md` only as supporting evidence when canonical requirements do not provide enough detail to assign task boundaries, acceptance ownership, technology ownership, or dependency order; do not create or reinterpret canonical requirement IDs from the raw source
5. Produce a JSON object with all of these top-level fields:
	- `items`: the work-item draft array
	- `delivery_complexity`: a coarse project classification (`S`, `M`, `L`, or `XL`) with short rationale and rough signals
	- `validation_gates`: module/milestone/release gate suggestions for the framework to persist in `docs/gates.json`
6. If the project is `M` or higher, `items` must include one or more validation-focused work items with `task_kind: "validation"`. These are ordinary execution tasks and should be planned with correct dependencies just like feature tasks.
7. Validation tasks must depend on the feature tasks they validate. Do not put them before their owning feature slices. Keep the DAG explicit and minimal.
8. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/task-decompose.json`.
9. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery decompose --project {project} --input {project}/.app-delivery-runtime/stage-inputs/task-decompose.json
```
10. If the harness reports invalid JSON, missing requirement coverage, unresolved dependencies, dependency cycles, oversized tasks, or invalid task contracts, feed the exact error details back into this skill reasoning and regenerate the stage JSON. Do not manually patch `docs/work-items.json`; repair the stage input and re-import it.
11. Verify the canonical task ledger:
```bash
test -f {project}/docs/work-items.json
test -f {project}/docs/work-items.md
```
12. Confirm the graph includes built-ins (`T000`, optional `T001`, `T-FINAL`) and uses only task IDs in `dependencies`.

Outputs:
- `{project}/docs/work-items.json`
- `{project}/docs/work-items.md`
- `{project}/docs/gates.json`

Stop conditions:
- dependency normalization failed
- requirement coverage check failed
- dependency cycle detected
- task graph self-review cannot identify a coherent product capability for one or more `feature` tasks
- work-items were not written

