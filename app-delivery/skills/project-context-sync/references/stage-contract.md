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
- `claude_md` / `agents_md` should be short project-specific context, not a second task prompt.
- Include only: a brief product/domain summary and a short `Tech Design` section that captures the high-level stack, major platform choices, and cross-cutting architecture constraints that apply across many tasks.
- Keep project context high-signal and stable. Prefer guidance that remains useful across the whole project rather than feature-local details.
- Prefer a compact bullet list or short table in `Tech Design` rather than long prose.
- Fold project-wide invariants or collaboration conventions into `Tech Design` when they are genuinely cross-cutting; avoid separate verbose sections unless they add unique value.
- Do not duplicate long generic runtime rules or framework-wide command policy boilerplate in `claude_md` / `agents_md`; the harness appends a standard runtime baseline automatically.
- Keep code-organization guidance in `claude_md` / `agents_md` compact and stable; do not duplicate long repository trees from architecture or project-structure artifacts.
- Keep code paths consistent with the canonical app-delivery project layout used by task-decompose and implementation. For backend code, prefer `backend/src/...` and backend tests under `backend/src/tests/...` or `backend/tests/...`; for mock services, use project-root `mock-server/...`. Do not introduce or preserve `backend/app/...`, `companion/mock-server/...`, or sibling `*-mocks/...` guidance unless the imported work-item contract explicitly uses that layout.
- Do not repeat validation commands, task-scope rules, or test execution instructions that belong in task prompts or the runtime baseline.
- Do not restate feature-specific acceptance details unless they are true project-wide invariants.
- Do not invent constraints that are not grounded in the provided requirements, architecture, shared components, or clarification answers.
- test_plan coverage rows must include every requirement ID.
- test_types should be realistic, such as unit, api, integration, browser, e2e, accessibility, or performance.

Architecture metadata:
{{architecture_meta_json}}

Architecture:
{{architecture_md}}

Requirements JSON:
{{requirements_json}}

Shared components:
{{shared_components_md}}

Clarification answers:
{{clarification_answers_md}}