Create UI design reference documents for this project.

Return JSON with these markdown fields:
- template_selection_md
- design_system_md
- page_archetypes_md
- states_md

Constraints:
- These documents are design references only, not implementation code.
- The template source of truth is the template-pack library under `/Users/hzg/apps/agents/m-opc/temp/hermes-skills/ui-design/references/`.
- Start by evaluating the available template packs there, especially `default`, `enterprise-console`, and `ai-workspace`, using each pack's `manifest.yaml`, `notes.md`, `style-contract.json`, and screenshot descriptions.
- `template_selection_md` must name the selected template pack id exactly, explain why it fits, and state what it optimizes for.
- `design_system_md` must stay consistent with the chosen pack's style contract and notes.
- Choose a coherent UI direction that fits the product domain.
- Page archetypes should map to major user workflows.
- States should cover loading, empty, error, permission, and destructive confirmation states.

Requirements JSON:
{{requirements_json}}

Architecture:
{{architecture_md}}

Shared components:
{{shared_components_md}}

Clarification answers:
{{clarification_answers_md}}