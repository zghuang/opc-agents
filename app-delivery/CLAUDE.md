# app-delivery Agent Notes

Keep this file synchronized with its counterpart guidance file. Both files describe the current app-delivery framework for implementation and review agents.

## Project Identity

app-delivery is a domain-agnostic OPC delivery agent. It turns project requirements into normalized planning artifacts, then drives scaffold, implementation, validation, review, repair, and final verification.

The framework does not encode one specific project, department, company, or business domain. Domain behavior comes from each project's requirements, architecture, module docs, UI docs, and test plan. app-delivery owns delivery mechanics: ledgers, runtime execution, test evidence, review routing, watchdogs, exception recovery, and gates.

## Current Architecture

The Python package under `delivery/` is the source of truth. Hermes skills and operator commands are thin entry points over the control plane. Implementation work is done by Claude Code or OpenCode sessions. Review is framework-managed through independent review artifacts and imports.

Important modules:

| Module | Responsibility |
| --- | --- |
| `__main__.py` | CLI entrypoint, locks, control actions, pause/resume/fix, stage imports |
| `control_plane.py` | Deterministic routing from project state to the next step |
| `loop.py` | Implementation loop, task execution, test repair, exception handling |
| `loop_task_prompt.py` | Runtime prompts for task, repair, validation, stalled recovery |
| `loop_review.py` | Review imports, machine preconditions, semantic-risk deferral |
| `loop_gitops.py` | Task-scoped staging, exception patches, dirty-worktree safety |
| `loop_reporting.py` | Status, project summary, release evidence |
| `runtime_liveness.py` | Runtime activity and stall detection inputs |
| `scaffold.py` | T000 scaffold generation from stack templates and Module Architecture |
| `stage_harness.py` | Stage payload validation/import for spec, architecture, context, tasks |
| `task.py` | Task model, dependency graph, task-decompose normalization and validation |
| `gates.py` | Complexity normalization, validation gates, gate refresh |
| `production_semantics.py` | Static semantic scans for stubs, fake data, auth gaps |
| `review_payload.py` | Review payload normalization and assessment validation |
| `review_artifacts.py` | Markdown review artifacts and deferred risk records |

## Delivery Flow

1. `spec-review` imports normalized requirements and acceptance scenarios.
2. `arch-design` imports architecture, module docs, ADRs, and metadata.
3. `ui-design` imports UI contracts when `ui_required=true`.
4. `project-context-sync` imports runtime context and test plan.
5. `task-decompose` imports executable work items and validation gates.
6. T000 creates scaffold and project structure.
7. T001 creates only shared foundation required before feature slices.
8. Feature and validation tasks run through implementation, tests, review, and repair.
9. Framework-managed audit/production gates run before final verification.
10. T-FINAL produces release evidence and final review artifacts.

## Planning And Scaffold Rules

- `docs/architecture.md` `Module Architecture` is the T000 scaffold contract. It is a structural skeleton, not a full source-file inventory.
- The scaffold parser accepts connector trees, consistently indented trees, arbitrary fenced languages, markdown-list trees, and either a wrapper root or direct repository roots.
- Module Architecture may contain limited source anchors, but implementation-file inventory is capped by `MAX_MODULE_TREE_IMPLEMENTATION_FILE_ENTRIES` in `stage_harness.py` (currently 50). Scaffold/config files are exempt.
- Task decomposition has no fixed hard cap on generated task count. Use semantic coherence, dependency quality, coverage, path/test size, and `intent.split_justification` instead of forcing a number.
- `delivery_complexity.tier` uses S/M/L/XL with `S = small`. Do not use `S` as a highest-complexity label. Rich signals such as requirements, acceptance scenarios, integrations, agent types, UI pages, and state machines must be consistent with the tier.
- `intent.done_when` and `intent.non_goals` must be arrays of complete strings. The framework guards against string values being expanded into character lists.

## Runtime And Review Rules

