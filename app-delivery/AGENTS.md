# app-delivery

## Project Identity

app-delivery is the first agent in the opc-agents family. It takes a software project's requirements document and runs a 24/7 delivery pipeline until all requirements are implemented, tested, and verified by independent code review.

The core delivery layer is a Python control plane (`delivery/` package) that manages ledgers, computes deterministic next steps, imports host-generated planning/review artifacts, owns implementation-session execution, persists wrapper-reported runtime state, and runs the framework-local watchdog. Hermes skills (or any equivalent skill host) are now thin entry points over the control plane plus the genuine planning/review host boundaries. Actual code implementation is done only by Claude Code or OpenCode sessions.

## Directory Structure

```
opc-agents/app-delivery/
├── delivery/             ← Python CLI package
│   ├── __init__.py
│   ├── __main__.py       ← CLI entrypoint
│   ├── state.py          ← JSON ledger management
│   ├── loop.py           ← delivery loop
│   ├── session.py        ← AI session lifecycle
│   ├── task.py           ← task model
│   ├── verify.py         ← test runner
│   └── scaffold.py       ← project scaffold generator
├── scripts/              ← setup/new-project/doctor shell entrypoints
├── skills/               ← Hermes operator skills + stage skills
│   ├── prompts/          ← LLM prompt assets used by stage skills and CLI bootstrap
├── tests/                ← 框架自测
│   ├── smoke/            ← 冒烟测试
│   └── unit/             ← 单元测试
├── project_temp/         ← project scaffold templates for `new-project.sh`
├── mock-server/          ← mock server template
└── docs/design/          ← design documents
```

## Key Design Principles

1. **Single main branch.** No per-task worktree isolation. Tasks execute in one shared repo, but not in one shared implementation session.
2. **Full-stack vertical slices.** Each task covers one complete feature (backend + frontend + tests). No frontend-only or backend-only task breakdown.
3. **Tests by same AI.** The AI that implements a feature also writes its tests. Constraints are: tests must be meaningful (weak command detection), must not be deleted, must be based on acceptance criteria.
4. **External independent review.** After tests pass, the core emits a review request and waits for a host-skill review artifact import.
5. **Task-bound sessions.** One task maps to one implementation session. Resume/repair for the same task keeps that session while the task stays actionable; unrelated tasks start their own sessions.
6. **Single public control surface.** Operator routing flows through `app-delivery control`; phase-specific operator skills are compatibility aliases, not the canonical API.
7. **Persistent runtime handoff state.** Wrapper-owned runtime facts are stored under `.app-delivery-runtime/task-runtime/` and are part of the control-plane routing contract.
8. **Canonical ledgers.** `docs/work-items.json` (task state) and `docs/test-results.json` (test evidence) remain canonical project ledgers; git commit SHA is the recovery baseline.

## Architecture

The Phase flow (see `docs/design/v2/01-ARCHITECTURE.md` for details):

```
Phase 0: host `spec-review` skill → imported requirements.json
Phase 1: host `arch-design` / `ui-design` / `project-context-sync` skills → imported design/context artifacts
Phase 2: host `task-decompose` skill → imported work-items.json (T000, T001, T002-T00N, T-FINAL)
Phase 3: T000 scaffold → complete file structure
Phase 4: T001 shared infrastructure → backend/src/shared/, frontend/src/shared/
Phase 5: T002-T00N feature delivery loop (one task = one implementation session)
Phase 6: final verify -> if review-ready, host `final-review` skill imports the final review result
```

## Code Conventions

- `delivery/` modules should be kept under ~500 lines each. Split if they grow beyond.
- State file paths: `docs/work-items.json`, `docs/test-results.json`, `docs/test-plan.json`, `docs/architecture-meta.json`
- CLI: `python3 -m delivery <command> --project <path>`
- Canonical operator routing command: `python3 -m delivery control --goal auto --project <path> [--requirements <path>]`
- Deployment scripts: `scripts/setup-opc.sh`, `scripts/new-project.sh`, `scripts/app-delivery-doctor.sh`, `scripts/app-delivery-preflight.sh`
- `setup-opc.sh` and `new-project.sh` require an explicit `--runtime claude|opencode`; the installed environment keeps a single active runtime.
- Canonical Hermes operator command: `/app-delivery`
- Hermes stage/review skills: `spec-review`, `arch-design`, `ui-design`, `project-context-sync`, `task-decompose`, `code-review`, `final-review`

## Design Documents

Read `docs/design/v2/00-OVERVIEW.md` first for context, then read the specific doc relevant to your task.

## Testing

Framework自测分两层：
- `tests/unit/` — 纯逻辑测试（`state.py`, `task.py`, `verify.py`），不依赖 AI runtime
- `tests/smoke/` — 冒烟测试，创建隔离临时项目跑完整交付流程

```bash
python3 -m pytest tests/
```

Keep CLAUDE.md and AGENTS.md in sync when directory structure changes.
