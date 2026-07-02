---
name: spec-review
description: Normalize raw requirements into docs/requirements.json for app-delivery. Use when start reports requirements_missing or when a raw requirements document must be converted into stage input JSON.
---

Use this skill when raw requirements need canonical structured requirements before architecture design. Treat the source as evidence, not as normalized schema.

Inputs:
- `project`: app-delivery project root
- `requirements_path`: raw requirements document path

Procedure:
1. Confirm the project was initialized and the requirements file exists.
2. Run deterministic preflight if needed:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery-preflight.sh --project {project} --requirements {requirements_path}
```
3. Normalize the raw source into canonical requirement slices:
	- preserve a source REQ/NFR ID as the canonical ID only when that source item is already actionable enough for architecture, implementation, and validation
	- split source IDs that contain multiple independent behaviors, workflows, roles, integrations, state changes, or acceptance signals into multiple canonical requirements and keep the original IDs in `source_requirement_ids`
	- merge candidate requirements that are only fields, UI fragments, endpoint fragments, tests, or implementation steps of one behavior
	- otherwise generate stable canonical IDs in document order using only `REQ-###` for functional/business requirements and `NFR-###` for non-functional requirements
	- do not invent domain-specific canonical ID prefixes; put domain labels in title, summary, or source traceability instead
	- extract explicit functional requirements, constraints, roles, data/security/ops rules, external dependencies, and acceptance signals
	- apply the coarse-vs-actionable requirement and C1/C2/C3 clarification rules from the stage contract before continuing
	- preserve explicitly named technologies as requirements or constraints, and emit matching `technology_hints`
	- if the source includes explicit or implicit user stories, use cases, user journeys, approval flows, degradation/recovery flows, or end-to-end operational scenarios, convert them into structured `acceptance_scenarios` instead of leaving them only in prose
4. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object with `requirements`, `acceptance_scenarios`, `clarifications`, and `source_requirements_path`.
	- `technology_hints` preserve the exact named choice, rationale, and evidence. Do not guess package coordinates.
	- Set `source_requirements_path` to the same absolute raw requirements path provided to this skill.
	- If the project already has clarification answers from an earlier spec-review pass, fold those resolved answers back into the canonical requirement interpretation instead of treating clarification resolution as a separate external planning stage.
	- When a clarification still genuinely requires a user decision, include a concise `recommended_answer` and a small `answer_options` list when defensible so the host can offer suggested choices without blocking freeform user input.
5. If `docs/clarification-needed.md` contains blocking questions from an earlier pass, inspect whether the current source requirements, existing project artifacts, or framework-selected defaults already support defensible answers.
	- When they do, write or update `docs/clarification-answers.md` with concise answers before regenerating the canonical JSON.
	- Do not invent answers that require a genuinely new product, security, architecture, or scope decision from the user.
6. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/spec-review.json`.
7. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery spec-review --project {project} --input {project}/.app-delivery-runtime/stage-inputs/spec-review.json
```
8. Verify the output exists:
```bash
test -f {project}/docs/requirements.json
```
9. If `{project}/docs/clarification-needed.md` is written and contains blocking questions that are still genuinely unresolved, stop before arch-design.

Outputs:
- `{project}/docs/requirements.json`
- `{project}/docs/clarification-needed.md` when contradictions or ambiguity require clarification

Review rules:
- Keep explicit technology choices visible for architecture and implementation.
- Convert source journeys and flows into `acceptance_scenarios`.
- Turn architecture-blocking ambiguity into `C1` clarifications.
- Do not assume the source is already structured just because it contains numbered bullets.

Stop conditions:
- project path is missing
- requirements path is missing
- runtime command is unavailable
- `{project}/docs/requirements.json` was not written
- blocking clarifications were emitted

