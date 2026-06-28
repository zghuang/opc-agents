# Browser-App UI Surfaces

For browser-app projects, `docs/ui/page-archetypes.md` must include a Route Mapping table that later frontend work items can consume.

Each route/workflow should list:

- route path
- requirement IDs
- allowed roles/personas
- backend/API dependencies
- loading/empty/error/disabled/success states
- target frontend paths
- browser/a11y QA expectations

`opc-ledger.py check-ui-coverage` uses route mappings to verify frontend work items exist.