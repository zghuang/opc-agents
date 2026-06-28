Generate a JSON object with these required top-level fields:
- `items`: full-stack vertical-slice delivery tasks
- `delivery_complexity`: coarse project classification and rationale
- `validation_gates`: suggested module/milestone/release gates for `docs/gates.json`

Each task item must include:
- title
- task_kind
- requirements
- acceptance_scenarios
- dependencies
- output_tests
- output_paths

Constraints:
- Do not emit T000, T001, or T-FINAL. The framework inserts built-in tasks itself.
- Prefer complete user-visible slices over layer-by-layer tasks.
- Decompose from overall product capability into executable vertical slices.
- A task may cross backend, frontend, mock-server, tests, and shared components when that is the smallest coherent slice.
- Avoid horizontal or layer-only tasks such as "all DB models" or "all frontend pages".
- Keep dependency edges explicit and minimal so the resulting graph is a practical DAG, not a linear dump.
- Keep each task small enough for one AI session to complete.
- A good task usually has 3-15 source paths, 1-5 output tests, can be described in 1-2 sentences, and delivers a complete user-visible feature without needing another task to "finish" it.
- If a feature would exceed that size, split it into smaller vertical slices by user workflow, sub-capability, or bounded context.
- output_tests should be concrete paths that can be executed later.
- output_paths should be the main source paths the task is expected to touch.
- All file paths must be project-root-relative. Use `backend/...`, `frontend/...`, `mock-server/...`, or `docs/...` paths, not backend-root-relative shortcuts like `src/...` or `tests/...`.
- For backend code paths, use `backend/src/...` and backend test paths such as `backend/src/tests/...` or `backend/tests/...`.
- For frontend code paths, use `frontend/src/...`; for browser/e2e tests use `frontend/e2e/...` when the test is file-based.
- Dependencies should only reference preceding feature tasks when necessary.
- Ensure every requirement is covered by at least one feature task.
- Prefer whole-feature sequencing: from infrastructure/shared foundations into functional slices, then final verification.
- Each acceptance_scenario should have corresponding output tests that exercise it.
- Do not create a generic end-to-end verification suite task. The framework already provides T-FINAL for cross-project verification.
- Use `task_kind: "feature"` for normal implementation slices and `task_kind: "validation"` for validation-focused work items.
- If the project is medium-or-larger (`M`, `L`, or `XL`), add validation-focused work items into `items` so module or milestone QA happens during implementation, not only at T-FINAL.
- Validation tasks must depend on the feature tasks they validate. Keep those dependencies explicit, and do not schedule validation before the validated slices can possibly pass.
- Complexity classification does not need to be exact. A rough, defensible estimate is enough.
- Avoid creating broad backend/frontend foundation buckets beyond the built-in scaffold/shared-infrastructure phases. If shared code is needed, keep it tightly scoped or attach it to the earliest owning vertical slice.
- If `ui_required=true`, user-visible tasks should naturally include frontend output paths where the requirement implies UI behavior.
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

Clarification answers:
{{clarification_answers_md}}