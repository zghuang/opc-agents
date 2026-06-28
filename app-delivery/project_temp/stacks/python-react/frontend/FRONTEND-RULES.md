# Frontend Rules

This file defines the technology baseline and state management rules for all OPC frontend projects. It applies regardless of which UI design template was selected.

When `docs/ui/` artifacts exist for this project, they take precedence for design-specific decisions (colors, typography, page shapes, state treatments, component names). This file governs the implementation layer — which library, which styling method, state management patterns, and how to structure code. Do not fold these rules into `/ui-design`: `/ui-design` owns project-specific UI contracts under `docs/ui/*`, while this file owns the stable frontend implementation baseline.

For unfamiliar or fast-moving third-party packages, do not guess APIs; first consult project-pinned examples or official upstream docs with available fetch/search tools, and stop if usage remains uncertain.

## Component Library — Ant Design v5
Use `antd` v5 for all UI components (Button, Input, Table, Form, Modal, Select, etc.).
Do not rebuild what Ant Design already provides. Custom primitives not covered by Ant Design go in `frontend/src/components/ui/`. Do not add other component libraries without an approved ADR.

## Custom Styling — CSS Modules
Use CSS Modules (`.module.css`) for any style not handled by Ant Design.
Colocate `MyComponent.module.css` with its `MyComponent.tsx`.
No inline styles for static values. No Tailwind, styled-components, or Emotion.

## Theming — Ant Design ConfigProvider
Define all visual tokens (brand color, radius, font) in `frontend/src/styles/theme.ts` as `ThemeConfig`, applied via `<ConfigProvider theme={theme}>` in `App.tsx`.
No hardcoded hex values in components — reference tokens via `theme.useToken()`.
If `docs/ui/design-system.md` defines project-specific color roles or spacing, map those values into `theme.ts` — do not scatter them across component files.

## State Management

- Use **TanStack Query** for all server state: API calls, caching, retries, and cache invalidation.
- Use **Zustand** for local client state: UI layout, wizard progress, filters, drafts, and cross-component interaction state.
- Do not mirror TanStack Query server data into Zustand stores.
- Keep feature-specific stores under `frontend/src/features/{feature}/`; put only cross-feature stores under `frontend/src/lib/stores/`.
- Store actions must be explicit and typed. Do not expose a generic setter for the whole store.

## Global Layout Shell
`frontend/src/layouts/AppShell.tsx` owns the header, sidebar, and main content area.
Implement it as a `kind=infrastructure` work item before any feature work item starts; all feature work items declare it in `depends_on`.
Features do not insert navigation items directly — register routes and menu entries in `frontend/src/routes/`.
If `docs/ui/page-archetypes.md` exists, use it as the authoritative spec for the shell's layout structure and navigation shape.

## Standard Feedback States
Never render a blank screen during loading, error, or empty data. Place shared feedback components in `frontend/src/components/feedback/`.
If `docs/ui/states.md` exists, use it as the authoritative spec for which states to handle and what each should communicate. The default antd-based implementations are:

| State   | Default Component | Based on            |
|---------|-------------------|---------------------|
| Loading | `<PageSkeleton>`  | Ant Design Skeleton |
| Error   | `<ErrorResult>`   | Ant Design Result   |
| Empty   | `<EmptyState>`    | Ant Design Empty    |

These components are part of the Shell infrastructure work item.

## Accessibility
All interactive elements need a visible label or `aria-label`. Form fields use `Form.Item label` — no placeholder-only labels. Color contrast ≥ 4.5:1 (WCAG 2.1 AA).

## Directories
| Path | Purpose |
|------|---------|
| `frontend/src/layouts/` | AppShell and layout wrappers |
| `frontend/src/components/ui/` | Custom primitives not in Ant Design |
| `frontend/src/components/feedback/` | Shared loading / error / empty states |
| `frontend/src/routes/` | TanStack Router routes and nav config |
| `frontend/src/features/{name}/` | Feature code (components, hooks, api, store) |
| `frontend/src/lib/stores/` | Cross-feature Zustand stores |
| `frontend/src/styles/` | `theme.ts` and global CSS |
