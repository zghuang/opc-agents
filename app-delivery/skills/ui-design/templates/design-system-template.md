# UI Design System - {{PROJECT_NAME}}

Selected template: {{UI_TEMPLATE}}
Status: draft

## Product Context

- Audience:
- Primary workflows:
- Data density:
- Device targets:
- Accessibility target: WCAG 2.1 AA

## Visual Language

### Color Roles

Use solid colors only. Do not use gradient backgrounds, gradient buttons, decorative color blobs, or bokeh effects.

| Role | Ant Design Token | Default Hex | Usage |
|------|------------------|-------------|-------|
| Primary Brand | `colorPrimary` | `#503291` | Primary actions, active navigation, key focus state |
| Navigation/Data Blue | `colorInfo` | `#0F69AF` | Navigation bars, data-heavy accents, professional content |
| Technical Accent | custom `colorCyan` | `#2DBECD` | Secondary technical indicators, integration/status accents |
| Innovation Accent | custom `colorMagenta` | `#EB3C96` | Limited highlights and selected badges |
| Success | `colorSuccess` | `#149B5F` | Completed/positive state |
| Success Background | custom `colorSuccessBgSoft` | `#B4DC96` | Low-emphasis positive background |
| Warning | `colorWarning` | `#FFC832` | Needs attention, pending state |
| Warning Background | custom `colorWarningBgSoft` | `#FFDCB9` | Low-emphasis warning background |
| Error | `colorError` | `#E61E50` | Failed/destructive state |
| Background | `colorBgLayout` | `#F5F6F8` | App shell background |
| Surface | `colorBgContainer` | `#FFFFFF` | Panels, forms, cards, tables |
| Soft Blue Surface | custom `colorInfoBgSoft` | `#96D7D2` | Subtle chart or status backgrounds |
| Soft Pink Surface | custom `colorAccentBgSoft` | `#E1C3CD` | Rare low-emphasis highlight backgrounds |
| Text | `colorText` | `#1F1F1F` | Main text |
| Muted Text | `colorTextSecondary` | `#5F6673` | Secondary labels and metadata |

Palette rules:
- Keep most screen area neutral (`colorBgLayout`, `colorBgContainer`, text tokens).
- Choose one dominant shell color for navigation chrome. If header or top navigation uses Rich Blue, keep the sidebar neutral or very light; if a colored sidebar is required, keep the header neutral.
- Use Rich Purple for primary actions, focus rings, and active indicators, not as a second full-surface navigation background.
- Do not make a page look like a single-color theme. Pair neutral surfaces with one primary brand color plus semantic status colors.

### Brand Asset

- Source asset:
- Placement: header brand slot by default; sidebar brand rail only when the layout requires it
- Desktop logo height:
- Mobile logo height:
- Clear space and aspect ratio:

### Typography

- Base font:
- Heading scale:
- Table/list density:
- Numeric/data display:

### Spacing And Shape

- Base spacing unit:
- Page padding:
- Panel radius:
- Control height:
- Elevation/shadow rules:

## Layout Shell

- Navigation:
- Header: include brand asset placement, product name, and global actions
- Sidebar: neutral surface by default with a clear active indicator treatment
- Content area:
- Responsive behavior:

## Shared Components

- `frontend/src/layouts/AppShell.tsx`
- `frontend/src/components/feedback/PageSkeleton.tsx`
- `frontend/src/components/feedback/ErrorResult.tsx`
- `frontend/src/components/feedback/EmptyState.tsx`
- `frontend/src/styles/theme.ts`

## Implementation Notes

- Use Ant Design v5 primitives by default.
- Keep colors in `frontend/src/styles/theme.ts`; do not hardcode hex values in feature components.
- Feature components consume the shell and shared feedback states rather than creating ad hoc page frames.
