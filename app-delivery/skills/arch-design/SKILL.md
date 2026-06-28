---
name: arch-design
description: Generate project architecture, module docs, ADRs, and architecture metadata. Use when start reports architecture_missing or after requirements were normalized.
---

Use this skill after `spec-review` produced `docs/requirements.json`.

Inputs:
- `project`: app-delivery project root

Procedure:
1. Ensure `docs/requirements.json` exists.
2. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object containing architecture markdown, shared components, ADR/module docs, and `ui_required`.
3. `architecture_md` must always include a dedicated `Module Architecture` section that shows the intended repository/module tree in a fenced code block and explains the responsibility of the major directories or modules. Do not rely on `docs/modules/*` alone for this; the top-level `docs/architecture.md` must contain the codebase structure summary directly.
4. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/arch-design.json`.
5. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery arch-design --project {project} --input {project}/.app-delivery-runtime/stage-inputs/arch-design.json
```
6. Verify the generated artifacts:
```bash
test -f {project}/docs/architecture.md
test -f {project}/docs/shared-components.md
test -f {project}/docs/architecture-meta.json
```
7. If `docs/architecture-meta.json` says `ui_required=true`, the next stage must include `ui-design` before `context-sync` and `start`.

Outputs:
- `{project}/docs/architecture.md`
- `{project}/docs/shared-components.md`
- `{project}/docs/architecture-meta.json`
- `{project}/docs/adrs/*` or `{project}/docs/architecture/*`

Stop conditions:
- requirements were not normalized first
- architecture files were not written

