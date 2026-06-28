status: pass | pass_with_notes | changes_requested | blocked
review_type: ui-design
source: docs/requirements.md, docs/ui/, selected template references
selected_template: {{UI_TEMPLATE}}

# UI Design Review

## Decision

## Checks

- [ ] Template selection is justified or fallback is explicit.
- [ ] Selected template token and shell constraints are preserved in `docs/ui/design-system.md`; no generic fallback palette or layout defaults were substituted.
- [ ] `python3 $HOME/opc/scripts/opc-ledger.py check-ui-style-consistency --workdir {workdir} --include-theme false` passes before this review is marked `pass` or `pass_with_notes`.
- [ ] `docs/ui/design-system.md` defines usable tokens and shell rules.
- [ ] `docs/ui/page-archetypes.md` maps expected pages to implementation shapes.
- [ ] If frontend work items already exist, route mapping changes were reconciled back into `docs/work-items.json` / `/arch-design` before handoff.
- [ ] `docs/ui/states.md` defines loading, empty, error, permission, and destructive states.
- [ ] UI foundation work item exists before feature work items.
- [ ] Accessibility expectations are concrete enough for QA.
- [ ] No unresolved C1 requirement is hidden as a UI assumption.

## Findings

## Follow-Up Work Items
