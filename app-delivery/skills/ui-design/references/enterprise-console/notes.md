# Enterprise Console

Use this pack for internal business systems. The UI should feel efficient, stable, and built for repeated work rather than promotion.

## Text Reference For Non-Vision Models

The default reference screenshot shows an OPC Agent Console marketplace screen that is also suitable for enterprise console baselines:

- A full-width top navigation bar in solid Rich Blue, with a small logo, product name, icon+text navigation tabs, active tab highlight, and user identity on the right.
- A light gray application background with a large white workspace panel.
- A compact page header, segmented tabs, search input, filter selects, refresh action, and item count.
- A three-column card grid for skill/agent entries. Cards use thin borders, small radius, restrained shadows, status badges, metadata, short descriptions, tags, and a single low-emphasis details action.
- The visual feel is operational and enterprise-oriented: clear hierarchy, moderate density, few decorative elements, and no marketing hero treatment.

## Enterprise Brand Color Rules

- Use solid colors only; do not use gradients.
- Primary brand: Enterprise Indigo `#1D4E89` for key actions, active states, and strong emphasis.
- Navigation/data: Enterprise Teal `#0F766E` for app chrome, dashboards, and data-heavy surfaces.
- Positive state: Enterprise Green `#2E7D32`; negative/destructive state: Enterprise Red `#C62828`; warning: Enterprise Amber `#F59E0B`.
- Accent colors: Soft Cyan `#38BDF8`, Soft Violet `#7C3AED`, and Soft Lime `#84CC16`; use sparingly for badges, charts, and technical signals.
- Soft backgrounds: Mist Blue `#E0F2FE`, Mist Green `#DCFCE7`, Mist Amber `#FEF3C7`, Mist Red `#FEE2E2`.
- Choose one dominant shell color for navigation chrome.
- Prefer Enterprise Indigo or Enterprise Teal in the top header or top navigation only. Keep the sidebar white or a very light neutral when the header already owns the shell color.
- Use the primary brand color for CTAs and active indicators, not as a second full-surface navigation background.
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
