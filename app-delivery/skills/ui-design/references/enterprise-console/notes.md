# Enterprise Console

Use this pack for internal business systems. The UI should feel efficient, stable, and built for repeated work rather than promotion.

## Text Reference For Non-Vision Models

The default reference screenshot shows an AppCopilot Console marketplace screen that is also suitable for enterprise console baselines:

- A full-width top navigation bar in solid Rich Blue, with a small logo, product name, icon+text navigation tabs, active tab highlight, and user identity on the right.
- A light gray application background with a large white workspace panel.
- A compact page header, segmented tabs, search input, filter selects, refresh action, and item count.
- A three-column card grid for skill/agent entries. Cards use thin borders, small radius, restrained shadows, status badges, metadata, short descriptions, tags, and a single low-emphasis details action.
- The visual feel is operational and enterprise-oriented: clear hierarchy, moderate density, few decorative elements, and no marketing hero treatment.

## Merck-Compatible Color Rules

- Use solid colors only; do not use gradients.
- Primary brand: Rich Purple `#503291` for key actions, active states, and strong emphasis.
- Navigation/data: Rich Blue `#0F69AF` for app chrome, dashboards, and data-heavy surfaces.
- Positive state: Rich Green `#149B5F`; negative/destructive state: Rich Red `#E61E50`; warning: Vibrant Yellow `#FFC832`.
- Accent colors: Vibrant Magenta `#EB3C96`, Vibrant Cyan `#2DBECD`, and Vibrant Green `#A5CD50`; use sparingly for badges, charts, and technical signals.
- Soft backgrounds: Sensitive Pink `#E1C3CD`, Sensitive Blue `#96D7D2`, Sensitive Green `#B4DC96`, Sensitive Yellow `#FFDCB9`.
- Choose one dominant shell color for navigation chrome.
- Prefer Rich Blue in the top header or top navigation only. Keep the sidebar white or a very light neutral when Rich Blue already owns the shell.
- Use Rich Purple for CTAs and active indicators, not as a second full-surface navigation background.
- When an approved logo asset is provided, place it in the left header brand slot at roughly 20-24 px visual height and preserve aspect ratio.

Design tendencies:
- Sidebar navigation with clear sections and role-aware entries.
- Dense but readable tables with filters, search, saved views, and bulk actions.
- Forms in focused panels or pages with explicit validation and confirmation states.
- Dashboards prioritize operational status and exceptions over decorative cards.
- Use restrained branding: one primary color, semantic state colors, high contrast, minimal shadows.

Implementation notes:
- Prefer Ant Design primitives from the OPC frontend rule set.
- Build shared table, filter, empty, error, and loading states during UI foundation.
- Avoid oversized hero typography, gradient backgrounds, and marketing-style composition.
