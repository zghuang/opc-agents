from __future__ import annotations

import os
import re
import shutil
import subprocess
import json
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .runtime_config import load_project_runtime
from .production_semantics import mock_only_browser_e2e_issues
from .stack_contracts import PYTHON_REACT_CONTRACT, backend_test_root
from .state import load_test_results
from .task import FINAL_VERIFY_TASK_ID, PREFINAL_AUDIT_OUTPUT_PATHS, PREFINAL_AUDIT_REPORT_PATH, PREFINAL_AUDIT_TASK_ID, SCAFFOLD_OUTPUT_PATHS, Task, reset_task
from .verify import is_path_test_spec


FEATURE_INTEGRATION_ENTRY_PATHS = (
    "frontend/src/App.tsx",
    "frontend/src/App.test.tsx",
    "frontend/src/layouts/AppShell.tsx",
)


ALWAYS_ALLOWED_FRAMEWORK_PATHS = {
    ".app-delivery-runtime/session-state.json",
    "app-delivery-runtime/session-state.json",
    "docs/project-structure.md",
    "docs/test-plan.json",
    "docs/gates.json",
    "docs/test-results.json",
    "docs/work-items.json",
    "docs/work-items.md",
    "docs/project-summary.json",
    "docs/project-summary.md",
    "CLAUDE.md",
    "AGENTS.md",
    "docs/reviews/final-repair-report.md",
}

T000_ONLY_FRAMEWORK_PATHS = {
    "docs/requirements-source.md",
    "docs/requirements.json",
    "docs/clarification-needed.md",
    "docs/clarification-answers.md",
    "docs/architecture.md",
    "docs/shared-components.md",
    "docs/architecture-meta.json",
    "docs/test-plan.json",
    "CLAUDE.md",
    "AGENTS.md",
}
ALWAYS_ALLOWED_FRAMEWORK_PREFIXES = (
    ".app-delivery-runtime/locks/",
    "app-delivery-runtime/locks/",
    ".app-delivery-runtime/prompts/",
    "app-delivery-runtime/prompts/",
    "docs/reviews/code-review-",
    "docs/reviews/exception-report-",
    "docs/reviews/gate-report-",
    "docs/reviews/test-report-",
)

RUNTIME_PROTECTED_FRAMEWORK_ARTIFACT_FILES = {
    "docs/reviews/final-review.md",
    "docs/reviews/final-repair-report.md",
}

RUNTIME_PROTECTED_FRAMEWORK_ARTIFACT_PREFIXES = (
    "docs/reviews/code-review-",
    "docs/reviews/exception-report-",
)

RUNTIME_PROTECTED_TASK_TEST_REPORT_RE = re.compile(r"^docs/reviews/test-report-T[A-Za-z0-9_-]+\.md$")

T000_ONLY_FRAMEWORK_PREFIXES = (
    ".app-delivery-runtime/stage-inputs/",
    "app-delivery-runtime/stage-inputs/",
    "docs/modules/",
    "docs/adr/",
    "docs/ui/",
)

RUNTIME_LOCAL_ONLY_PREFIXES = (
    ".app-delivery-runtime/",
    "app-delivery-runtime/",
)

EXCEPTION_PATCHES_DIR = Path(".app-delivery-runtime") / "exception-patches"
EXCEPTION_CONFLICTS_DIR = Path(".app-delivery-runtime") / "exception-conflicts"
GIT_INDEX_LOCK_RETRY_SECONDS = 10.0
GIT_INDEX_LOCK_RETRY_INTERVAL_SECONDS = 0.5
MAX_EXCEPTION_PATCH_CONFLICT_OUTPUT_CHARS = 4000


def _git_index_lock_error(output: str) -> bool:
    lowered = str(output or "").casefold()
    return ".git/index.lock" in lowered or ("index.lock" in lowered and "file exists" in lowered)


def _git_index_lock_retry_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("APP_DELIVERY_GIT_INDEX_LOCK_RETRY_SECONDS") or GIT_INDEX_LOCK_RETRY_SECONDS))
    except ValueError:
        return GIT_INDEX_LOCK_RETRY_SECONDS


