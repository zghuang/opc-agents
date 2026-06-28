# UI States - {{PROJECT_NAME}}

Selected template: {{UI_TEMPLATE}}

## Global Feedback Rules

Every user-facing page or panel must define these states before implementation:

- Loading
- Empty
- Error
- Permission denied
- Disabled/unavailable action
- Success confirmation
- Destructive confirmation when relevant

## State Matrix

| Surface | Loading | Empty | Error | Permission | Success | Notes |
|---------|---------|-------|-------|------------|---------|-------|

## Interaction Rules

- Forms use visible labels, not placeholder-only labels.
- Primary actions are explicit and stable across loading/error transitions.
- Destructive actions require confirmation when data loss or irreversible changes are possible.
- Long-running actions show progress or queued/running/completed state.
- Browser tests should target stable accessible names or `data-testid` only where accessible names are insufficient.

## Accessibility

- Color contrast: at least 4.5:1 for text.
- Keyboard path: every interactive flow can be completed with keyboard only.
- Focus: modal/dialog focus trapping and return focus where applicable.
- Labels: all interactive controls have visible labels or `aria-label`.

## QA Hooks

List critical selectors, accessible names, and user paths for QA verification:

| User Path | Required State | Test Type | Notes |
|-----------|----------------|-----------|-------|
