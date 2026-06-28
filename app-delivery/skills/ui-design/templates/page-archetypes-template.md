# UI Page Archetypes - {{PROJECT_NAME}}

Selected template: {{UI_TEMPLATE}}

## App Shell

Purpose:

Required elements:
- Navigation
- Current user/session state
- Global actions
- Main content outlet

## Dashboard / Workspace

Use when:

Layout:

States:
- Loading:
- Empty:
- Error:

## List / Table

Use when:

Required elements:
- Search
- Filters
- Primary action
- Row actions
- Pagination or cursor loading
- Bulk action rules when applicable

## Detail Page

Use when:

Required elements:
- Summary header
- Metadata
- Primary actions
- Related records or timeline

## Form / Edit Flow

Use when:

Required elements:
- Field grouping
- Validation
- Cancel/submit behavior
- Unsaved changes handling when required

## AI / Activity Workspace

Use when:

Required elements:
- Input/composer
- Stream or timeline
- Activity/tool run list
- Artifact or context panel
- Interrupt/retry/resume states

## Route Mapping

Every user-facing route from the requirements must appear here before `arch-design` creates frontend work items. The Source Requirement column must contain REQ IDs. Use Notes to name the owning frontend feature spec, for example `docs/modules/contracts-ui.md`.

| Route | Archetype | Source Requirement | Notes |
|-------|-----------|--------------------|-------|
