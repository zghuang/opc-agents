# Template Selection Policy

Template selection must be auditable.

1. User-supplied UI requirements, brand rules, screenshots, and style preferences are highest authority.
2. If `ui_template` is explicit and exists, use it.
3. If requirements give detailed UI direction but no template ID, use `default` only for unspecified tokens/states.
4. Otherwise score template manifests against domain, roles, data density, interaction style, and audience.
5. If no template clearly fits, use `default` and record the missing template recommendation.

Save selected template, alternatives considered, and rationale to `docs/ui/template-selection.md`.