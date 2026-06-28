# Acceptance Scenarios

Preserve explicit user stories, use cases, user journeys, approval flows, operator workflows, lifecycle flows, degradation/recovery flows, or replay/setup journeys from the source requirements. Do not invent new scenarios that are not justified by the source.

Use stable scenario IDs so architecture, work items, QA, and completion evidence can refer to the same scenario deterministically.

Keep every scenario in the single markdown table below. Do not convert this file into per-scenario headings, nested `Field | Value` tables, or free-prose sections because deterministic ledger parsing only recognizes the `Scenario ID` table rows.

| Scenario ID | Type | Source Requirement | Primary Actor | Summary | Notes |
|-------------|------|--------------------|---------------|---------|-------|
| AS-001 | user-journey | REQ-001 | End User | Complete the primary workflow successfully | Happy path plus the key alternate/failure edge that must be tested |
| AS-002 | use-case | REQ-002, REQ-003 | Operator | Approve and dispatch the queued action | Include approval thresholds, permission boundaries, or degraded-path behavior |

Guidelines:

- Keep `Scenario ID` stable across reruns when the meaning is unchanged.
- `Type` can be `user-story`, `use-case`, `user-journey`, `workflow`, `approval-flow`, or another concise scenario class justified by the source.
- Use scenario rows when the source implies a multi-step journey even if it does not label it with formal headings.
- `Source Requirement` must contain canonical REQ/NFR IDs.
- `Summary` should be short enough to be copied into work item `acceptance_scenarios`, ideally as `AS-001 <summary>`.
- `Notes` should capture the most important alternate path, failure edge, or assertion that QA must preserve.
