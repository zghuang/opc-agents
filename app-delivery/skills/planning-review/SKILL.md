---
name: planning-review
description: Run one independent, structured quality review of a spec-review or task-decompose candidate before it becomes canonical planning state.
---

Use this skill exactly once for each candidate planning artifact before importing it into the canonical project docs.

Inputs:
- `project`: app-delivery project root
- `stage`: `spec-review` or `task-decompose`
- `candidate_path`: candidate JSON written by the upstream stage skill
- `review_input_path`: temporary JSON path for this review verdict

Procedure:
1. Read the candidate JSON, its source evidence, and the supporting planning inputs.
2. For `spec-review`, read the raw source at the candidate's `source_requirements_path`; if it is unavailable, use the archived raw source matching `docs/requirements-source.*`. Review the candidate requirements, acceptance scenarios, clarifications, technology hints, and proposed canonical requirement IDs against that source.
3. For `task-decompose`, inspect `docs/requirements.json`, the archived raw source matching `docs/requirements-source.*` as supporting evidence, architecture, UI route mapping, test plan, and the candidate task graph.
4. Return one raw JSON review object. Do not edit `docs/requirements.json`, `docs/work-items.json`, `docs/gates.json`, or any source document.
5. Write the raw review JSON to the requested `review_input_path`.
6. Stop after this one review. Do not regenerate the candidate or import the review in this session; the foreground framework runner imports it after this process exits.

The framework keeps the latest review at `.app-delivery-runtime/planning-reviews/<stage>.json` and preserves every completed review under `.app-delivery-runtime/planning-review-history/<stage>/`.

Review JSON contract:

```json
{
  "status": "pass | revise",
  "summary": "short evidence-based verdict",
  "findings": [
    {
      "severity": "blocking | major | minor | note",
      "code": "stable_machine_readable_code",
      "message": "specific finding",
      "evidence": ["source or candidate evidence"]
    }
  ],
  "source_coverage": {
    "covered": [],
    "missing": [],
    "deferred": []
  },
  "proposed_corrections": [],
  "review_runner_mode": "hermes-foreground-oneshot",
  "reviewed_by": "foreground planning reviewer"
}
```

Spec-review review focus:
- Every source capability-bearing section has an explicit disposition: requirement, NFR, acceptance scenario, technology constraint, deferred scope, non-goal, or clarification.
- No independent workflow, lifecycle, actor, integration, security boundary, recovery path, or acceptance signal is silently merged into an over-broad requirement.
- Requirement IDs are stable, source anchors are valid, and acceptance scenarios point to canonical requirement IDs.
- NFRs, production constraints, technology choices, backup/recovery, rollback, observability, and UI requirements are represented or explicitly deferred.
- Clarification recommendations are not treated as confirmed scope decisions.

Task-decompose review focus:
- Every canonical requirement and acceptance scenario has an appropriate feature or validation owner.
- Feature tasks are coherent, independently useful vertical slices, not catch-all domain bundles.
- Large tasks with many requirements, scenarios, paths, or tests have a defensible split justification or are proposed for splitting.
- UI route mappings have explicit frontend source and browser-test ownership.
- Validation tasks depend on the feature tasks they validate.
- Complexity signals match the graph: requirements, scenarios, routes, integrations, state machines, external systems, and estimated task count.
- `done_when` items have executable tests; placeholder or mock-only evidence is not treated as completion.

Rules:
- `status=pass` only when no blocking or major finding remains.
- `status=revise` when the candidate should be regenerated or split before import, including when a user decision or missing source evidence must be represented as a finding for the host.
- Do not use a separate blocked verdict for this review. A planning review is a recoverable host handoff: record the blocking finding, stop this invocation, and let the host regenerate the candidate before running a new foreground review.
- The framework permits two `revise` opportunities across the entire stage budget, including all candidate cycles. A third completed review that still returns `revise` is recorded with its findings and allowed to continue as a forced pass; no later cycle may silently reset this budget.
- Preserve canonical IDs in proposed corrections. Do not silently renumber, merge, or delete requirements or tasks.
- The framework binds the review to the candidate SHA-256 and will reject stale reviews.