def git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + _git_index_lock_retry_seconds()
    while True:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0 or not _git_index_lock_error(completed.stdout):
            return completed
        if time.monotonic() >= deadline:
            return completed
        time.sleep(GIT_INDEX_LOCK_RETRY_INTERVAL_SECONDS)


def git_head_sha(project_root: Path | str) -> str:
    completed = git(["rev-parse", "HEAD"], cwd=Path(project_root).expanduser().resolve())
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_commit_subject(project_root: Path | str, commit: str = "HEAD") -> str:
    completed = git(["show", "-s", "--format=%s", commit], cwd=Path(project_root).expanduser().resolve())
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_latest_task_commit(project_root: Path | str, task_id: str) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    prefixes = [f"feat({task_id}):"]
    if task_id == "T000":
        prefixes.insert(0, f"chore({task_id}):")
    completed = git(["log", "--format=%H%x09%s", "--"], cwd=project_dir)
    if completed.returncode != 0:
        return ""
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        commit_sha, subject = line.split("\t", 1)
        if any(subject.startswith(prefix) for prefix in prefixes):
            return commit_sha.strip()
    return ""


def git_commit_timestamp(project_root: Path | str, commit_sha: str) -> str | None:
    if not commit_sha:
        return None
    project_dir = Path(project_root).expanduser().resolve()
    completed = git(["show", "-s", "--format=%cI", commit_sha], cwd=project_dir)
    if completed.returncode != 0:
        return None
    timestamp = completed.stdout.strip()
    return timestamp or None


def review_artifact_status(project_root: Path | str, task_id: str) -> tuple[str | None, str | None]:
    project_dir = Path(project_root).expanduser().resolve()
    review_path = project_dir / "docs" / "reviews" / f"code-review-{task_id}.md"
    if not review_path.exists():
        return None, None
    try:
        text = review_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    status_value: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("status:"):
            status_value = line.split(":", 1)[1].strip()
            break
    return status_value, str(review_path.relative_to(project_dir))


def _review_file_status(review_path: Path) -> str | None:
    if not review_path.exists():
        return None
    try:
        text = review_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


def verified_task_issue(project_root: Path | str, task: Task) -> str | None:
    if task.status != "verified":
        return None
    project_dir = Path(project_root).expanduser().resolve()
    if task.id == FINAL_VERIFY_TASK_ID:
        final_review_path = project_dir / "docs" / "reviews" / "final-review.md"
        final_review_status = str(_review_file_status(final_review_path) or "").strip().casefold()
        if final_review_status != "pass":
            return "missing pass final review evidence"
        if not (project_dir / "docs" / "release-evidence.md").exists():
            return "missing release evidence"
        full_suite = load_test_results(project_root).get("full_suite_results")
        if not isinstance(full_suite, dict) or not bool(full_suite.get("passed")):
            return "missing passing full-suite evidence"
        return None

    commit_ok = bool(task.git_commit and git_commit_timestamp(project_root, task.git_commit))
    review_status_value, review_artifact = review_artifact_status(project_root, task.id)
    review_ok = bool(review_artifact) and str(review_status_value or "").strip().casefold() == "pass"
    semantic_issues = mock_only_browser_e2e_issues(project_root, task.output_tests)
    if semantic_issues:
        return semantic_issues[0]
    if commit_ok and review_ok:
        return None
    if not commit_ok and not review_ok:
        return "missing task commit and pass code review evidence"
    if not commit_ok:
        return "missing task commit evidence"
    return "missing pass code review evidence"


def repair_invalid_verified_tasks(project_root: Path | str, tasks: list[Task]) -> tuple[list[Task], dict[str, str]]:
    issues: dict[str, str] = {}
    for task in tasks:
        issue = verified_task_issue(project_root, task)
        if not issue:
            continue
        issues[task.id] = issue
    return tasks, issues


