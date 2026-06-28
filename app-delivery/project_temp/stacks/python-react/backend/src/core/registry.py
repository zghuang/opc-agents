"""Auto-discovery of feature-module routers and SQLAlchemy models.

Backend feature modules live under ``src/<module>/`` and expose, by convention:

- ``models.py`` with SQLAlchemy mapped classes (registered on ``Base.metadata``)
- ``router.py`` with a module-level ``router`` (a FastAPI ``APIRouter``)
- optional ``seed.py`` with async seed callables for local/dev validation data

This module discovers and imports those automatically so that ``main.py`` and
``core/database.py`` never have to be edited when a new module is added. That
removes the single biggest source of merge conflicts when modules are
implemented in parallel git worktrees, and guarantees every model is registered
for ``Base.metadata.create_all`` without a hand-maintained import list.

Adding a backend module is therefore just: drop ``src/<module>/router.py`` and
``src/<module>/models.py``. No shared-file edits required.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import importlib
import importlib.util
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter

# Packages under ``src/`` that are infrastructure, not discoverable feature
# modules. Everything else that is a package is treated as a feature module.
NON_FEATURE_PACKAGES = {"core", "shared", "tests"}


def _source_root_package() -> str:
    """Return the top-level source package name (e.g. ``"src"``).

    ``registry`` is imported as ``<root>.core.registry``; the source root is the
    package two levels up.
    """
    parent = __package__ or ""
    parts = [segment for segment in parent.split(".") if segment]
    if parts and parts[-1] == "registry":
        parts = parts[:-1]
    if parts and parts[-1] == Path(__file__).resolve().parent.name:
        if len(parts) >= 2:
            return ".".join(parts[:-1])
        try:
            return Path(__file__).resolve().parents[1].name
        except Exception:
            return parts[0]
    if len(parts) >= 2:
        return ".".join(parts[:-1])
    try:
        return Path(__file__).resolve().parents[1].name
    except Exception:
        return parent


def feature_module_names() -> list[str]:
    """Return sorted names of feature-module packages under the source root."""
    root_name = _source_root_package()
    if not root_name:
        return []
    root_pkg = importlib.import_module(root_name)
    names: list[str] = []
    for module_info in pkgutil.iter_modules(root_pkg.__path__):
        if not module_info.ispkg or module_info.name in NON_FEATURE_PACKAGES:
            continue
        names.append(module_info.name)
    return sorted(names)


def _submodule_exists(qualified_name: str) -> bool:
    try:
        return importlib.util.find_spec(qualified_name) is not None
    except ModuleNotFoundError:
        return False


def import_models() -> None:
    """Import every feature module's ``models.py`` so mappers register on Base.

    A module without a ``models.py`` is skipped. A ``models.py`` that fails to
    import (a real error) is allowed to propagate so the failure is loud.
    """
    root_name = _source_root_package()
    for name in feature_module_names():
        target = f"{root_name}.{name}.models"
        if _submodule_exists(target):
            importlib.import_module(target)


def iter_routers() -> list[APIRouter]:
    """Return the ``router`` object from every feature module's ``router.py``."""
    root_name = _source_root_package()
    routers: list[APIRouter] = []
    for name in feature_module_names():
        target = f"{root_name}.{name}.router"
        if not _submodule_exists(target):
            continue
        module = importlib.import_module(target)
        router = getattr(module, "router", None)
        if router is not None:
            routers.append(router)
    return routers


def iter_seeders() -> list[Callable[[Any], Awaitable[None]]]:
    """Return async seed callables from feature modules' optional ``seed.py``.

    The framework provides the hook, while each generated feature module owns its
    own data. Supported conventions are ``seed_data`` and more specific names
    like ``seed_auth_data`` / ``seed_rfq_data``.
    """
    root_name = _source_root_package()
    seeders: list[Callable[[Any], Awaitable[None]]] = []
    for name in feature_module_names():
        target = f"{root_name}.{name}.seed"
        if not _submodule_exists(target):
            continue
        module = importlib.import_module(target)

        candidates: list[str] = []
        if callable(getattr(module, "seed_data", None)):
            candidates.append("seed_data")
        candidates.extend(
            attr
            for attr in sorted(dir(module))
            if attr != "seed_data"
            and attr.startswith("seed_")
            and attr.endswith("_data")
            and callable(getattr(module, attr, None))
        )

        for attr in candidates:
            seeders.append(getattr(module, attr))
    return seeders
