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
	- tasks may cross backend, frontend, mock-server, shared code, and tests
	- avoid layer-only tasks
	- every requirement must be covered by at least one task
	- dependencies should form a usable DAG, not an arbitrary serial list
	- T000, optional T001, and T-FINAL are inserted by the framework
3. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object.
4. The JSON object must contain all of these top-level fields:
	- `items`: the work-item draft array
	- `delivery_complexity`: a coarse project classification (`S`, `M`, `L`, or `XL`) with short rationale and rough signals
	- `validation_gates`: module/milestone/release gate suggestions for the framework to persist in `docs/gates.json`
5. If the project is `M` or higher, `items` must include one or more validation-focused work items with `task_kind: "validation"`. These are ordinary execution tasks and should be planned with correct dependencies just like feature tasks.
6. Validation tasks must depend on the feature tasks they validate. Do not put them before their owning feature slices. Keep the DAG explicit and minimal.
7. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/task-decompose.json`.
8. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery decompose --project {project} --input {project}/.app-delivery-runtime/stage-inputs/task-decompose.json
```
9. Verify the canonical task ledger:
```bash
test -f {project}/docs/work-items.json
test -f {project}/docs/work-items.md
```
10. Confirm the graph includes built-ins (`T000`, optional `T001`, `T-FINAL`) and uses only task IDs in `dependencies`.

Outputs:
- `{project}/docs/work-items.json`
- `{project}/docs/work-items.md`
- `{project}/docs/gates.json`

Stop conditions:
- dependency normalization failed
- requirement coverage check failed
- dependency cycle detected
- work-items were not written

