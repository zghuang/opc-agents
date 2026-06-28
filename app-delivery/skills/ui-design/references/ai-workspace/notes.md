# Default Product UI

Use this pack when no specialized UI template clearly fits. It favors a quiet product shell, restrained color, readable type, predictable navigation, and complete loading/error/empty states.

When the project is for Merck or an internal enterprise product and no stricter brand guide is supplied, use the same solid-color baseline as `enterprise-console`.

## Text Reference For Non-Vision Models

The bundled `screenshots/image1.png` shows an AppCopilot Console marketplace screen:

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
- Use Rich Purple for CTAs, selected tabs, and focus states; do not pair a full Rich Blue sidebar with purple-filled primary navigation.
- When `screenshots/logo.png` is the approved brand asset, place it in the left header brand slot or sidebar brand rail at roughly 20-24 px visual height, preserve aspect ratio, and pair it with product name text.

Recommended baseline:
- App shell with header and optional sidebar.
- Content width constrained for forms and details; full-width layouts for tables and dashboards.
- Neutral surfaces with one primary brand color plus semantic status colors.
- Avoid decorative hero sections unless the requirements call for a public landing page.
