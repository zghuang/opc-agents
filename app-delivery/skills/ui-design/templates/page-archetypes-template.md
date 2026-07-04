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

Every user-facing page, route, or major workflow from the requirements must appear here before `task-decompose` creates frontend work items. Source Requirements must contain REQ IDs. Suggested Output Paths and Suggested Browser Tests must be project-root-relative paths.

| Page / Route | Source Requirements | Primary Roles | Required Regions / Components | States | Suggested Output Paths | Suggested Browser Tests |
|--------------|---------------------|---------------|--------------------------------|--------|------------------------|-------------------------|
