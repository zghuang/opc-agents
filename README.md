# OPC Agents

OPC Agents is a repository for building and sharing OPC agents for enterprise domain teams. The long-term goal is to let different business functions create agents that fit their own goals, policies, data, collaboration patterns, and operating context.

The repository currently contains one production OPC agent: **app-delivery**. It focuses on application delivery because the first use case comes from the IT department; future OPC agents can target other enterprise domains as those teams define their own needs.

## app-delivery

app-delivery is a domain-agnostic delivery agent. It does not encode one specific project, department, or business domain. Instead, it is a control-plane framework that can take project requirements from any domain and drive a complete delivery lifecycle.

At a high level, app-delivery:

- normalizes requirements into structured project artifacts
- imports architecture, UI, test-plan, and task-decomposition stage outputs
- creates a project scaffold from the selected stack and Module Architecture
- runs implementation tasks through Claude Code or OpenCode sessions
- executes task-owned validation
- routes independent code review and final review through importable artifacts
- tracks state in durable ledgers under the project repository
- handles runtime stalls, exception patches, review repair, semantic preconditions, and production gates

## Why It Exists

Large AI-assisted delivery runs tend to fail when all state lives in a chat transcript. app-delivery makes the delivery process explicit and recoverable:

- Task state is stored in `docs/work-items.json`.
- Test evidence is stored in `docs/test-results.json`.
- Runtime state is stored under `.app-delivery-runtime/`.
- Reviews, gates, exception reports, and validation reports are stored under `docs/reviews/`.
- The framework computes the next deterministic step from project state instead of relying on memory.

## Ledger Philosophy

app-delivery is built around ledgers because long-running agent work needs durable state that survives model sessions, terminal restarts, host handoffs, and human intervention. A ledger is not just a log. It is the contract that tells the framework what has been requested, what was attempted, what passed, what failed, what is waiting for review, and what can safely happen next.

The core idea is simple: important delivery facts must live in project files, not in chat memory.

- `docs/work-items.json` records task ownership, status, dependencies, requirements, output paths, review state, and repair routing.
- `docs/test-results.json` records executable evidence instead of relying on a runtime's claim that tests passed.
- `.app-delivery-runtime/` records active runtime state, watchdog state, host handoffs, exception patches, locks, and recovery metadata.
- `docs/reviews/` records independent review, machine preconditions, final review, gate reports, and deferred risks.

This design makes delivery restartable and inspectable. The framework can stop, resume, repair, retry host-owned steps, reject stale artifacts, preserve exception patches, and explain why it is blocked. It also gives humans a concrete place to intervene: inspect the ledger, fix the project, rerun the control loop, or submit better framework behavior as a bug report or PR.

## Delivery Expectations

app-delivery is not a promise that AI will fully implement every requirement in a project. In practice, it is most useful for the front half of delivery: turning requirements into artifacts, scaffolding the project, driving implementation slices, running validation, surfacing review findings, and preserving recovery state.

Expect the framework and runtime agents to get a project meaningfully started and often to complete a substantial share of the engineering work, but not to replace human ownership. A realistic delivery still has two phases:

- app-delivery-in-the-loop: the framework drives planning, scaffold, implementation tasks, validation, and review loops.
- human-in-the-loop: engineers and domain owners finish edge cases, product judgment, integration details, production hardening, acceptance review, and release decisions.

Bug reports, failing cases, documentation fixes, and pull requests are welcome. The project will improve fastest if real users share where the framework falls short.

## Key Characteristics

### Domain Agnostic

app-delivery does not contain business logic for a specific domain. Domain meaning comes from each project's requirements, architecture, module docs, UI docs, and test plan.

### Framework-Owned Delivery Loop

The Python control plane owns task execution, validation, review import, recovery, and watchdog routing. Operator tools and skills are thin entry points over that control plane.

### Skill-Based Host Control

The framework controls process flow through skill rules and importable artifacts. Hermes is the current packaged skill host, but it is not a hard architectural dependency. The same control model can be adapted to other agent orchestration platforms such as OpenClaw by mapping their host capabilities to the same CLI, skill contracts, artifact imports, and runtime state.

Current Hermes-specific pieces are intentionally kept at the host-adapter boundary:

- packaged skills are installed under `~/.hermes/skills/opc-agents`
- long `control --goal auto` and `control --goal repair` commands use Hermes managed background execution with a completion signal
- Hermes skill rules guard the main host session from directly editing target-project feature code; target code changes must happen inside framework-managed runtime task sessions

The delivery core under `delivery/` remains the source of truth for ledgers, state transitions, imports, validation, review routing, recovery, and gates.

### Pluggable Implementation Runtimes

Implementation is delegated to runtime sessions. The current implementation supports Claude Code and OpenCode, and additional runtimes can be added by implementing the same wrapper, liveness, task-runtime, and guard contracts.

### Artifact-Based Handoffs

Planning and review boundaries are represented as files, not ephemeral chat state. This makes reviews repeatable, auditable, and importable.

### Independent Review

Implementation and review are separated. A task is not verified just because the implementation runtime says it is done; it must pass tests and independent review or an explicit deferred-risk path.

### Exception Patch Recovery

When a task fails into exception, task-scoped changes are parked as a patch under `.app-delivery-runtime/exception-patches/`. Later repair attempts reapply or reconcile that patch instead of silently losing prior work.

### Safety Gates

app-delivery includes production semantic checks, validation gates, dirty-worktree guards, machine-precondition handling, final verification, and release evidence generation.

## Repository Layout

