---
name: project-context-sync
description: Refresh runtime-specific context, CODE_MAP.md, and test-plan.json from source docs. Use when start reports context_missing or after architecture and UI references are ready.
---

Use this skill after `arch-design`, and after `ui-design` when `ui_required=true`.

Inputs:
- `project`: app-delivery project root

Procedure:
1. Confirm architecture artifacts are present.
2. If UI is required, ensure `docs/ui/*` has been generated first.
3. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object containing `claude_md`, `agents_md`, `code_map_md`, and `test_plan`.
	- Keep `claude_md` / `agents_md` concise and project-specific.
	- Include the project-level knowledge that every runtime session should know up front: product/domain summary, key working rules, code-organization guidance, testing expectations, and critical domain constraints.
	- Do not spend tokens restating generic framework/runtime rules that are constant across projects; the harness injects that baseline automatically.
	- In `code_map_md`, do not hand-write a repository root tree. Focus on stable navigation guidance such as major module responsibilities, extension points, and where new code should go. The harness injects the current filesystem snapshot and the planned module tree automatically.
4. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/project-context-sync.json`.
5. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery context-sync --project {project} --input {project}/.app-delivery-runtime/stage-inputs/project-context-sync.json
```
6. Verify the outputs:
```bash
test -f {project}/CLAUDE.md || test -f {project}/AGENTS.md
test -f {project}/CODE_MAP.md
test -f {project}/docs/test-plan.json
```

Outputs:
- `{project}/CLAUDE.md` or `{project}/AGENTS.md` depending on runtime
- `{project}/CODE_MAP.md`
- `{project}/docs/test-plan.json`

Stop conditions:
- architecture docs missing
- required UI docs missing for a UI project
- derived context files were not written

