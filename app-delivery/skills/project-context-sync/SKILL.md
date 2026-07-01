---
name: project-context-sync
description: Refresh runtime-specific context and test-plan.json from source docs. Use when start reports context_missing or after architecture and UI references are ready.
---

Use this skill after `arch-design`, and after `ui-design` when `ui_required=true`.

Inputs:
- `project`: app-delivery project root

Procedure:
1. Confirm architecture artifacts are present.
2. If UI is required, ensure `docs/ui/*` has been generated first.
3. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object containing `claude_md`, `agents_md`, and `test_plan`.
	- Keep `claude_md` / `agents_md` concise, project-specific, and focused on stable architecture context.
4. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/project-context-sync.json`.
5. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery context-sync --project {project} --input {project}/.app-delivery-runtime/stage-inputs/project-context-sync.json
```
6. Verify the outputs:
```bash
test -f {project}/CLAUDE.md || test -f {project}/AGENTS.md
test -f {project}/docs/test-plan.json
```

Outputs:
- `{project}/CLAUDE.md` or `{project}/AGENTS.md` depending on runtime
- `{project}/docs/test-plan.json`

Stop conditions:
- architecture docs missing
- required UI docs missing for a UI project
- derived context files were not written

