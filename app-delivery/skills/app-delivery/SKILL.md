---
name: app-delivery
description: Canonical operator entrypoint for app-delivery control-plane routing.
---
Use this skill for normal app-delivery operation.

Rules:

- Only use the delivery CLI.
- Treat `{project}` and `{requirements_path}` as authoritative absolute paths.
- For `/app-delivery <project> <requirements-file>`, treat the first positional argument as `{project}` and the second positional argument as `{requirements_path}`.
- Never replace `{project}` with the parent directory of `{requirements_path}` when the user already supplied a first positional project argument.
- If `{requirements_path}` is invalid or missing, keep `{project}` unchanged and report the requirements-path error instead of inferring a different project.
- This skill and Hermes must not directly implement, edit, or validate target-project feature code. Target-project code changes must happen only inside app-delivery runtime task sessions.
- This skill and Hermes may only run the delivery CLI and the planning/review skills explicitly requested by `next_step`. If target-project code changes are needed, rerun `app-delivery control --goal auto` so the assigned runtime task session performs that work.
- Do not stop while `must_continue=true` unless the framework reports a real blocking condition.
- On Hermes, `app-delivery control --goal auto ...` and `app-delivery control --goal repair ...` are long-running bounded commands. Launch them with Hermes-managed background execution: `terminal(command="...", background=true, notify_on_complete=true)`. Do not run them as foreground terminal calls, and do not use shell-level backgrounding such as `&`, `nohup`, `disown`, or `setsid`.
- On Hermes, after launching a managed background `control --goal auto ...` or repair command, report that the delivery run is hosted in the background and wait for the completion notification. When the notification arrives, run status-only inspection, then continue any required host/framework step from the reported `next_step`.
- Outside Hermes, the bash snippets below are plain CLI commands. Use the equivalent managed-background/notify primitive if the host platform has one; otherwise run status polling explicitly.
- If a planning host skill returns a contract-style failure such as `stage_output_invalid` or `work_items_contract_invalid`, rerun the exact requested host skill once with the reported contract errors fed back into the prompt, then rerun `app-delivery control --goal auto`. Do not retry the same failing planning artifact more than once unless the artifact changed.
- When `control --goal auto` returns `must_continue=true` with `next_step.owner=host`, execute that host step immediately instead of only reporting it. Then rerun `app-delivery control --goal auto` and continue the same loop until the framework no longer requires immediate continuation or a real blocking condition occurs.
- Host-owned steps are also persisted in `{project}/.app-delivery-runtime/host-handoff.json` for visibility. Treat `status="waiting_for_host"` as an outstanding host action. If `expected_input_path` already exists, do not wait for the host again; immediately run the provided `import_command` or rerun `app-delivery control --goal auto` so the framework imports it. If the same handoff later has `retry_attempts > 0` / `retry_requested_at`, run the indicated host skill again using the current request artifact and overwrite any stale input. A host executor can mark it `running` when work starts, and the core marks it `imported` when the artifact is imported.
- For `next_step.skill=code-review`, call the `code-review` skill with the exact `project` and `task_id` from `next_step`, let it write/import the canonical JSON artifact, then rerun `app-delivery control --goal auto`.
- For `next_step.skill=final-review`, call the `final-review` skill with the exact `project` from `next_step`, let it write/import the canonical JSON artifact, then rerun `app-delivery control --goal auto`.
- For planning host skills (`spec-review`, `arch-design`, `ui-design`, `project-context-sync`, `task-decompose`), execute the requested skill, then rerun `app-delivery control --goal auto`. If the framework reports a contract-style planning failure, apply the existing one-retry rule above.
- Do not manually edit framework ledgers to simulate host-step completion; always satisfy host steps by running the requested skill so the core can import the canonical artifact.

Default run / continue command string:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal auto --project {project} --requirements {requirements_path}
```

If requirements have already been archived or the project is already in progress, command string:

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

Explicit repair of a task or final-verify-routed repair target, command string:

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

1. Start the default `app-delivery control --goal auto ...` command. On Hermes, start it as a managed background command with `background=true, notify_on_complete=true` and do not continue the loop until the completion notification arrives.
2. If `must_continue=false`, stop and report status.
3. If `must_continue=true` and `next_step.owner=framework`, rerun the requested framework step via `app-delivery control --goal auto ...` using the same host execution mode and continue looping.
4. If `must_continue=true` and `next_step.owner=host`, execute the indicated host skill immediately:
   - `code-review` -> run the `code-review` skill for `next_step.task_id`
   - `final-review` -> run the `final-review` skill
   - planning skills -> run the requested planning skill with the project path / requirements path from `next_step`
5. After the host skill finishes and imports its artifact, rerun `app-delivery control --goal auto ...` using the same host execution mode.
6. Repeat until the framework reports a genuine blocking condition, `must_continue=false`, or delivery is complete.
