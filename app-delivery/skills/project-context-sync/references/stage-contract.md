Generate project context files from the architecture, requirements, and architecture metadata below.

Return JSON with these fields:
- claude_md
- agents_md
- test_plan

test_plan must be an object with:
- schema_version
- coverage: array of objects with requirement_id, test_types, suite

Constraints:
- `claude_md` is only for Claude-family runtimes.
- `agents_md` is only for OpenCode-family runtimes.
- The harness will select exactly one runtime-specific context file based on the project's configured runtime.
- `claude_md` / `agents_md` must be written in English.
- `claude_md` / `agents_md` should be short project context, not a second task prompt.
- Include a brief product/domain summary and a short `Tech Design` section with project-wide stack choices and architecture constraints.
- Preserve explicit technology choices from requirements, architecture, and project technology constraints in `Tech Design`.
- Keep context stable; omit feature-local details.
- Prefer a compact bullet list or short table in `Tech Design` rather than long prose.
- Do not duplicate generic runtime rules, task instructions, command policy, long repository trees, or feature acceptance details.
- Keep code paths consistent with the python-react stack contract: backend Python code under `backend/src/...`, frontend code under `frontend/src/...`, browser tests under `frontend/e2e/...`, and mock services under project-root `mock-server/...`. Do not introduce alternative package roots, service roots, mock-service roots, or layout guidance unless the project has explicitly selected a different stack/template that supports them.
- Do not invent constraints that are not grounded in the provided requirements, architecture, shared components, or clarification answers.
- test_plan coverage rows must include every requirement ID.
- test_types should be realistic, such as unit, api, integration, browser, e2e, accessibility, or performance.

Architecture metadata:
{{architecture_meta_json}}

Architecture:
{{architecture_md}}

Requirements JSON:
{{requirements_json}}

Project technology constraints:
{{dependency_hints_json}}

Shared components:
{{shared_components_md}}

Clarification answers:
{{clarification_answers_md}}