def ensure_git_repo(project_root: Path | str) -> None:
    project_dir = Path(project_root).expanduser().resolve()
    if (project_dir / ".git").exists():
        return
    git(["init"], cwd=project_dir)
    git(["config", "user.name", "app-delivery"], cwd=project_dir)
    git(["config", "user.email", "app-delivery@local"], cwd=project_dir)


def _normalize_scope_path(path: str) -> str:
    normalized = str(path or "").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _path_in_scope(path: str, scope: str) -> bool:
    normalized_path = _normalize_scope_path(path)
    normalized_scope = _normalize_scope_path(scope).rstrip("/")
    if not normalized_scope:
        return False
    return normalized_path == normalized_scope or normalized_path.startswith(normalized_scope + "/")


def _is_always_allowed_framework_path(path: str) -> bool:
    normalized = _normalize_scope_path(path)
    if normalized in ALWAYS_ALLOWED_FRAMEWORK_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in ALWAYS_ALLOWED_FRAMEWORK_PREFIXES)


def _is_runtime_protected_framework_artifact(path: str) -> bool:
    normalized = _normalize_scope_path(path)
    if normalized in RUNTIME_PROTECTED_FRAMEWORK_ARTIFACT_FILES:
        return True
    if any(normalized.startswith(prefix) and normalized.endswith(".md") for prefix in RUNTIME_PROTECTED_FRAMEWORK_ARTIFACT_PREFIXES):
        return True
    return bool(RUNTIME_PROTECTED_TASK_TEST_REPORT_RE.fullmatch(normalized))


def is_runtime_protected_framework_artifact(path: str) -> bool:
    return _is_runtime_protected_framework_artifact(path)


def _is_t000_only_framework_path(path: str) -> bool:
    normalized = _normalize_scope_path(path)
    if normalized in T000_ONLY_FRAMEWORK_PATHS:
        return True
    normalized_path = Path(normalized)
    if normalized_path.parent == Path("docs") and normalized_path.name.endswith("-raw.json"):
        return True
    return any(normalized.startswith(prefix) for prefix in T000_ONLY_FRAMEWORK_PREFIXES)


def _is_runtime_local_only_path(path: str) -> bool:
    normalized = _normalize_scope_path(path)
    return any(normalized.startswith(prefix) for prefix in RUNTIME_LOCAL_ONLY_PREFIXES)


def git_changed_paths(project_root: Path | str) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    completed = git(["status", "--porcelain", "--untracked-files=all"], cwd=project_dir)
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        normalized = _normalize_scope_path(raw_path)
        if normalized:
            paths.append(normalized)
    return paths


def git_status_entries(project_root: Path | str) -> list[tuple[str, str]]:
    project_dir = Path(project_root).expanduser().resolve()
    completed = git(["status", "--porcelain", "--untracked-files=all"], cwd=project_dir)
    entries: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        normalized = _normalize_scope_path(raw_path)
        if normalized:
            entries.append((status, normalized))
    return entries


def _git_paths_in_head(project_root: Path | str, paths: list[str]) -> set[str]:
    project_dir = Path(project_root).expanduser().resolve()
    normalized_paths = [_normalize_scope_path(path) for path in paths if _normalize_scope_path(path)]
    if not normalized_paths or not git_head_sha(project_dir):
        return set()
    completed = git(["ls-tree", "-r", "--name-only", "HEAD", "--", *normalized_paths], cwd=project_dir)
    if completed.returncode != 0:
        return set()
    return {
        normalized
        for line in completed.stdout.splitlines()
        if (normalized := _normalize_scope_path(line))
    }


def _git_paths_in_index(project_root: Path | str, paths: list[str]) -> set[str]:
    project_dir = Path(project_root).expanduser().resolve()
    normalized_paths = [_normalize_scope_path(path) for path in paths if _normalize_scope_path(path)]
    if not normalized_paths:
        return set()
    completed = git(["ls-files", "--stage", "--", *normalized_paths], cwd=project_dir)
    if completed.returncode != 0:
        return set()
    listed: set[str] = set()
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        normalized = _normalize_scope_path(line.split("\t", 1)[1])
        if normalized:
            listed.add(normalized)
    return listed


