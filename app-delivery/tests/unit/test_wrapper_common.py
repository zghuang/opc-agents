from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path


def test_run_observed_process_updates_runtime_state_when_session_id_is_observed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import app_delivery_wrapper_common as wrapper

    seen_payloads: list[dict[str, object]] = []
    original_write = wrapper.write_task_runtime_state

    def capture_write(path: Path, payload: dict[str, object]) -> None:
        seen_payloads.append(dict(payload))
        original_write(path, payload)

    monkeypatch.setattr(wrapper, "write_task_runtime_state", capture_write)

    script_path = tmp_path / "emit_session.py"
    script_path.write_text(
        "import json\n"
        "import time\n"
        "print(json.dumps({'sessionID': 'ses_runtime_seen'}), flush=True)\n"
        "time.sleep(0.1)\n",
        encoding="utf-8",
    )

    log_file = tmp_path / ".app-delivery-runtime" / "logs" / "wrapper.log"
    completed = wrapper.run_observed_process(
        command=[sys.executable, str(script_path)],
        cwd=tmp_path,
        env=os.environ.copy(),
        project_root=tmp_path,
        runtime="opencode",
        session_id="",
        task_id="T001",
        task_title="Shared infra",
        log_file=str(log_file),
    )

    assert completed.returncode == 0
    assert any(
        payload.get("status") == "running" and payload.get("session_id") == "ses_runtime_seen"
        for payload in seen_payloads
    )


def test_run_observed_process_terminates_runtime_group_when_parent_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import app_delivery_wrapper_common as wrapper

    popen_kwargs: dict[str, object] = {}
    killed: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 43210
        stdin = None
        stdout: list[str] = []

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self) -> int:
            time.sleep(0.05)
            self.returncode = -signal.SIGTERM
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -signal.SIGTERM

    fake_process = FakeProcess()

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return fake_process

    parent_pids = iter([9876, 1, 1, 1])

    monkeypatch.setattr(wrapper.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wrapper.os, "getppid", lambda: next(parent_pids, 1))
    monkeypatch.setattr(wrapper.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(wrapper, "PARENT_WATCH_INTERVAL_SECONDS", 0.01)

    script_path = tmp_path / "noop.py"
    script_path.write_text("pass\n", encoding="utf-8")

    completed = wrapper.run_observed_process(
        command=[sys.executable, str(script_path)],
        cwd=tmp_path,
        env=os.environ.copy(),
        project_root=tmp_path,
        runtime="opencode",
        session_id="",
        task_id="T001",
        task_title="Shared infra",
        log_file=None,
    )

    assert popen_kwargs["start_new_session"] is True
    assert killed == [(fake_process.pid, signal.SIGTERM)]
    assert completed.returncode == -signal.SIGTERM
