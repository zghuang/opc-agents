# Default Product UI

Use this pack when no specialized UI template clearly fits. It favors a quiet product shell, restrained color, readable type, predictable navigation, and complete loading/error/empty states.

When the project is an enterprise product and no stricter brand guide is supplied, use the same solid-color baseline as `enterprise-console`.

## Text Reference For Non-Vision Models

The bundled `screenshots/image1.png` shows an OPC Agent Console marketplace screen:

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
- Use the primary brand color for CTAs, selected tabs, and focus states; do not pair two saturated navigation backgrounds.
- When `screenshots/logo.png` is the approved brand asset, place it in the left header brand slot or sidebar brand rail at roughly 20-24 px visual height, preserve aspect ratio, and pair it with product name text.

Recommended baseline:
- App shell with header and optional sidebar.
- Content width constrained for forms and details; full-width layouts for tables and dashboards.
- Neutral surfaces with one primary brand color plus semantic status colors.
- Avoid decorative hero sections unless the requirements call for a public landing page.
