from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

def load_case_modules(namespace: dict[str, Any], source_file: str, *relative_paths: str) -> None:
    base = Path(source_file).resolve().parent
    for relative_path in relative_paths:
        path = base / relative_path
        spec = spec_from_file_location(f"{Path(source_file).stem}_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load case module: {path}")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, value in vars(module).items():
            if not name.startswith("test_"):
                continue
            try:
                value.__module__ = namespace.get("__name__", value.__module__)
            except Exception:
                pass
            namespace[name] = value
