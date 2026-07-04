Generate a JSON object with these required top-level fields:
- `items`: full-stack vertical-slice delivery tasks
- `delivery_complexity`: coarse project classification and rationale
- `validation_gates`: suggested module/milestone/release gates for `docs/gates.json`

Each task item must include:
- title
- task_kind
- intent
- technology_constraints
- requirements
- acceptance_scenarios
- dependencies
- output_tests
- output_paths

The `intent` object must use this lightweight shape:

```json
{
	"objective": "short capability statement",
	"journey": "user, external-system, or background workflow this task enables",
	"done_when": ["concrete completion signal", "validation signal"],
	"non_goals": ["optional scope boundary"],
	"split_justification": "optional reason for keeping a large task whole"
}
```

Intent rules:
- `objective` is recommended for every generated task.
- `journey` is recommended for user-visible feature tasks; for internal tasks, describe the system or external-system workflow instead of forcing a UI journey.
- `done_when` should contain two to four concrete completion signals that help runtime and review understand what done means.
- `non_goals` is optional. Use it only when the task is likely to overlap neighboring tasks or invite scope creep.
- Intent must be grounded in the same requirements and acceptance scenarios as the task. Do not add new product scope through intent.
- If two generated tasks have nearly identical objectives or journeys, merge them, clarify their boundaries, or add an explicit dependency so they do not duplicate work.
- The size checks apply only to non-built-in delivery tasks. Framework-inserted scaffold, shared foundation, audit, final, and production-gate tasks are not part of this limit.
- If a task has more than 12 requirements, more than 5 acceptance scenarios, or more than 8 output tests, and two or more of those are true, reassess the boundary. Decide whether it is one coherent independently testable capability or should be split into vertical slices. Record the decision in `intent.split_justification`.
- If the graph has more than 20 non-built-in delivery tasks, reassess whether the count is appropriate for the project size and module boundaries. Keep tasks separate when each is a coherent independently testable slice; merge only when the merged task remains focused. Record the decision in `intent.split_justification` for intentionally retained count-heavy boundaries.

`technology_constraints` must use this shape on every task. Use an empty array only when no project technology choice is specifically owned or required by that task:

```json
[
	{
		"name": "selected-framework-or-library-name",
		"ecosystem": "backend|frontend|infra|project",
		"requirement": "must_use",
		"reason": "brief source-grounded reason this task must use or verify the technology",
		"source": "requirements, architecture, ADR, clarification, or other project artifact path/section",
		"expected_evidence": [
			"concrete dependency/config/import/API/use evidence expected from this task",
			"concrete behavior or integration evidence expected from this task"
		]
	}
]
```

Technology constraint rules:
- Assign explicit project-wide technology choices from requirements, architecture, ADRs, shared components, and Project technology constraints to the smallest owning tasks that must implement or directly integrate them.
- Do not copy all technologies into every task. A constraint belongs on a task only when that task must use, integrate, or verify that technology to satisfy its own intent and owned requirements.
- Assign constraints by the task's required capability and source artifacts, not by whether the technology name appears in the task title.
- Use `must_use` when architecture/ADR/requirements mandate the technology. Use `should_use` only for preferred libraries where an equivalent implementation could be accepted with explicit rationale. Use `avoid` only for rejected alternatives.
- `expected_evidence` must be concrete and reviewable. Prefer dependency manifest entries, imports/API usage, configuration files, generated artifacts, or behavior-specific implementation evidence.
- If a task explicitly supersedes a technology choice, require an ADR/clarification in `source` and explain it in `reason`; do not silently omit the original constraint.

