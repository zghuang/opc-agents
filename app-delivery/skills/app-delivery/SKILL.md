---
name: app-delivery
description: Canonical operator entrypoint for app-delivery control-plane routing.
---
Use this skill for normal app-delivery operation.

Rules:

- Only use the delivery CLI.
- Treat `{project}` as the authoritative project identifier. A single bare project name resolves through the CLI to `${OPC_HOME:-$HOME/opc}/projects/<name>`; an explicit path is used as-is.
- Treat `{requirements_path}` as the local requirements source file path. If the source is a URL or shared document, ask the operator to save or export it to a local file first.
- For `/app-delivery <project> <requirements-file>`, treat the first positional argument as `{project}` and the second positional argument as `{requirements_path}`.
- Never replace `{project}` with the parent directory of `{requirements_path}` when the user already supplied a first positional project argument.
- If `{requirements_path}` is invalid or missing, keep `{project}` unchanged and report the requirements-path error instead of inferring a different project.
- This skill and Hermes must not directly implement, edit, or validate target-project feature code. Target-project code changes must happen only inside app-delivery runtime task sessions.
- This skill and Hermes may only run the delivery CLI and the planning skills explicitly requested by `next_step`. During implementation, task execution and code-review are framework-managed: code-review runs through the framework review runner as an isolated oneshot, not through the Hermes main session. If target-project code changes are needed, rerun `app-delivery control --goal auto` so the assigned runtime task session performs that work.
- Do not stop while `must_continue=true` unless the framework reports a real blocking condition.
- On Hermes, `app-delivery control --goal auto ...` and `app-delivery control --goal repair ...` are long-running bounded commands. Launch them with Hermes-managed background execution: `terminal(command="...", background=true, notify_on_complete=true)`. Do not run them as foreground terminal calls, and do not use shell-level backgrounding such as `&`, `nohup`, `disown`, or `setsid`.
- On Hermes, after launching a managed background `control --goal auto ...` or repair command, you may launch one managed background read-only status observer for the same project: `terminal(command="${OPC_HOME:-$HOME/opc}/bin/app-delivery watch-status --project {project}", background=true, watch_patterns=["APP_DELIVERY_STATUS_CHANGE"])`.
- The companion status observer is optional visibility only. Framework correctness must not depend on it. When it fires, do at most one status-only inspection. Do not resume the control loop, import artifacts, or start host/framework actions from the observer alone.
- Outside Hermes, the bash snippets below are plain CLI commands. Use the equivalent managed-background/notify primitive if the host platform has one; otherwise run status polling explicitly.
- When enabled, the project watchdog is project-scoped and may run detached from Hermes; closing Hermes is not a watchdog stop operation.
- To stop automatic continuation, use `control --goal pause --project {project}`. For explicit immediate termination, only target processes whose command line contains the exact project path and `watchdog-run`, `oc-exec.py`, or that project's active runtime.
- If a planning host skill returns a contract-style failure such as `stage_output_invalid` or `work_items_contract_invalid`, rerun the exact requested host skill once with the reported contract errors fed back into the prompt, then rerun `app-delivery control --goal auto`. Do not retry the same failing planning artifact more than once unless the artifact changed.
- During implementation, `next_step.skill=code-review` is handled by the framework review runner. Do not run code-review from the Hermes main session unless the user explicitly asks for a manual override. Rerun `app-delivery control --goal auto` or inspect status; the framework will start an isolated review runner and import the canonical JSON artifact.
- Host-owned steps are persisted in `{project}/.app-delivery-runtime/host-handoff.json` for visibility. For code-review, expected statuses are `waiting_for_host`, `review_running`, and `imported`; `review_running` means the framework review runner has claimed the handoff. If `expected_input_path` already exists, do not import it manually unless the framework asks; rerun `app-delivery control --goal auto` so freshness checks and import rules are applied.
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

Task-level status observer:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery watch-status --project {project}
```

`watch-status` is read-only and does not advance the control loop. In the default Hermes path it may be launched only as a companion visibility process.

Pause:

```bash
${OPC_HOME:-$HOME/opc}/bin/app-delivery control --goal pause --project {project}
```

Immediate operator stop, only when explicitly requested:

```bash
project="{project}"
pkill -TERM -f "watchdog-run --project ${project}"
pkill -TERM -f "oc-exec.py --project-root ${project}"
```

Use this only for the exact project path; do not use broad `app-delivery`, runtime, or project-name-only patterns.

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

1. Start the default `app-delivery control --goal auto ...` command. On Hermes, start it as a managed background command with `background=true, notify_on_complete=true`.
2. Optionally start the companion `watch-status` observer for visibility. The observer is not required for correctness; do not use observer output to drive control flow.
3. If `must_continue=false`, stop and report status.
4. If `must_continue=true` and `next_step.owner=framework`, rerun the requested framework step via `app-delivery control --goal auto ...` using the same host execution mode and continue looping.
5. During implementation, do not run `code-review` from the Hermes main session. If a code-review handoff appears, rerun `app-delivery control --goal auto ...` or inspect status; the framework review runner will claim it, run isolated review, and import the result.
6. If `must_continue=true` and `next_step.owner=host` for non-code-review work, execute the indicated host skill immediately:
   - `final-review` -> run the `final-review` skill
   - planning skills -> run the requested planning skill with the project path / requirements path from `next_step`
7. After a non-code-review host skill finishes and imports its artifact, rerun `app-delivery control --goal auto ...` using the same host execution mode.
8. Repeat until the framework reports a genuine blocking condition, `must_continue=false`, or delivery is complete.
