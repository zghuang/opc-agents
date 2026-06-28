#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app_delivery_wrapper_common import run_observed_process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenCode execution wrapper for app-delivery")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--log-file")
    parser.add_argument("--task-id")
    parser.add_argument("--task-title")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opencode_bin = os.environ.get("APP_DELIVERY_OPENCODE_BIN", "opencode")
    prompt = sys.stdin.read()
    real_git = os.environ.get("APP_DELIVERY_REAL_GIT") or subprocess.run(
        ["/usr/bin/env", "which", "git"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout.strip() or "git"
    shim_dir = tempfile.mkdtemp(prefix="app-delivery-git-shim.")
    shim_path = Path(shim_dir) / "git"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cmd=${1:-}\n"
        "case \"$cmd\" in\n"
        "  add|commit|merge|rebase|reset|checkout|switch|cherry-pick|push|pull|fetch|branch|tag)\n"
        "    echo 'app-delivery runtime: git history/state mutation is owned by the framework' >&2\n"
        "    exit 2\n"
        "    ;;\n"
        "esac\n"
        f"exec {real_git} \"$@\"\n",
        encoding="utf-8",
    )
    shim_path.chmod(0o755)
    command = [
        opencode_bin,
        "run",
        "--format",
        "json",
        "--dangerously-skip-permissions",
    ]
    if args.session_id:
        command.extend(["--session", args.session_id])
    command.append(prompt)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    env["APP_DELIVERY_REAL_GIT"] = real_git
    completed = run_observed_process(
        command=command,
        cwd=Path(args.project_root).expanduser().resolve(),
        env=env,
        project_root=Path(args.project_root).expanduser().resolve(),
        runtime="opencode",
        session_id=str(args.session_id or ""),
        task_id=str(args.task_id or ""),
        task_title=str(args.task_title or ""),
        log_file=args.log_file,
    )
    sys.stdout.write(completed.stdout)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())