```text
opc-agents/
+-- app-delivery/      # Current OPC delivery agent
`-- README.md          # This file
```

Inside `app-delivery/`:

```text
app-delivery/
+-- delivery/          # Python control-plane package
+-- skills/            # Current Hermes skill adapter and stage/review skill contracts
+-- scripts/           # install, setup, doctor, and wrapper scripts
+-- project_temp/      # scaffold templates
+-- mock-server/       # companion mock-server template
+-- tests/             # unit and smoke tests for the framework
`-- docs/              # framework design and operations documentation
```

## Basic Usage

### One-Time Host Setup

app-delivery assumes the host is already prepared. The setup script validates the environment, but it does not replace proper installation and authentication of external tools.

Install and configure these before running a real delivery project:

- Python 3.14.x available as `python3`
- Git
- Node.js, npm, and pnpm
- Docker with Compose support
- Hermes Agent, authenticated and available on `PATH`, for the current packaged skill-host integration
- At least one implementation runtime:
  - Claude Code, if using `--runtime claude`
  - OpenCode, if using `--runtime opencode`

If you use non-Claude official models, `opencode` is the recommended runtime. In current testing it has generally been cheaper for long implementation loops, and it integrates well with the framework's wrapper, guard, task-runtime state, and watchdog flow.

This repository's shipped operator path currently expects Hermes for packaged skills and host-side routing. Hermes should be understood as today's supported host adapter, not as the permanent boundary of the framework. A different host, for example OpenClaw, can integrate by providing equivalent skill invocation, managed long-running command behavior, status visibility, and guard semantics.

Install the framework into an OPC home:

```bash
cd app-delivery
scripts/setup-opc.sh --runtime opencode --opc-home "$HOME/opc" --framework-root "$PWD"
```

This installs wrapper commands under `$HOME/opc/bin`, deploys the current Hermes skill adapter under `~/.hermes/skills/opc-agents`, records the selected runtime, and runs a doctor check.

Use `--runtime claude` only when the host is intended to run Claude Code. Use `--runtime opencode` for OpenCode-based delivery.

### Create A Project

Create a new project under `$HOME/opc/projects`:

```bash
$HOME/opc/bin/new-project.sh --runtime opencode my-project "Short project description"
```

### Start Or Continue In Hermes

For normal operation, open Hermes and run the `app-delivery` skill:

```text
/app-delivery my-project /path/to/requirements.md
```

`my-project` is a project name. app-delivery resolves it to `$OPC_HOME/projects/my-project`. You can also pass an explicit project path if you need one.

The second argument is the local requirements source file. If the requirements live in a web page or shared document, export or save them to a local file first.

After the project has started, continue it with:

```text
/app-delivery my-project
```

The Hermes skill calls the control plane, runs planning skills when required, launches long-running implementation work through managed background execution, and imports review artifacts. Operators normally do not need to run low-level CLI commands directly.

### Optional CLI Operations

The CLI exists for status checks, repair, and troubleshooting. The common case is still the Hermes command above.

Common operator commands:

```bash
$HOME/opc/bin/app-delivery control --goal auto --project /path/to/project
$HOME/opc/bin/app-delivery control --goal status --project /path/to/project
$HOME/opc/bin/app-delivery control --goal pause --project /path/to/project
$HOME/opc/bin/app-delivery resume --project /path/to/project --runtime opencode
$HOME/opc/bin/app-delivery fix --project /path/to/project --task-id T012 --runtime opencode
```

Use `control --goal status` before intervening in a live project. It reports active tasks, review handoffs, blockers, and the next routable step.

Run framework tests from source:

```bash
cd app-delivery
PYTHONPATH="$PWD" pipenv run pytest tests/unit/
```

## Design Rationale

### Why Not Run Many Tasks In Parallel?

app-delivery is deliberately serial inside one project worktree. A task is a vertical slice that may touch backend, frontend, tests, mocks, and shared files. Running several such tasks at the same time in one repository would create conflicting edits, confusing review scope, duplicated migrations, overlapping test fixtures, and difficult recovery semantics.

The framework favors deterministic progress over raw concurrency:

- one active implementation task at a time
- one review boundary per task
- one canonical task ledger
- one recoverable worktree state

If parallel delivery is truly needed, run separate delivery loops on separate Git branches or separate project copies, then merge through ordinary engineering review and full validation. Do not run multiple active implementation tasks concurrently in the same worktree.

### Why Not Use Git Worktrees Per Task?

Per-task worktrees sound attractive, but they move complexity into merge and recovery:

- feature slices often edit shared backend, frontend, test, and config files
- scaffold and foundation tasks establish shared package structure
- dependency manifests and lockfiles conflict easily
- review and exception patches become harder to reconcile
- final integration still has to happen on one branch

app-delivery instead uses one main project worktree with strict task scopes, explicit ledgers, parked exception patches, dirty-worktree guards, and independent review. This keeps state visible and recoverable.

### Why Keep Planning And Review As Artifacts?

Planning and review outputs are files, not hidden chat state. This lets the framework validate schemas, import results deterministically, retry failed host handoffs, and audit what changed between attempts.

### Why Domain Agnostic?

The same delivery machinery should work for many project types. Domain-specific behavior belongs in requirements, architecture, module docs, UI docs, test plans, and generated tasks. app-delivery only enforces delivery quality, state management, recovery, and validation rules.

## License And Attribution

OPC Agents is released under the Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

If you use OPC Agents or app-delivery in a public project, article, demo, or derived framework, please mention the original project and link back to this repository. [CITATION.cff](CITATION.cff) is included for citation metadata. Stars are appreciated if the project is useful to you.

## Project Status

app-delivery is the first OPC agent and the only currently documented package in this repository. Future agents should be added with their own directories and README sections when they become part of the OPC agent family.
