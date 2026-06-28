from __future__ import annotations

import subprocess
from pathlib import Path


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