Constraints:
- Do not emit T000, T001, or T-FINAL. The framework inserts built-in tasks itself.
- Prefer complete user-visible slices over layer-by-layer tasks.
- Decompose from overall product capability into executable vertical slices.
- Choose the smallest coherent task that still delivers a meaningful product capability.
- Assign each explicit project-wide technology choice from requirements, architecture, or project technology constraints to at least one owning task through `technology_constraints`.
- A task may cross backend, frontend, mock-server, tests, and shared components when that is the smallest coherent slice.
- Avoid horizontal or layer-only tasks such as "all DB models" or "all frontend pages".
- Also avoid over-fragmenting one feature into separate model/API/UI/test fragments; too many handoff points can reduce delivery quality just as much as oversized tasks.
- Keep dependency edges explicit and minimal so the resulting graph is a practical DAG, not a linear dump.
- For medium-or-larger projects, a graph where every generated feature/validation task has `dependencies: []` is invalid. Add explicit prerequisite edges where workflow order, shared contracts, domain sequencing, or validation ownership exists.
- Keep each task small enough for one AI session to complete.
- A good task usually has 3-15 source paths, 1-5 output tests, and one coherent feature outcome.
- If a feature would exceed that size, split it into smaller vertical slices by user workflow, sub-capability, or bounded context.
- If splitting a feature would leave tasks that are only plumbing, placeholder UI, generic contracts, or tests without a usable behavior, keep the slice together instead.
- output_tests should be concrete paths that can be executed later.
- output_paths should be the main source paths the task is expected to touch.
- All file paths must be project-root-relative. Use `backend/...`, `frontend/...`, `mock-server/...`, or `docs/...` paths, not backend-root-relative shortcuts like `src/...` or `tests/...`.
- Follow the architecture-selected paths and the selected stack guidance below for backend, frontend, mock-server, MCP/FastMCP, and test path roots. Do not assume a backend package directory unless requirements or architecture explicitly selected one.
- Add dependency edges only for real prerequisites: technical/shared foundation needs or product workflow/domain ordering. If feature B only makes sense after feature A, make B depend on A.
- Ensure every requirement is covered by at least one feature task.
- Do not use a foundation/support slice to claim full user-visible requirement coverage when it only creates scaffolding, stubs, wiring, or shared primitives; keep that full requirement on the owning feature slice and any validation task.
- If a task owns API endpoints, route handlers, UI data flows, workflow actions, or external-system contracts, its intent and output paths/tests must include the real backing behavior for that slice: service/domain logic, persistence or query/update path, external adapter/mock-server contract when explicitly dev-only, and assertions on returned data semantics. Do not treat "route exists", "response shape matches", hardcoded/default responses, or static fake data as a completed feature.
- Mock servers, seed data, fixtures, common components, base contracts, app shells, and runtime wiring may be necessary tasks only when they unlock later feature work; they should not become catch-all requirement owners.
- Prefer whole-feature sequencing: from infrastructure/shared foundations into functional slices, then final verification.
- Before finalizing `items`, self-check every `feature` task with this question: after this task alone and its dependencies complete, what user, operator, or external system capability works end-to-end enough to be tested?
- If the answer is only "shared setup exists", "a page shell exists", "models exist", "API routes exist", or "tests exist", revise the task boundary unless the task is a necessary dependency task with narrowly scoped requirements.
- Each acceptance_scenario should have corresponding output tests that exercise it.
- Assign an acceptance_scenario to the task that makes the scenario's observable journey, decision, or action executable; do not attach it only to a prerequisite data, adapter, model, or signal-producing slice.
- If a task owns a user-visible acceptance scenario and includes `frontend/src/...` output paths, its `output_tests` should normally include at least one browser/e2e test for that flow. Do not defer all browser evidence to a later validation task unless that downstream validation task explicitly owns the same scenario.
- Validation tasks may deepen browser coverage, but they should not be the first and only browser evidence for a user-visible frontend feature slice.
- Do not create a generic end-to-end verification suite task. The framework already provides T-FINAL for cross-project verification.
- Use `task_kind: "feature"` for normal implementation slices and `task_kind: "validation"` for validation-focused work items.
- If the project is medium-or-larger (`M`, `L`, or `XL`), add validation-focused work items into `items` so module or milestone QA happens during implementation, not only at T-FINAL.
- Validation tasks must depend on the feature tasks they validate by task ID or exact/shortened task title. Keep those dependencies explicit, and do not schedule validation before the validated slices can possibly pass.
- Complexity classification does not need to be exact. A rough, defensible estimate is enough.
- Avoid creating broad backend/frontend foundation buckets beyond the built-in scaffold/shared-infrastructure phases. If shared code is needed, keep it tightly scoped or attach it to the earliest owning vertical slice.
- If `ui_required=true`, user-visible tasks should naturally include frontend output paths where the requirement implies UI behavior.
- If `docs/ui/page-archetypes.md` exists and contains a `## Route Mapping` table, every mapped page/route/workflow row must be covered by at least one `feature` or `validation` task through overlapping source requirement IDs. The covering task must include relevant `frontend/...` output paths; when the route row suggests browser tests, the task must include at least one `frontend/e2e/...`, Playwright, browser, or e2e output test.
- Do not leave page shell, primary navigation, page archetypes, mobile drawer behavior, or required UI states only in `docs/ui/*`; attach them to the owning user-visible feature tasks through `intent.done_when`, `output_paths`, and `output_tests`.
- Use the provided test-plan coverage hints to keep output_tests aligned with expected test types.

Use this `delivery_complexity` shape:

```json
{
	"tier": "M",
	"rationale": "short explanation",
	"signals": {
		"estimated_loc": 80000,
		"estimated_modules": 6,
		"estimated_tasks": 18
	}
}
```

Selected stack: {{stack_id}}

Selected stack contract:
{{stack_guidance_md}}

Use this `validation_gates` entry shape:

```json
{
	"id": "GATE-review-domain",
	"kind": "module",
	"title": "Module gate: review domain",
	"scope_tasks": ["T004", "T005"],
	"scope_requirements": ["REQ-002"],
	"required_test_types": ["api", "browser"]
}
```

Example validation task inside `items`:

```json
{
	"title": "Review domain validation gate",
	"task_kind": "validation",
	"requirements": ["REQ-002"],
	"acceptance_scenarios": [],
	"dependencies": ["T004", "T005"],
	"output_tests": ["backend/tests/test_review/test_review_pipeline.py", "frontend/e2e/review.spec.ts"],
	"output_paths": ["docs/reviews/test-report-review-domain.md"]
}
```

Requirements JSON:
{{requirements_json}}

Architecture:
{{architecture_md}}

Shared components:
{{shared_components_md}}

Architecture metadata:
{{architecture_meta_json}}

Test plan coverage:
{{test_plan_json}}

Project technology constraints:
{{dependency_hints_json}}

Clarification answers:
{{clarification_answers_md}}