def _classify_paths_for_head_restore(project_root: Path | str, paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    project_dir = Path(project_root).expanduser().resolve()
    normalized_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        normalized = _normalize_scope_path(raw_path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            normalized_paths.append(normalized)
    if not normalized_paths:
        return [], [], []

    status_paths = {path for _, path in git_status_entries(project_dir)}
    head_paths = _git_paths_in_head(project_dir, normalized_paths)
    index_paths = _git_paths_in_index(project_dir, normalized_paths)

    restore_paths: list[str] = []
    unstage_then_remove_paths: list[str] = []
    remove_paths: list[str] = []
    for path in normalized_paths:
        file_path = project_dir / path
        if path in head_paths:
            restore_paths.append(path)
            continue
        if path not in status_paths and path not in index_paths and not file_path.exists() and not file_path.is_symlink():
            continue
        if path in index_paths:
            unstage_then_remove_paths.append(path)
        remove_paths.append(path)
    return restore_paths, unstage_then_remove_paths, remove_paths


def _implicit_python_package_paths(paths: list[str]) -> list[str]:
    inferred: list[str] = []
    for raw_path in paths:
        normalized = _normalize_scope_path(raw_path)
        if not normalized.endswith(".py"):
            continue
        path = Path(normalized)
        if path.name == "__init__.py" or len(path.parts) < 2:
            continue
        inferred.append(str(path.parent / "__init__.py"))
    return inferred


def _implicit_feature_support_paths(paths: list[str], tests: list[str]) -> list[str]:
    inferred: list[str] = []
    for raw_path in paths:
        normalized = _normalize_scope_path(raw_path)
        path = Path(normalized)
        parts = path.parts
        if len(parts) >= 5 and parts[0] == "frontend" and parts[1] == "src" and parts[2] == "features" and parts[4] == "pages":
            feature = parts[3]
            inferred.extend(
                [
                    f"frontend/src/features/{feature}/api.ts",
                    f"frontend/src/features/{feature}/components/",
                ]
            )

    for raw_path in tests:
        normalized = _normalize_scope_path(raw_path)
        path = Path(normalized)
        parts = path.parts
        backend_tests_root = backend_test_root(PYTHON_REACT_CONTRACT)
        backend_tests_parts = PurePosixPath(backend_tests_root).parts
        if len(parts) >= len(backend_tests_parts) + 1 and parts[: len(backend_tests_parts)] == backend_tests_parts and path.name.startswith("test_") and path.suffix == ".py":
            feature = parts[len(backend_tests_parts)]
            inferred.append(f"{backend_tests_root}/{feature}/conftest.py")
    return inferred


def task_commit_paths(task: Task, *, extra_paths: list[str] | None = None) -> list[str]:
    if task.id == PREFINAL_AUDIT_TASK_ID:
        paths = [*PREFINAL_AUDIT_OUTPUT_PATHS, PREFINAL_AUDIT_REPORT_PATH]
        if extra_paths:
            paths.extend(path for path in extra_paths if _normalize_scope_path(path))
        seen: set[str] = set()
        ordered: list[str] = []
        for path in paths:
            normalized = _normalize_scope_path(path)
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered
    paths = [path for path in task.output_paths if _normalize_scope_path(path)]
    paths.extend(path for path in task.output_tests if is_path_test_spec(path))
    if task.id == "T000":
        paths.extend(SCAFFOLD_OUTPUT_PATHS)
    elif task.id not in {"T001", "T-FINAL"}:
        paths.extend(FEATURE_INTEGRATION_ENTRY_PATHS)
    if extra_paths:
        paths.extend(path for path in extra_paths if _normalize_scope_path(path))
    paths.extend(_implicit_feature_support_paths(task.output_paths, task.output_tests))
    paths.extend(_implicit_python_package_paths(paths))
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        normalized = _normalize_scope_path(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def task_scoped_changed_paths(project_root: Path | str, task: Task, *, extra_paths: list[str] | None = None) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    changed_paths = git_changed_paths(project_dir)
    scopes = scaffold_commit_paths(project_dir) if task.id == "T000" else task_commit_paths(task, extra_paths=extra_paths)
    scoped = [path for path in changed_paths if any(_path_in_scope(path, scope) for scope in scopes)]
    if task.id == "T000":
        scoped.extend(path for path in changed_paths if _is_t000_only_framework_path(path) and path not in scoped)
    explicit = {_normalize_scope_path(path) for path in (extra_paths or []) if _normalize_scope_path(path)}
    scoped = [path for path in scoped if not _is_runtime_protected_framework_artifact(path) or path in explicit]
    return scoped


def task_scope_delta(
    project_root: Path | str,
    task: Task,
    *,
    extra_paths: list[str] | None = None,
    preserved_paths: list[str] | None = None,
) -> dict[str, list[str]]:
    project_dir = Path(project_root).expanduser().resolve()
    changed_paths = git_changed_paths(project_dir)
    scopes = scaffold_commit_paths(project_dir) if task.id == "T000" else task_commit_paths(task, extra_paths=extra_paths)
    scoped_paths = [path for path in changed_paths if any(_path_in_scope(path, scope) for scope in scopes)]
    explicit = {_normalize_scope_path(path) for path in (extra_paths or []) if _normalize_scope_path(path)}
    always_allowed_paths = [path for path in changed_paths if _is_always_allowed_framework_path(path) and (not _is_runtime_protected_framework_artifact(path) or path in explicit)]
    t000_only_paths = [path for path in changed_paths if task.id == "T000" and _is_t000_only_framework_path(path)]
    runtime_local_paths = [path for path in changed_paths if _is_runtime_local_only_path(path)]
    preserved = {_normalize_scope_path(path) for path in (preserved_paths or []) if _normalize_scope_path(path)}
    allowed_paths = {*(scoped_paths), *(always_allowed_paths), *(t000_only_paths), *(runtime_local_paths)}
    out_of_scope = [path for path in changed_paths if path not in allowed_paths and path not in preserved]
    staged_paths = [path for path in changed_paths if path in allowed_paths and path not in runtime_local_paths]
    return {
        "changed_paths": changed_paths,
        "staged_paths": staged_paths,
        "out_of_scope": out_of_scope,
        "preserved_paths": sorted(preserved),
    }


def task_exception_patch_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    return project_dir / EXCEPTION_PATCHES_DIR / f"{task_id}.patch"


def task_exception_conflict_brief_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    return project_dir / EXCEPTION_CONFLICTS_DIR / f"{task_id}.md"


def _truncate_exception_patch_output(text: str) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= MAX_EXCEPTION_PATCH_CONFLICT_OUTPUT_CHARS:
        return normalized
    return normalized[:MAX_EXCEPTION_PATCH_CONFLICT_OUTPUT_CHARS].rstrip() + "\n[truncated]"


def _patch_changed_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_line in str(patch_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        candidates = [parts[3], parts[2]]
        for candidate in candidates:
            if candidate == "/dev/null":
                continue
            if candidate.startswith("b/") or candidate.startswith("a/"):
                candidate = candidate[2:]
            normalized = _normalize_scope_path(candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
            break
    return paths


def clear_task_exception_patch_conflict(project_root: Path | str, task_id: str, *, remove_patch: bool = False) -> None:
    conflict_path = task_exception_conflict_brief_path(project_root, task_id)
    if conflict_path.exists():
        conflict_path.unlink()
    if remove_patch:
        patch_path = task_exception_patch_path(project_root, task_id)
        if patch_path.exists():
            patch_path.unlink()


def _write_exception_patch_conflict_brief(
    project_root: Path | str,
    task_id: str,
    *,
    patch_path: Path,
    affected_paths: list[str],
    git_output: str,
) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    brief_path = task_exception_conflict_brief_path(project_dir, task_id)
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    patch_relative_path = str(patch_path.relative_to(project_dir))
    affected_summary = ", ".join(affected_paths) if affected_paths else "unknown"
    output = _truncate_exception_patch_output(git_output)
    lines = [
        f"# Exception Patch Conflict: {task_id}",
        "",
        f"- Task: {task_id}",
        f"- Patch: {patch_relative_path}",
        f"- Current HEAD: {git_head_sha(project_dir) or 'unknown'}",
        f"- Affected paths: {affected_summary}",
        "",
        "## Runtime Handling",
        "",
        "- Resolve this inside the current implementation runtime session before ordinary task coding continues.",
        "- Do not invoke an external review runner for this merge step.",
        "- Do not add Git conflict markers; edit the current source files directly to preserve the patch intent against current HEAD.",
        "- Keep the current verified task code unless the patch intent requires a narrow compatible change.",
        "- After migrating the patch intent, continue the task implementation and declared tests normally.",
        "",
        "## git apply --check Output",
        "",
        "```text",
        output or "git apply --check failed without output",
        "```",
    ]
    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(brief_path.relative_to(project_dir))


def _remove_path_if_exists(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def restore_paths_to_head(project_root: Path | str, paths: list[str]) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    normalized_paths = [_normalize_scope_path(path) for path in paths if _normalize_scope_path(path)]
    if not normalized_paths:
        return []
    restore_paths, unstage_then_remove_paths, remove_paths = _classify_paths_for_head_restore(project_dir, normalized_paths)
    if unstage_then_remove_paths:
        remove_cached_result = git(["rm", "--cached", "-f", "--", *unstage_then_remove_paths], cwd=project_dir)
        if remove_cached_result.returncode != 0:
            raise RuntimeError(remove_cached_result.stdout.strip() or "git rm --cached failed while restoring out-of-scope paths")
    if restore_paths:
        restore_result = git(["restore", "--staged", "--worktree", "--source=HEAD", "--", *restore_paths], cwd=project_dir)
        if restore_result.returncode != 0:
            raise RuntimeError(restore_result.stdout.strip() or "git restore failed while restoring out-of-scope paths")
    for raw_path in remove_paths:
        _remove_path_if_exists(project_dir / raw_path)
    return normalized_paths


def _filter_git_pathspecs_for_snapshot(project_dir: Path, paths: list[str]) -> list[str]:
    normalized_paths = [_normalize_scope_path(path) for path in paths if _normalize_scope_path(path)]
    if not normalized_paths:
        return []
    head_paths = _git_paths_in_head(project_dir, normalized_paths)
    index_paths = _git_paths_in_index(project_dir, normalized_paths)
    status_by_path = {path: status for status, path in git_status_entries(project_dir)}
    keep: list[str] = []
    remove_cached: list[str] = []
    restore_framework_deletions: list[str] = []
    for path in normalized_paths:
        file_path = project_dir / path
        status = status_by_path.get(path, "  ")
        if status[:1] == "D" and not file_path.exists() and not file_path.is_symlink():
            if path in head_paths and _is_always_allowed_framework_path(path):
                restore_framework_deletions.append(path)
            continue
        if file_path.exists() or file_path.is_symlink() or path in head_paths:
            keep.append(path)
            continue
        if path in index_paths:
            remove_cached.append(path)
    if restore_framework_deletions:
        restore_result = git(["restore", "--staged", "--worktree", "--source=HEAD", "--", *restore_framework_deletions], cwd=project_dir)
        if restore_result.returncode != 0:
            raise RuntimeError(restore_result.stdout.strip() or "git restore failed while pruning stale framework deletion paths")
    if remove_cached:
        remove_cached_result = git(["rm", "--cached", "-f", "--", *remove_cached], cwd=project_dir)
        if remove_cached_result.returncode != 0:
            raise RuntimeError(remove_cached_result.stdout.strip() or "git rm --cached failed while pruning stale snapshot paths")
    return keep


def park_task_exception_changes(project_root: Path | str, task: Task) -> str | None:
    project_dir = Path(project_root).expanduser().resolve()
    scoped_paths = task_scoped_changed_paths(project_dir, task)
    scoped_paths = _filter_git_pathspecs_for_snapshot(project_dir, scoped_paths)
    patch_path = task_exception_patch_path(project_dir, task.id)
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    if not scoped_paths:
        if patch_path.exists():
            patch_path.unlink()
        return None

    add_result = git(["add", "-N", "--", *scoped_paths], cwd=project_dir)
    if add_result.returncode != 0:
        raise RuntimeError(add_result.stdout.strip() or "git add -N failed while parking exception changes")

    diff_result = git(["diff", "--binary", "HEAD", "--", *scoped_paths], cwd=project_dir)
    if diff_result.returncode != 0:
        raise RuntimeError(diff_result.stdout.strip() or "git diff failed while parking exception changes")
    patch_text = diff_result.stdout
    if patch_text.strip():
        patch_path.write_text(patch_text, encoding="utf-8")
    elif patch_path.exists():
        patch_path.unlink()

    restore_paths, unstage_then_remove_paths, remove_paths = _classify_paths_for_head_restore(project_dir, scoped_paths)
    if unstage_then_remove_paths:
        remove_cached_result = git(["rm", "--cached", "-f", "--", *unstage_then_remove_paths], cwd=project_dir)
        if remove_cached_result.returncode != 0:
            raise RuntimeError(remove_cached_result.stdout.strip() or "git rm --cached failed while parking exception changes")
    if restore_paths:
        restore_result = git(["restore", "--staged", "--worktree", "--source=HEAD", "--", *restore_paths], cwd=project_dir)
        if restore_result.returncode != 0:
            raise RuntimeError(restore_result.stdout.strip() or "git restore failed while parking exception changes")
    for raw_path in remove_paths:
        _remove_path_if_exists(project_dir / raw_path)

    return str(patch_path.relative_to(project_dir)) if patch_path.exists() else None


def reapply_task_exception_patch(project_root: Path | str, task_id: str) -> str | None:
    result = try_reapply_task_exception_patch(project_root, task_id)
    status = str(result.get("status") or "").strip()
    if status == "conflict":
        raise RuntimeError(str(result.get("git_output") or "exception patch conflict"))
    if status in {"applied", "already_applied"}:
        return str(result.get("patch_path") or "") or None
    return None


def try_reapply_task_exception_patch(project_root: Path | str, task_id: str) -> dict[str, Any]:
    project_dir = Path(project_root).expanduser().resolve()
    patch_path = task_exception_patch_path(project_dir, task_id)
    patch_relative_path = str(patch_path.relative_to(project_dir))
    if not patch_path.exists():
        clear_task_exception_patch_conflict(project_dir, task_id)
        return {"status": "missing", "task_id": task_id, "patch_path": patch_relative_path}
    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        patch_text = ""
    affected_paths = _patch_changed_paths(patch_text)
    check_result = git(["apply", "--check", "--whitespace=nowarn", str(patch_path)], cwd=project_dir)
    if check_result.returncode == 0:
        apply_result = git(["apply", "--whitespace=nowarn", str(patch_path)], cwd=project_dir)
        if apply_result.returncode != 0:
            git_output = _truncate_exception_patch_output(apply_result.stdout.strip() or f"git apply failed for {patch_path}")
            conflict_brief_path = _write_exception_patch_conflict_brief(
                project_dir,
                task_id,
                patch_path=patch_path,
                affected_paths=affected_paths,
                git_output=git_output,
            )
            return {
                "status": "conflict",
                "task_id": task_id,
                "patch_path": patch_relative_path,
                "conflict_brief_path": conflict_brief_path,
                "affected_paths": affected_paths,
                "git_output": git_output,
            }
        patch_path.unlink()
        clear_task_exception_patch_conflict(project_dir, task_id)
        return {"status": "applied", "task_id": task_id, "patch_path": patch_relative_path, "affected_paths": affected_paths}

    reverse_check = git(["apply", "--reverse", "--check", "--whitespace=nowarn", str(patch_path)], cwd=project_dir)
    if reverse_check.returncode == 0:
        patch_path.unlink()
        clear_task_exception_patch_conflict(project_dir, task_id)
        return {"status": "already_applied", "task_id": task_id, "patch_path": patch_relative_path, "affected_paths": affected_paths}

    git_output = _truncate_exception_patch_output(check_result.stdout.strip() or f"git apply --check failed for {patch_path}")
    conflict_brief_path = _write_exception_patch_conflict_brief(
        project_dir,
        task_id,
        patch_path=patch_path,
        affected_paths=affected_paths,
        git_output=git_output,
    )
    return {
        "status": "conflict",
        "task_id": task_id,
        "patch_path": patch_relative_path,
        "conflict_brief_path": conflict_brief_path,
        "affected_paths": affected_paths,
        "git_output": git_output,
    }


def git_stage_task_snapshot(
    project_root: Path | str,
    task: Task,
    *,
    extra_paths: list[str] | None = None,
    preserved_paths: list[str] | None = None,
) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    report = task_scope_delta(project_dir, task, extra_paths=extra_paths, preserved_paths=preserved_paths)
    out_of_scope = report["out_of_scope"]
    if out_of_scope:
        raise RuntimeError(f"task produced out-of-scope changes: {', '.join(out_of_scope)}")
    staged_paths = report["staged_paths"]
    staged_paths = _filter_git_pathspecs_for_snapshot(project_dir, staged_paths)
    if staged_paths:
        add_result = git(["add", "--", *staged_paths], cwd=project_dir)
        if add_result.returncode != 0:
            raise RuntimeError(add_result.stdout.strip() or "git add failed")
    return staged_paths


def scaffold_commit_paths(project_root: Path | str) -> list[str]:
    runtime = load_project_runtime(project_root) or "claude"
    paths = list(SCAFFOLD_OUTPUT_PATHS)
    if runtime == "claude":
        paths.append(".claude/")
    if runtime == "opencode":
        paths.append(".opencode/")
    return paths


def git_commit_task(project_root: Path | str, task: Task, message: str, *, extra_paths: list[str] | None = None) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    git_stage_task_snapshot(project_root, task, extra_paths=extra_paths)
    diff = git(["diff", "--cached", "--quiet"], cwd=project_dir)
    if diff.returncode == 0:
        head_sha = git_head_sha(project_dir)
        if not head_sha:
            raise RuntimeError("git repository has no baseline commit")
        subject = git_commit_subject(project_dir, head_sha)
        if task.id != "T000" and task.id not in subject:
            raise RuntimeError(
                f"no staged changes for {task.id}; refusing to assign existing HEAD commit {head_sha} ({subject}) to this task"
            )
        return head_sha
    commit_result = git(["commit", "-m", message], cwd=project_dir)
    if commit_result.returncode != 0:
        raise RuntimeError(commit_result.stdout.strip() or "git commit failed")
    return git_head_sha(project_dir)


def git_commit_explicit_paths(project_root: Path | str, paths: list[str], message: str) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    normalized_paths = [str(path).strip() for path in paths if str(path).strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for path in normalized_paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    deduped = _filter_git_pathspecs_for_snapshot(project_dir, deduped)
    if deduped:
        add_result = git(["add", "--", *deduped], cwd=project_dir)
        if add_result.returncode != 0:
            raise RuntimeError(add_result.stdout.strip() or "git add failed")
    diff = git(["diff", "--cached", "--quiet"], cwd=project_dir)
    if diff.returncode == 0:
        head_sha = git_head_sha(project_dir)
        if not head_sha:
            raise RuntimeError("git repository has no baseline commit")
        return head_sha
    commit_result = git(["commit", "-m", message], cwd=project_dir)
    if commit_result.returncode != 0:
        raise RuntimeError(commit_result.stdout.strip() or "git commit failed")
    return git_head_sha(project_dir)