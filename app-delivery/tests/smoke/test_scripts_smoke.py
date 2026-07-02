from __future__ import annotations

import subprocess
from pathlib import Path
import tempfile
import os


SCRIPTS = [
    "setup-opc.sh",
    "new-project.sh",
    "app-delivery-doctor.sh",
    "app-delivery-preflight.sh",
    "cc-exec.sh",
    "oc-exec.sh",
]


def test_shell_scripts_have_valid_syntax() -> None:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    for name in SCRIPTS:
        result = subprocess.run(
            ["bash", "-n", str(scripts_dir / name)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, result.stdout


def test_shell_scripts_support_help() -> None:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    for name in SCRIPTS:
        result = subprocess.run(
            ["bash", str(scripts_dir / name), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, result.stdout
        assert "usage:" in result.stdout.lower()


def test_new_project_reports_init_project_errors_and_forwards_force() -> None:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        framework_root = temp_path / "framework"
        delivery_dir = framework_root / "delivery"
        delivery_dir.mkdir(parents=True)
        (delivery_dir / "__main__.py").write_text(
            """
from __future__ import annotations
import json
import sys

def main() -> int:
    args = sys.argv[1:]
    if '--force' in args:
        print(json.dumps({'status': 'ok', 'project_root': '/tmp/project', 'metadata_path': '/tmp/project/docs/project-bootstrap.json', 'runtime': 'opencode', 'stack': 'python-react', 'mock_server_path': '/tmp/project/mock-server'}))
        return 0
    print(json.dumps({'status': 'error', 'code': 'project_exists', 'message': 'project already exists', 'suggested_action': 'Pass --force to replace the project.'}))
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
""",
            encoding="utf-8",
        )
        opc_home = temp_path / "opc"
        state_dir = opc_home / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "active-runtime").write_text("opencode\n", encoding="utf-8")

        first = subprocess.run(
            ["bash", str(scripts_dir / "new-project.sh"), "--opc-home", str(opc_home), "--framework-root", str(framework_root), "demo"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert first.returncode == 1
        assert "project_exists: project already exists" in first.stdout
        assert "Suggested action: Pass --force" in first.stdout

        second = subprocess.run(
            ["bash", str(scripts_dir / "new-project.sh"), "--opc-home", str(opc_home), "--framework-root", str(framework_root), "--force", "demo"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={**os.environ, "APP_DELIVERY_SKIP_FRONTEND_INSTALL": "1"},
        )
        assert second.returncode == 0
        assert "Project root:" in second.stdout