- The implementation phase is framework-owned. The watchdog/control loop drives task execution and code review. Hermes main sessions are operator consoles, not implementation drivers.
- `app-delivery control --goal auto` is the canonical operator routing surface.
- `app-delivery watch-status` is optional read-only visibility. It must not drive correctness decisions.
- Normal tasks stop at `review_pending` after tests pass. Review import decides whether a task becomes `verified`, returns to `pending`, or escalates.
- Machine precondition findings are represented as `review_type: machine-precondition` while preserving `external_review_status`.
- Repeated deferable machine preconditions may become `verified` with a deferred semantic-risk report. `security-access-control` can defer only when the current task or a later unfinished feature task owns auth/RBAC/access-control work.
- Manual `fix` and `task --action reset-repair` must refuse cross-task repairs while another unfinished task has task-scoped dirty changes. Do not auto-restore or auto-park another task's changes.

## Exception Handling

- When a task becomes `exception`, task-scoped dirty changes are parked under `.app-delivery-runtime/exception-patches/<TASK_ID>.patch` and restored from the worktree.
- `fix` and `reset-repair` try to reapply that patch before the next repair attempt.
- If the patch conflicts, the framework preserves the patch, writes `.app-delivery-runtime/exception-conflicts/<TASK_ID>.md`, sets `force_task_prompt_reason=exception_patch_conflict`, and lets the implementation runtime migrate the patch intent into current code.
- Exception patch conflict resolution belongs to the implementation runtime session, not the review runner.

## Canonical Artifacts

- `docs/work-items.json` is the task ledger.
- `docs/work-items.md` is the operator-readable rendering.
- `docs/test-results.json` is the test-evidence ledger.
- `.app-delivery-runtime/task-runtime/*.json` stores wrapper/runtime state.
- `.app-delivery-runtime/host-handoff.json` stores importable host planning/review handoffs.
- `.app-delivery-runtime/review-inputs/*.json` stores imported review payloads.
- `docs/reviews/*.md` stores code-review, final-review, exception, gate, and validation artifacts.

Runtime implementations must not self-certify by writing framework-owned review artifacts as proof of review.

## Development Rules

- Preserve task scope and root-cause behavior. Do not add broad refactors while fixing a narrow framework issue.
- Do not patch Hermes core. Use app-delivery skills, prompts, config, hooks, or the Python framework.
- Live project investigations are read-only by default. Do not run `control`, `fix`, `resume`, setup, deploy, or other state-changing commands unless explicitly asked.
- Use `apply_patch` for source edits.
- Do not revert user/project changes unless explicitly instructed.
- Keep `AGENTS.md` and `CLAUDE.md` synchronized.

## Commands

Use the repository environment explicitly:

```bash
cd /path/to/opc-agents/app-delivery
PYTHONPATH="$PWD" pipenv run pytest tests/unit/test_loop.py
PYTHONPATH="$PWD" pipenv run pytest tests/unit/test_cli_control.py
PYTHONPATH="$PWD" pipenv run pytest tests/unit/test_task.py -k "decompose_tasks or task_decompose"
```

Install locally:

```bash
scripts/setup-opc.sh --runtime opencode --opc-home "$HOME/opc" --framework-root "$PWD"
```

Remote deployment is environment-specific. Do not commit real hosts, usernames, IP addresses, or internal paths. Keep private inventory outside the repository and use placeholders in shared documentation:

```bash
rsync -az --delete --exclude '.git/' --exclude '.pytest_cache/' --exclude '**/__pycache__/' --exclude '*.pyc' ./ deploy-user@example.com:/opt/app-delivery-deploy-src/
ssh deploy-user@example.com 'cd /opt/app-delivery-deploy-src && scripts/setup-opc.sh --runtime opencode --opc-home /opt/opc --framework-root /opt/app-delivery-deploy-src'
```

Useful live-project checks:

```bash
$HOME/opc/bin/app-delivery control --goal status --project /path/to/project
$HOME/opc/bin/app-delivery pause --project /path/to/project
$HOME/opc/bin/app-delivery resume --project /path/to/project --runtime opencode
```
