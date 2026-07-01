---
name: ui-design
description: Generate visual reference docs when architecture requires a UI. Use when start reports ui_design_missing or when architecture metadata says ui_required=true.
---

Use this skill only when the architecture metadata marks the project as UI-bearing, or when the operator explicitly requests UI reference generation.

Inputs:
- `project`: app-delivery project root
- `ui_template`: optional explicit template id

Procedure:
1. Check `docs/architecture-meta.json` and confirm `ui_required=true`.
2. Read the local template packs and policies from this skill's own `references/` directory.
3. Use the local packs as the source of truth for template selection and style vocabulary.
4. Load the stage contract from [references/stage-contract.md](./references/stage-contract.md), replace placeholders with the actual project inputs, and produce a JSON object containing the four UI markdown documents.
5. The produced `template_selection_md` must explicitly name which template pack was selected and why.
6. The produced `design_system_md` must align with the selected pack's `style-contract.json` and notes.
7. Write that JSON to `${project}/.app-delivery-runtime/stage-inputs/ui-design.json`.
8. Import the stage result through the harness:
```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery ui-design --project {project} --input {project}/.app-delivery-runtime/stage-inputs/ui-design.json
```
9. Verify the generated docs:
```bash
test -f {project}/docs/ui/template-selection.md
test -f {project}/docs/ui/design-system.md
test -f {project}/docs/ui/page-archetypes.md
test -f {project}/docs/ui/states.md
```
10. Confirm `template-selection.md` names the chosen UI template/reference direction and why it fits the product domain.

Outputs:
- `{project}/docs/ui/template-selection.md`
- `{project}/docs/ui/design-system.md`
- `{project}/docs/ui/page-archetypes.md`
- `{project}/docs/ui/states.md`

Stop conditions:
- architecture metadata missing
- ui_required is false and no override was requested
- any required UI docs were not written

