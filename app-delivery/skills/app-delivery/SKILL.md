---
name: app-delivery
description: Canonical operator entrypoint for app-delivery control-plane routing.
---

Use this skill for normal app-delivery operation.

Rules:
- Only use the delivery CLI.
- Treat `{project}` and `{requirements_path}` as authoritative absolute paths.
- Do not stop while `must_continue=true` unless the framework reports a real blocking condition.
- If a planning host skill returns a contract-style failure such as `stage_output_invalid` or `work_items_contract_invalid`, rerun the exact requested host skill once with the reported contract errors fed back into the prompt, then rerun `app-delivery control --goal auto`. Do not retry the same failing planning artifact more than once unless the artifact changed.
- When `control --goal auto` returns `must_continue=true` with `next_step.owner=host`, execute that host step immediately instead of only reporting it. Then rerun `app-delivery control --goal auto` and continue the same loop until the framework no longer requires immediate continuation or a real blocking condition occurs.
- For `next_step.skill=code-review`, call the `code-review` skill with the exact `project` and `task_id` from `next_step`, let it write/import the canonical JSON artifact, then rerun `app-delivery control --goal auto`.
- For `next_step.skill=final-review`, call the `final-review` skill with the exact `project` from `next_step`, let it write/import the canonical JSON artifact, then rerun `app-delivery control --goal auto`.
- For planning host skills (`spec-review`, `arch-design`, `ui-design`, `project-context-sync`, `task-decompose`), execute the requested skill, then rerun `app-delivery control --goal auto`. If the framework reports a contract-style planning failure, apply the existing one-retry rule above.
- Do not manually edit framework ledgers to simulate host-step completion; always satisfy host steps by running the requested skill so the core can import the canonical artifact.

Default run / continue:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal auto --project {project} --requirements {requirements_path}
```

If requirements have already been archived or the project is already in progress:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal auto --project {project}
```

Status-only inspection:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal status --project {project}
```

Pause:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal pause --project {project}
```

Explicit repair of a task or final-verify-routed repair target:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal repair --project {project} --task-id {task_id}
```

Direct final verification check:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery verify --project {project}
```

Report back:
- control status
- whether the framework must continue immediately
- the current `next_step`
- whether delivery may be claimed complete
- if blocked, whether the framework produced a repair candidate or repair report

Host-step loop:

1. Run the default `app-delivery control --goal auto ...` command.
2. If `must_continue=false`, stop and report status.
3. If `must_continue=true` and `next_step.owner=framework`, immediately rerun the requested framework step via `app-delivery control --goal auto ...` and continue looping.
4. If `must_continue=true` and `next_step.owner=host`, execute the indicated host skill immediately:
	- `code-review` -> run the `code-review` skill for `next_step.task_id`
	- `final-review` -> run the `final-review` skill
	- planning skills -> run the requested planning skill with the project path / requirements path from `next_step`
5. After the host skill finishes and imports its artifact, rerun `app-delivery control --goal auto ...`.
6. Repeat until the framework reports a genuine blocking condition, `must_continue=false`, or delivery is complete.