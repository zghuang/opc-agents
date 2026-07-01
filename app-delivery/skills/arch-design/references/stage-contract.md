Design the project architecture from the structured requirements JSON below.

Return JSON with these fields:
- architecture_md: top-level architecture overview in markdown
- shared_components_md: shared backend/frontend components that should be implemented before feature slices
- modules: array of objects with path and content for module design docs under docs/
- adrs: array of objects with path and content for architecture decisions under docs/
- ui_required: boolean indicating whether this project needs a user interface

Constraints:
- Choose a pragmatic, production-appropriate architecture.
- Preserve explicit technology, framework, protocol, runtime, infrastructure, and component choices from the requirements. Treat template defaults as non-authoritative hints, not architecture decisions.
- Keep module boundaries explicit.
- architecture_md must include a dedicated `Module Architecture` section that contains a fenced repository/module tree and a concise explanation of the major code areas. This section is mandatory even when separate `docs/modules/*` files are also provided.
- The fenced Module Architecture tree is also the T000 scaffold skeleton contract. Include the directories that should exist before implementation tasks run, plus only lightweight placeholders needed to make empty directories durable (`__init__.py` for Python packages, `.gitkeep` for non-Python leaf directories). Do not list implementation files merely to force a generic pattern such as CRUD modules, services, repositories, or ORM models; include those only when this project's requirements and architecture actually call for them.
- For the python-react stack, backend Python packages live under `backend/src/...`. Put project-specific modules such as API routes, agents, orchestration, MCP adapters, simulation, models, and core code under that source root. Do not place Python packages outside that backend source root or invent additional service roots unless the selected stack/template explicitly supports them. Implement production FastMCP code under `backend/src/...` or simulated tool services under project-root `mock-server/...`.
- Keep the skeleton project-specific and minimal. T000 owns this structural skeleton; T001 should only implement true shared foundation code that multiple feature slices need, not compensate for missing architecture structure.
- If a UI is required, make that clear in the architecture and set ui_required=true.
- Shared components should only include code that is reused across multiple modules.

Requirements JSON:
{{requirements_json}}

Clarification answers:
{{clarification_answers_md}}