Design the project architecture from the structured requirements JSON below.

Return JSON with these fields:
- architecture_md: top-level architecture overview in markdown
- shared_components_md: shared backend/frontend components that should be implemented before feature slices
- modules: array of objects with path and content for module design docs under docs/
- adrs: array of objects with path and content for architecture decisions under docs/
- ui_required: boolean indicating whether this project needs a user interface

Constraints:
- Choose a pragmatic, production-appropriate architecture.
- Adopt explicit technology, framework, protocol, runtime, infrastructure, and component choices from the requirements. If you replace one, record the reason in an ADR.
- Adopt the project technology constraints below. Name required project-wide choices in `architecture_md` Tech Design; do not leave them only in requirements prose.
- Keep module boundaries explicit.
- `modules` must contain module design docs only: responsibilities, public interfaces, data ownership, integration points, and tests. Do not put ADRs in `modules`.
- `adrs` must contain decision records only: context, decision, consequences, and alternatives.
- `architecture_md` must include `Module Architecture`: a fenced repository tree plus concise responsibilities for major code areas.
- The fenced tree is the T000 scaffold contract. List directories, durable empty-directory placeholders, and minimal scaffold/config files only. Do not inventory implementation source files; keep concrete source files to rare entrypoints that T000 must create.
- For the python-react stack, backend Python packages live under `backend/src/...`. Put project-specific modules such as API routes, agents, orchestration, MCP adapters, simulation, models, and core code under that source root. Do not place Python packages outside that backend source root or invent additional service roots unless the selected stack/template explicitly supports them. Implement production FastMCP code under `backend/src/...` or simulated tool services under project-root `mock-server/...`.
- Keep the skeleton project-specific and minimal. T000 owns this structural skeleton; T001 should only implement true shared foundation code that multiple feature slices need, not compensate for missing architecture structure.
- If a UI is required, make that clear in the architecture and set ui_required=true.
- Shared components should only include code that is reused across multiple modules.

Requirements JSON:
{{requirements_json}}

Project technology constraints:
{{dependency_hints_json}}

Clarification answers:
{{clarification_answers_md}}