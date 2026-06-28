Design the project architecture from the structured requirements JSON below.

Return JSON with these fields:
- architecture_md: top-level architecture overview in markdown
- shared_components_md: shared backend/frontend components that should be implemented before feature slices
- modules: array of objects with path and content for module design docs under docs/
- adrs: array of objects with path and content for architecture decisions under docs/
- ui_required: boolean indicating whether this project needs a user interface

Constraints:
- Choose a pragmatic, production-appropriate architecture.
- Keep module boundaries explicit.
- architecture_md must include a dedicated `Module Architecture` section that contains a fenced repository/module tree and a concise explanation of the major code areas. This section is mandatory even when separate `docs/modules/*` files are also provided.
- If a UI is required, make that clear in the architecture and set ui_required=true.
- Shared components should only include code that is reused across multiple modules.

Requirements JSON:
{{requirements_json}}

Clarification answers:
{{clarification_answers_md}}