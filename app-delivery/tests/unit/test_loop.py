from __future__ import annotations

from pathlib import Path
import runpy


def _load_case_modules(*relative_paths: str) -> None:
    base = Path(__file__).resolve().parent
    for relative_path in relative_paths:
        path = base / relative_path
        namespace = runpy.run_path(str(path))
        for name, value in namespace.items():
            if not name.startswith("test_"):
                continue
            try:
                value.__module__ = __name__
            except Exception:
                pass
            globals()[name] = value


_load_case_modules(
    "_loop_cases_prompts.py",
    "_loop_cases_review_execute.py",
    "_loop_cases_git_final.py",
    "_loop_cases_recover_run.py",
)
