---
name: spec-review
description: Normalize raw requirements into docs/requirements.json for app-delivery. Use when start reports requirements_missing or when a raw requirements document must be converted into stage input JSON.
---

Use this skill when a project has a raw requirements document and needs canonical structured requirements before architecture design.

The source requirements may be an informal PRD, issue list, workshop transcript, user story collection, use-case description, journey narrative, mixed Chinese/English notes, or other non-canonical material. Treat the source as evidence, not as already-normalized schema.

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
	- preserve stable IDs when the source already has defensible IDs
	- otherwise generate stable IDs in document order
	- extract explicit functional requirements, constraints, roles, data/security/ops rules, external dependencies, and acceptance signals
	- apply the coarse-vs-actionable requirement and C1/C2/C3 clarification rules from the stage contract before continuing
	- if the source names a specific runtime, framework, package, library, infrastructure component, protocol, or integration contract, preserve that as a requirement or implementation constraint instead of dropping it
	- if the source includes explicit or implicit user stories, use cases, user journeys, approval flows, degradation/recovery flows, or end-to-end operational scenarios, convert them into structured `acceptance_scenarios` instead of leaving them only in prose
4. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object with `requirements`, `acceptance_scenarios`, `clarifications`, and `source_requirements_path`.
	- When the source explicitly selects technologies, frameworks, SDKs, component libraries, protocols, or infrastructure building blocks, also emit `technology_hints` so later tasks can preserve those choices without reinventing them.
	- `technology_hints` should preserve the named technology choice and rationale; do not fabricate exact package names when the source only names a framework family.
	- Set `source_requirements_path` to the same absolute raw requirements path provided to this skill.
	- If the project already has clarification answers from an earlier spec-review pass, fold those resolved answers back into the canonical requirement interpretation instead of treating clarification resolution as a separate external planning stage.
4. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/spec-review.json`.
5. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery spec-review --project {project} --input {project}/.app-delivery-runtime/stage-inputs/spec-review.json
```
6. Verify the output exists:
```bash
test -f {project}/docs/requirements.json
```
7. If `{project}/docs/clarification-needed.md` is written and contains blocking questions that are still genuinely unresolved, stop before arch-design.

Outputs:
- `{project}/docs/requirements.json`
- `{project}/docs/clarification-needed.md` when contradictions or ambiguity require clarification

Review rules:
- If the source explicitly constrains framework/library/runtime/package choices, keep them visible for later architecture and implementation.
- If the source contains explicit user stories, use cases, user journeys, or multi-step flows, treat them as acceptance material and represent them in `acceptance_scenarios`.
- Do not hide contradictions or under-specified scope. Turn architecture-blocking ambiguity into `C1` clarifications.
- Use the detailed C1/C2/C3 severity criteria from the stage contract.
- Do not assume the source is already structured just because it contains numbered bullets.

Stop conditions:
- project path is missing
- requirements path is missing
- runtime command is unavailable
- `{project}/docs/requirements.json` was not written
- blocking clarifications were emitted

