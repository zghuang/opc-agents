status: pass_with_notes | needs_clarification
review_type: requirements
source: {{REQUIREMENTS_PATH}}
c1_questions_count: {{C1_COUNT}}
external_api_dependency_count: {{EXTERNAL_API_COUNT}}
performance_requirement_count: {{PERFORMANCE_SIGNAL_COUNT}}
process_surface_count: {{PROCESS_SURFACE_COUNT}}
scenario_inventory_required: yes | no
scenario_inventory_path: docs/acceptance-scenarios.md | not-needed

# Requirements Review

## Confirmed Feature Checklist

- [ ] REQ-001 ...

## Requirement Change Summary

- Added scope:
- Changed scope:
- Removed/replaced scope:
- Source buckets/items mapped:

## Blind Spot Checklist

For each feature, check boundary conditions, error paths, concurrency, permission boundaries, and data consistency.

```text
Blind spots for Feature X:
  - [ ] [boundary] ...
  - [ ] [error] ...
```

## Non-Functional Requirements Assessment

- Performance:
- Availability:
- Security:
- Scalability:

## External API Dependency List

```text
- ServiceName
  - Endpoint:
  - Request fields:
  - Response fields:
  - Scenarios to mock: success / failure / timeout
```

## Technical Decision Points

- [ ] ...

## Scenario Inventory

- Acceptance scenario inventory path: `docs/acceptance-scenarios.md` | not needed
- Scenario-backed requirements:
- Journeys/use cases that must survive into work-item `acceptance_scenarios` and executable acceptance tests:
- Why scenario inventory is required or not required:

## Clarification Questions

```text
C1 - [Security] ... Impacted requirements: REQ-001
C2 - [API] ...
C3 - [UX] ...
```
