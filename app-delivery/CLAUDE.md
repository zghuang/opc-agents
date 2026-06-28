# app-delivery

## Project Identity

app-delivery is the first agent in the opc-agents family. It takes a software project's requirements document and runs a 24/7 delivery pipeline until all requirements are implemented, tested, and verified by independent code review.

The core idea: a thin Python CLI (`delivery/` package) manages task state and AI sessions. Hermes skills provide operator entry points and cron monitoring. The actual implementation is done by Claude Code / OpenCode sessions.

## Directory Structure

```
opc-agents/app-delivery/
├── delivery/                    ← Python CLI package (python3 -m delivery ...)
│   ├── __init__.py
│   ├── __main__.py              ← CLI entrypoint
│   ├── state.py                 ← work-items.json + test-results.json management
│   ├── loop.py                  ← delivery loop core
│   ├── session.py               ← AI session (CC/OC) lifecycle
│   ├── task.py                  ← task model, decomposition
│   ├── verify.py                ← test runner, weak command detection
│   └── scaffold.py              ← T000 scaffold generator
│   ├── scripts/                 ← setup/new-project/doctor shell entrypoints
│   ├── skills/                   ← canonical operator skill + stage/review skills
│   │   ├── app-delivery/
│   │   ├── spec-review/
│   │   ├── arch-design/
│   │   ├── ui-design/
│   │   ├── project-context-sync/
│   │   ├── task-decompose/
│   │   ├── code-review/
│   │   └── final-review/
│   ├── tests/                    ← 框架自测
│   │   ├── smoke/                ← 冒烟测试（继承 m-opc 的 opc-smoke-test.py）
│   │   └── unit/                 ← 单元测试
│   ├── project_temp/             ← 项目脚手架模板目录
├── project_temp/                ← project scaffold assets for `new-project.sh`
│   ├── stacks/python-react/     ← default tech stack template
│   └── test-skeleton/           ← optional generic test scaffold assets
├── mock-server/                 ← mock server template (inherited from m-opc)
└── docs/design/
    ├── v1/                      ← original architect's design (archived)
    └── v2/                      ← current design
```

## Code Conventions

**Module boundaries** (read `docs/design/v2/01-ARCHITECTURE.md` for full detail):

| Module | Responsibility | Core functions |
|--------|---------------|----------------|
| `state.py` | JSON ledger R/W + atomic write + file locks | `load_work_items`, `save_work_items`, `load_test_results`, `save_test_results`, `acquire_lock` |
| `task.py` | Task model, ID management, dependency resolution | `load_task_ledger`, `pick_next_task`, `mark_task`, `check_requirements_coverage` |
| `session.py` | AI session create/reuse/rotate | `create_session`, `get_or_create_session`, `execute_in_session`, `should_rotate` |
| `verify.py` | Run tests, collect results, weak command detection | `run_tests`, `run_full_suite`, `collect_failures`, `weak_command_reason` |
| `loop.py` | Delivery loop orchestration | `DeliveryLoop.run()`, `_execute_task`, `_build_task_prompt`, `recover` |
| `scaffold.py` | T000 scaffold generation | Generate file structure from architecture + requirements |

**State files** (read `docs/design/v2/04-STATE-MODEL.md` for full schema):
- `docs/work-items.json` — task state (pending/active/done/verified/blocked/cancelled)
- `docs/test-results.json` — test run evidence
- `docs/test-plan.json` — requirement-to-test mapping (planning only, not execution)
- `docs/work-items.md` — auto-generated human-readable view

**Key design decisions** (read `docs/design/v2/06-KEY-DECISIONS.md`):
- No per-task worktree isolation. Single main branch, continuous AI session.
- Tasks are full-stack vertical slices, not split by frontend/backend.
- Tests are written by the same AI session that wrote the code — normal practice.
- Independent code-review via separate Hermes sub-agent (no implementation context).
- Core/shared separation: `core/` (framework infra, don't touch), `shared/` (project reusable, free to modify).
- Two-layer verification: deterministic test execution (pytest exit code) + AI code review (fresh perspective).

**Inherited from m-opc** (reusable modules):
- `opc_ledger_state.py` → atomic JSON write, fcntl lock, JSON load/save patterns
- `opc_requirements.py` → REQ-ID regex extraction, range expansion, markdown parsing
- `opc_completion_contract.py` → weak command detection (echo/true/ls/python3 -c)
- `opc_exec_common.py` → subprocess environment isolation, failure signature normalization, pyproject.toml parsing
- `cc-exec.py` / `oc-exec.py` → runtime adapter (CLI wrapper for CC/OC)

**Not inherited** (removed from m-opc):
- `opc_task_contract.py` (987 lines) — not needed, task contract is 8 JSON fields
- `opc_context_pack.py` — not needed, AI reads full code directly
- All 5 control-plane modules (6,433 lines total) — replaced by thin CLI
- Per-task worktree, framework recovery, dispatch, graph, complexity gate

## Design Docs

All detailed design decisions are in `docs/design/v2/`. Read these before making architectural changes:

1. `00-OVERVIEW.md` — Core principles, what changed from m-opc
2. `01-ARCHITECTURE.md` — Component model, Phase flow, core/shared rules, T001 design
3. `02-TASK-MODEL.md` — Task decomposition, built-in tasks, requirement mapping, blocked handling
4. `03-DELIVERY-LOOP.md` — Loop algorithm, session management, Hermes vs standalone
5. `04-STATE-MODEL.md` — State schemas, Git management, test types
6. `05-BUILD-PLAN.md` — Implementation roadmap (pilot project first)
7. `06-KEY-DECISIONS.md` — Key design decisions and their rationale
8. `07-SKILLS.md` — Hermes skill design for each entry point

## Testing the Framework

The delivery package tests itself at two levels:

**Unit tests** (`tests/unit/`): Pure logic tests for `state.py`, `task.py`, `verify.py`. No AI runtime dependency. Run with:

```bash
python3 -m pytest tests/unit/
```

**Smoke test** (`tests/smoke/`): Inherited from m-opc's `opc-smoke-test.py`. Creates an isolated temp project directory, runs the full delivery pipeline (scaffold → loop → verify) against a sample requirements file, and checks that outputs are correct:

```bash
python3 -m pytest tests/smoke/
```

Run all tests:
```bash
python3 -m pytest tests/
```

For integration: create a temp project and run `python3 -m delivery start --project /tmp/test-project --requirements <path>`, verify output.

## Objective Judgment Rule

- Do not implement features because they "might be useful." Every feature must solve a known problem from a real project.
- If a change makes the framework more complex without corresponding delivery improvement, argue against it.
- Prefer evidence from running code, test output, and real project delivery over theoretical elegance.
- If the user proposes something that contradicts the design docs, evaluate the trade-off honestly.
