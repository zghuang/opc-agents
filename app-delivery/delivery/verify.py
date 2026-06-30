from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import (
    active_task_record_path,
    append_task_log_event,
    load_test_results,
    load_work_items,
    remove_active_task_record,
    save_test_results,
    utc_now_iso,
    write_active_task_record,
)
from .test_env import ensure_task_test_environment, frontend_e2e_env_prefix
from .task import FINAL_VERIFY_TASK_ID, SHARED_FOUNDATION_TASK_ID, SCAFFOLD_TASK_ID, all_tasks


WEAK_COMMAND_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:^|&&)\s*(?:python3?|uv\s+run\s+python(?:3)?)\s+-c\s+['\"]\s*import\s+", "import smoke only"),
    (r"(?:^|&&)\s*(?:python3?|uv\s+run\s+python(?:3)?)\s+-c\s+['\"].*\bfrom\s+[A-Za-z0-9_\.]+\s+import\s+", "import smoke only"),
    (r"^\s*test\s+-d\s+", "directory-exists smoke only"),
    (r"^\s*\[\s+-d\s+", "directory-exists smoke only"),
    (r"^\s*echo\b", "echo-only validation"),
    (r"^\s*true\s*$", "no-op validation"),
    (r"^\s*ls\b", "listing-only validation"),
)
SUBSTANTIVE_COMMAND_MARKERS = (
    "pytest",
    "vitest",
    "playwright",
    "cypress",
    "ruff",
    "mypy",
    "tsc",
    "npm run test",
    "npm test",
)
VALIDATION_STRIP_ENV_VARS = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__")
TEST_FILE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
COMMAND_PREFIXES = (
    "pytest ",
    "python -m pytest",
    "python3 -m pytest",
    "pnpm ",
    "npm ",
    "npx ",
    "curl ",
    "bash ",
    "sh ",
    "docker ",
    "docker-compose ",
    "docker compose ",
    "alembic ",
    "uv ",
    "node ",
    "tsx ",
    "playwright ",
    "vitest ",
    "ruff ",
    "mypy ",
    "tsc ",
)
SHELL_TOKENS = ("&&", "||", "|", ";", "$(", "`")
TRIVIAL_JS_EXPECT_PATTERN = re.compile(r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)", re.IGNORECASE)
JS_EXPECT_PATTERN = re.compile(r"expect\(", re.IGNORECASE)
TRIVIAL_PY_ASSERT_PATTERN = re.compile(r"assert\s+True\b", re.IGNORECASE)
PY_ASSERT_PATTERN = re.compile(r"assert\b", re.IGNORECASE)
TEST_ARTIFACT_PATTERN = re.compile(r"[A-Za-z0-9_./@+-]+(?:test|spec)\.(?:tsx|ts|jsx|js|py)")
PLACEHOLDER_TEST_MARKERS = (
    "placeholder test not implemented yet",
    "test.skip(\"placeholder",
    "test.skip('placeholder",
    "describe.skip(\"placeholder",
    "describe.skip('placeholder",
    "it.skip(\"placeholder",
    "it.skip('placeholder",
)


@dataclass
class TestFailure:
    test: str
    message: str
    traceback: str
    failure_kind: str | None = None


@dataclass
class TestResult:
    task_id: str
    timestamp: str
    test_files: list[str]
    test_types: list[str]
    requirement_ids: list[str]
    passed: bool
    passed_count: int
    failed_count: int
    failures: list[TestFailure]
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "test_files": self.test_files,
            "test_types": self.test_types,
            "requirement_ids": self.requirement_ids,
            "passed": self.passed,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "failures": [failure.__dict__ for failure in self.failures],
            "attempt": self.attempt,
        }


def _test_report_path(project_root: Path | str, task_id: str) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    reviews_dir = project_dir / "docs" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    safe_task_id = str(task_id or "task").strip().replace(":", "-") or "task"
    return reviews_dir / f"test-report-{safe_task_id}.md"


def write_test_report(project_root: Path | str, result: TestResult) -> str:
    report_path = _test_report_path(project_root, result.task_id)
    lines = [
        f"status: {'pass' if result.passed else 'fail'}",
        "report_type: test",
        f"work_item: {result.task_id}",
        f"generated_at: {result.timestamp}",
        f"attempt: {result.attempt}",
        f"test_types: {', '.join(result.test_types) or 'none'}",
        f"requirements: {', '.join(result.requirement_ids) or 'none'}",
        "",
        "# Test Report",
        "",
        "## Summary",
        "",
        f"- Passed: {result.passed}",
        f"- Passed count: {result.passed_count}",
        f"- Failed count: {result.failed_count}",
        "",
        "## Test Specs",
        "",
    ]
    if result.test_files:
        for spec in result.test_files:
            lines.append(f"- {spec}")
    else:
        lines.append("- none")
    lines.extend(["", "## Failures", ""])
    if result.failures:
        for failure in result.failures:
            lines.append(f"- {failure.test}: {failure.message}")
    else:
        lines.append("- none")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(Path(project_root).expanduser().resolve()))


def _final_repair_report_path(project_root: Path | str) -> Path:
    project_dir = Path(project_root).expanduser().resolve()
    reviews_dir = project_dir / "docs" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    return reviews_dir / "final-repair-report.md"


def infer_final_repair_candidates(
    project_root: Path | str,
    results: list[TestResult],
    missing_test_types: list[tuple[str, str]],
) -> list[str]:
    tasks = all_tasks(project_root)
    by_id = {task.id: task for task in tasks}
    candidates: list[str] = []

    def add_candidate(task_id: str) -> None:
        if not task_id or task_id in candidates:
            return
        task = by_id.get(task_id)
        if task is None:
            return
        if task.id in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}:
            return
        if task.status != "verified":
            return
        candidates.append(task_id)

    for result in results:
        if result.passed:
            continue
        add_candidate(result.task_id)

    verified_tasks = [
        task
        for task in tasks
        if task.status == "verified" and task.id not in {SCAFFOLD_TASK_ID, SHARED_FOUNDATION_TASK_ID, FINAL_VERIFY_TASK_ID}
    ]

    for requirement_id, _test_type in missing_test_types:
        direct_matches = [task for task in verified_tasks if requirement_id in task.requirements]
        for task in direct_matches:
            add_candidate(task.id)
    return candidates


def write_final_repair_report(
    project_root: Path | str,
    *,
    results: list[TestResult],
    requirement_coverage: dict[str, Any],
    missing_test_types: list[tuple[str, str]],
    repair_candidates: list[str],
) -> str:
    report_path = _final_repair_report_path(project_root)
    lines = [
        "status: fail",
        "report_type: final_repair",
        f"generated_at: {utc_now_iso()}",
        f"repair_candidates: {', '.join(repair_candidates) or 'none'}",
        "",
        "# Final Repair Report",
        "",
        "## Full Suite Summary",
        "",
    ]
    summary = test_results_to_summary(results)
    lines.append(summary or "- none")
    lines.extend(
        [
            "",
            "## Requirement Coverage",
            "",
            f"- total: {requirement_coverage.get('total', 0)}",
            f"- covered: {requirement_coverage.get('covered', 0)}",
            f"- uncovered: {', '.join(requirement_coverage.get('uncovered', [])) or 'none'}",
            "",
            "## Missing Test Types",
            "",
        ]
    )
    if missing_test_types:
        for requirement_id, test_type in missing_test_types:
            lines.append(f"- {requirement_id}: missing {test_type}")
    else:
        lines.append("- none")
    lines.extend(["", "## Suggested Repair Targets", ""])
    if repair_candidates:
        for task_id in repair_candidates:
            lines.append(f"- {task_id}")
    else:
        lines.append("- none")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(Path(project_root).expanduser().resolve()))


def weak_command_reason(command: str) -> str:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return "empty validation command"
    lowered = normalized.lower()
    if any(marker in lowered for marker in SUBSTANTIVE_COMMAND_MARKERS):
        return ""
    for pattern, reason in WEAK_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return ""


def validation_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in VALIDATION_STRIP_ENV_VARS:
        env.pop(key, None)
    return env


def is_command_test_spec(spec: str) -> bool:
    normalized = str(spec or "").strip()
    lowered = normalized.casefold()
    if not normalized:
        return False
    if any(token in normalized for token in SHELL_TOKENS):
        return True
    return any(lowered.startswith(prefix) for prefix in COMMAND_PREFIXES)


def is_path_test_spec(spec: str) -> bool:
    normalized = str(spec or "").strip()
    if not normalized or is_command_test_spec(normalized):
        return False
    return not Path(normalized).is_absolute()


def is_placeholder_test_file(spec: str) -> bool:
    if not is_path_test_spec(spec):
        return False
    path = Path(str(spec).strip())
    return path.suffix.lower() in TEST_FILE_SUFFIXES


def weak_test_file_reason(project_root: Path | str, spec: str) -> str:
    if not is_placeholder_test_file(spec):
        return ""
    path = Path(project_root).expanduser().resolve() / str(spec).strip()
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    lowered_content = content.casefold()
    if any(marker in lowered_content for marker in PLACEHOLDER_TEST_MARKERS):
        return "placeholder test file: replace generated placeholder with executable behavior checks"
    significant_lines: list[str] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith(("import ", "from ", "//", "/*", "*", "#")):
            continue
        significant_lines.append(stripped)
    normalized = "\n".join(significant_lines)
    suffix = path.suffix.lower()
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        expect_count = len(JS_EXPECT_PATTERN.findall(normalized))
        trivial_expect_count = len(TRIVIAL_JS_EXPECT_PATTERN.findall(normalized))
        if expect_count > 0 and expect_count == trivial_expect_count:
            return "placeholder test file: replace trivial expect(true).toBe(true) assertions with behavior checks"
    if suffix == ".py":
        assert_count = len(PY_ASSERT_PATTERN.findall(normalized))
        trivial_assert_count = len(TRIVIAL_PY_ASSERT_PATTERN.findall(normalized))
        if assert_count > 0 and assert_count == trivial_assert_count:
            return "placeholder test file: replace assert True with meaningful assertions"
    return ""


def infer_test_types(test_files: list[str]) -> list[str]:
    kinds: list[str] = []
    for path in test_files:
        lowered = path.lower()

        if lowered.startswith("backend/src/tests/test_") and not lowered.startswith("backend/src/tests/test_health.py") and not lowered.startswith("backend/src/tests/test_core"):
            if "api" not in kinds:
                kinds.append("api")
        elif lowered.startswith("backend/src/tests/") or lowered.startswith("backend/tests/core/"):
            if "unit" not in kinds:
                kinds.append("unit")
        elif lowered.startswith("backend/tests/"):
            if "api" not in kinds:
                kinds.append("api")
            if "integration" not in kinds:
                kinds.append("integration")
        if lowered.startswith("frontend/e2e/") and "browser" not in kinds:
            kinds.append("browser")
        if lowered.startswith("mock-server/tests/") and "contract" in Path(lowered).name:
            if "contract" not in kinds:
                kinds.append("contract")

        mapping = (
            ("mock-server/tests/", "integration"),
            ("mock-server/tests/test_routers/", "contract"),
            ("mock-server/tests/mcp_servers/", "contract"),
            ("mock-server/tests/test_connectivity.py", "contract"),
            ("/unit/", "unit"),
            ("/integration/", "integration"),
            ("/e2e/", "e2e"),
            ("/browser/", "browser"),
            ("/performance/", "performance"),
            ("/accessibility/", "accessibility"),
            ("accessibility.test", "accessibility"),
            ("a11y.test", "accessibility"),
            ("test_accessibility", "accessibility"),
            ("test_a11y", "accessibility"),
            ("test_api_", "api"),
            ("test_ui_", "browser"),
            ("playwright", "browser"),
            ("curl ", "api"),
            ("/api/", "api"),
            ("docker compose", "integration"),
            ("docker-compose", "integration"),
            ("alembic", "integration"),
            ("tsx ", "integration"),
            ("npm run test", "unit"),
            ("pnpm run test", "unit"),
            ("yarn test", "unit"),
            ("vitest", "unit"),
            ("npm run typecheck", "typecheck"),
            ("pnpm run typecheck", "typecheck"),
            ("yarn typecheck", "typecheck"),
            ("tsc --noemit", "typecheck"),
            ("npm run lint", "lint"),
            ("pnpm run lint", "lint"),
            ("yarn lint", "lint"),
            ("eslint ", "lint"),
            ("npm run build", "build"),
            ("pnpm run build", "build"),
            ("yarn build", "build"),
            ("vite build", "build"),
            ("npm run e2e", "e2e"),
            ("pnpm run e2e", "e2e"),
            ("yarn e2e", "e2e"),
        )
        for marker, kind in mapping:
            if marker in lowered and kind not in kinds:
                kinds.append(kind)
        if any(marker in lowered for marker in ("npm run e2e", "pnpm run e2e", "yarn e2e")) and "browser" not in kinds:
            kinds.append("browser")
    return kinds or ["unit"]


def _observed_test_specs_from_output(output: str) -> list[str]:
    specs: list[str] = []
    for match in TEST_ARTIFACT_PATTERN.finditer(str(output or "")):
        spec = match.group(0).strip().lstrip("./")
        if spec and spec not in specs:
            specs.append(spec)
    return specs


def _discovered_js_test_specs_for_command(project_root: Path, spec: str) -> list[str]:
    normalized = str(spec or "").strip()
    lowered = normalized.casefold()
    if not any(marker in lowered for marker in ("npm run test", "pnpm run test", "yarn test", "vitest")):
        return []
    candidate_roots: list[Path] = []
    frontend_root = project_root / "frontend"
    if (frontend_root / "package.json").exists():
        candidate_roots.append(frontend_root)
    if (project_root / "package.json").exists():
        candidate_roots.append(project_root)
    specs: list[str] = []
    ignored_parts = {"node_modules", "dist", "build", "coverage", "playwright-report"}
    for root in candidate_roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if ignored_parts.intersection(path.relative_to(root).parts):
                continue
            if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            lowered_name = path.name.casefold()
            if ".test." not in lowered_name and ".spec." not in lowered_name:
                continue
            relative = str(path.relative_to(project_root))
            if relative not in specs:
                specs.append(relative)
    return specs


def _frontend_quality_gate_tasks(project_root: Path) -> list[dict[str, Any]]:
    frontend_dir = project_root / "frontend"
    package_json = frontend_dir / "package.json"
    if not package_json.exists():
        return []
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
    if not isinstance(scripts, dict):
        return []
    tasks: list[dict[str, Any]] = []
    gate_specs = [
        ("frontend-quality-gates", [], ["test", "typecheck", "lint", "build"]),
        ("frontend-browser-qa", [], ["e2e"]),
    ]
    for suffix, requirement_ids, script_names in gate_specs:
        commands = [f"npm run {name}" for name in script_names if str(scripts.get(name) or "").strip()]
        if not commands:
            continue
        if isinstance(requirement_ids, str):
            normalized_requirement_ids = [requirement_ids] if requirement_ids else []
        else:
            normalized_requirement_ids = [str(value).strip() for value in requirement_ids if str(value).strip()]
        tasks.append(
            {
                "id": f"{FINAL_VERIFY_TASK_ID}:{suffix}",
                "requirements": normalized_requirement_ids,
                "output_tests": commands,
            }
        )
    return tasks


def _parse_pytest_output(text: str) -> tuple[int, int, list[TestFailure]]:
    passed_count = 0
    failed_count = 0
    failures: list[TestFailure] = []
    passed_match = re.search(r"(\d+) passed", text)
    failed_match = re.search(r"(\d+) failed", text)
    if passed_match:
        passed_count = int(passed_match.group(1))
    if failed_match:
        failed_count = int(failed_match.group(1))
    current_test = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        if "::" in line and "FAILED" in line:
            if current_test:
                traceback = "\n".join(current_lines).strip()
                failures.append(TestFailure(test=current_test, message=current_lines[0] if current_lines else "failed", traceback=traceback))
                current_lines = []
            current_test = line.split(" FAILED", 1)[0].strip()
            current_lines.append(line.strip())
        elif current_test:
            current_lines.append(line)
    if current_test:
        traceback = "\n".join(current_lines).strip()
        failures.append(TestFailure(test=current_test, message=current_lines[0] if current_lines else "failed", traceback=traceback))
    return passed_count, failed_count, failures


def _run(
    command: list[str],
    *,
    cwd: Path,
    project_root: Path | None = None,
    task_id: str = "",
    task_title: str = "",
    spec: str = "",
) -> subprocess.CompletedProcess[str]:
    record_path: Path | None = None
    normalized_task_id = str(task_id or "").strip()
    normalized_task_title = str(task_title or "").strip()
    normalized_spec = str(spec or "").strip()
    if project_root is not None and normalized_task_id:
        record_path = active_task_record_path(project_root, runtime="validation", task_id=normalized_task_id)
        write_active_task_record(
            record_path,
            {
                "pid": os.getpid(),
                "runtime": "validation",
                "task_id": normalized_task_id,
                "task_title": normalized_task_title or normalized_task_id,
                "phase": "verification",
                "command": command,
                "spec": normalized_spec or None,
            },
        )
        append_task_log_event(
            project_root,
            runtime="validation",
            level="INFO",
            message=f"Started verification {normalized_task_id}",
            task_id=normalized_task_id,
            task_title=normalized_task_title,
            extra={"phase": "verification", "spec": normalized_spec or None},
        )
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=validation_subprocess_env(),
            check=False,
        )
    finally:
        remove_active_task_record(record_path)
    if project_root is not None and normalized_task_id:
        append_task_log_event(
            project_root,
            runtime="validation",
            level="INFO" if completed.returncode == 0 else "ERROR",
            message=(f"Completed verification {normalized_task_id}" if completed.returncode == 0 else f"Failed verification {normalized_task_id}"),
            task_id=normalized_task_id,
            task_title=normalized_task_title,
            extra={
                "phase": "verification",
                "spec": normalized_spec or None,
                "exit_code": completed.returncode,
            },
        )
    return completed


def detect_js_package_manager(project_root: Path, spec: str) -> str:
    roots: list[Path] = []
    spec_path = project_root / str(spec).strip()
    roots.extend(spec_path.parents)
    roots.append(project_root)
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        if (root / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (root / "yarn.lock").exists():
            return "yarn"
        if (root / "package-lock.json").exists():
            return "npm"
        package_json = root / "package.json"
        if package_json.exists():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            package_manager = str(payload.get("packageManager") or "").strip().casefold()
            if package_manager.startswith("pnpm"):
                return "pnpm"
            if package_manager.startswith("yarn"):
                return "yarn"
            if package_manager.startswith("npm"):
                return "npm"
    return "pnpm"


def _js_runner_prefix(project_root: Path, spec: str, tool: str) -> str:
    package_manager = detect_js_package_manager(project_root, spec)
    if package_manager == "yarn":
        return f"yarn {tool}"
    if package_manager == "npm":
        return f"npx {tool}"
    return f"pnpm exec {tool}"


def _rewrite_command_test_spec(project_root: Path, spec: str) -> str:
    normalized = str(spec or "").strip()
    if not normalized:
        return normalized
    lowered = normalized.casefold()
    backend_root = project_root / "backend"
    frontend_root = project_root / "frontend"
    e2e_env = frontend_e2e_env_prefix(project_root)
    if lowered.startswith("npm run "):
        if (frontend_root / "package.json").exists():
            if lowered.startswith("npm run e2e"):
                return f"cd {shlex.quote(str(frontend_root))} && {e2e_env}{normalized}"
            return f"cd {shlex.quote(str(frontend_root))} && {normalized}"
        return normalized
    if lowered.startswith(("pytest ", "python -m pytest", "python3 -m pytest", "uv run pytest", "uv run python -m pytest", "uv run python3 -m pytest")):
        if backend_root.exists():
            if lowered.startswith("uv run "):
                return f"cd {shlex.quote(str(backend_root))} && {normalized}"
            return f"cd {shlex.quote(str(backend_root))} && uv run {normalized}"
    return normalized


def _command_for_test_spec(project_root: Path, spec: str) -> list[str]:
    normalized = str(spec or "").strip()
    path = Path(normalized)
    if is_command_test_spec(normalized):
        return ["/bin/zsh", "-lc", _rewrite_command_test_spec(project_root, normalized)]
    backend_root = project_root / "backend"
    frontend_root = project_root / "frontend"
    mock_root = project_root / "mock-server"
    e2e_env = frontend_e2e_env_prefix(project_root)
    if normalized.startswith("backend/"):
        relative = Path(normalized).relative_to("backend")
        if relative.suffix.lower() in {".py"} or relative.suffix == "":
            return ["/bin/zsh", "-lc", f"cd {shlex.quote(str(backend_root))} && uv run pytest {shlex.quote(str(relative))} --tb=short -q"]
    if normalized.startswith("mock-server/"):
        relative = Path(normalized).relative_to("mock-server")
        if relative.suffix.lower() in {".py"} or relative.suffix == "":
            return ["/bin/zsh", "-lc", f"cd {shlex.quote(str(mock_root))} && uv run pytest {shlex.quote(str(relative))} --tb=short -q"]
    if normalized.startswith("frontend/"):
        relative = Path(normalized).relative_to("frontend")
        if ".spec." in relative.name or "e2e" in relative.parts or "playwright" in normalized.lower():
            return ["/bin/zsh", "-lc", f"cd {shlex.quote(str(frontend_root))} && {e2e_env}{ _js_runner_prefix(project_root, normalized, 'playwright') } test {shlex.quote(str(relative))}"]
        if relative.suffix.lower() in {".ts", ".tsx", ".js", ".jsx"}:
            return ["/bin/zsh", "-lc", f"cd {shlex.quote(str(frontend_root))} && { _js_runner_prefix(project_root, normalized, 'vitest') } run {shlex.quote(str(relative))}"]
    if path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx"}:
        if ".spec." in path.name or "e2e" in path.parts or "playwright" in normalized.lower():
            return ["/bin/zsh", "-lc", f"{e2e_env}{_js_runner_prefix(project_root, normalized, 'playwright')} test {shlex.quote(normalized)}"]
        return ["/bin/zsh", "-lc", f"{_js_runner_prefix(project_root, normalized, 'vitest')} run {shlex.quote(normalized)}"]
    pytest_runner = os.environ.get("APP_DELIVERY_PYTHON") or sys.executable or "python3"
    return [pytest_runner, "-m", "pytest", normalized, "--tb=short", "-q"]


def _looks_like_pytest_spec(spec: str, command: list[str]) -> bool:
    normalized = str(spec or "").strip().lower()
    return "pytest" in normalized or (len(command) >= 3 and command[1] == "-m" and command[2] == "pytest")


def _failure_from_output(spec: str, output: str, message: str) -> TestFailure:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    summary = message
    if lines:
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.casefold()
            if lowered.startswith(("> ", "npm notice", "npm info", "npm warn")):
                continue
            summary = stripped[:300]
            break
        else:
            summary = lines[0][:300]
    lowered_output = str(output or "").casefold()
    normalized_spec = str(spec or "").strip()
    if normalized_spec.startswith(("backend/", "frontend/", "mock-server/")) and (
        "file or directory not found" in lowered_output or "no tests found" in lowered_output
    ):
        summary = (
            f"{summary} [hint: `{normalized_spec}` is project-root-relative; if you run inside its package directory, drop the leading root prefix first]"
        )
    return TestFailure(test=spec, message=summary, traceback=str(output or "").strip()[:4000])


def _append_result(project_root: Path | str, result: TestResult) -> None:
    payload = load_test_results(project_root)
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    results.append(result.to_dict())
    payload["results"] = results
    save_test_results(project_root, payload)
    write_test_report(project_root, result)


def run_task_tests(project_root: Path | str, task: dict[str, Any], *, attempt: int = 1) -> TestResult:
    project_dir = Path(project_root).expanduser().resolve()
    test_specs = [str(value).strip() for value in task.get("output_tests", []) if str(value).strip()]
    if not test_specs:
        result = TestResult(
            task_id=str(task.get("id") or ""),
            timestamp=utc_now_iso(),
            test_files=[],
            test_types=[],
            requirement_ids=[str(value).strip() for value in task.get("requirements", []) if str(value).strip()],
            passed=True,
            passed_count=0,
            failed_count=0,
            failures=[],
            attempt=attempt,
        )
        _append_result(project_root, result)
        return result

    passed_count = 0
    failed_count = 0
    failures: list[TestFailure] = []
    overall_passed = True
    observed_test_specs = list(test_specs)
    for spec in test_specs:
        env_result = ensure_task_test_environment(project_dir, task, spec)
        if not env_result.ready:
            overall_passed = False
            failed_count += 1
            failures.append(
                TestFailure(
                    test=spec,
                    message=f"test environment not ready ({env_result.profile}): {env_result.summary}",
                    traceback=json.dumps({"actions_run": env_result.actions_run, "details": env_result.details}, ensure_ascii=False),
                    failure_kind="environment_not_ready",
                )
            )
            continue
        if is_command_test_spec(spec):
            reason = weak_command_reason(spec)
            if reason:
                overall_passed = False
                failed_count += 1
                failures.append(TestFailure(test=spec, message=reason, traceback=spec, failure_kind="validation_contract"))
                continue
        weak_file_reason = weak_test_file_reason(project_dir, spec)
        if weak_file_reason:
            overall_passed = False
            failed_count += 1
            failures.append(TestFailure(test=spec, message=weak_file_reason, traceback=spec, failure_kind="validation_contract"))
            continue
        command = _command_for_test_spec(project_dir, spec)
        completed = _run(
            command,
            cwd=project_dir,
            project_root=project_dir,
            task_id=str(task.get("id") or ""),
            task_title=str(task.get("title") or ""),
            spec=spec,
        )
        for observed_spec in _observed_test_specs_from_output(completed.stdout):
            if observed_spec not in observed_test_specs:
                observed_test_specs.append(observed_spec)
        if completed.returncode == 0:
            for discovered_spec in _discovered_js_test_specs_for_command(project_dir, spec):
                if discovered_spec not in observed_test_specs:
                    observed_test_specs.append(discovered_spec)
        if _looks_like_pytest_spec(spec, command):
            spec_passed, spec_failed, spec_failures = _parse_pytest_output(completed.stdout)
            passed_count += spec_passed
            failed_count += spec_failed
            failures.extend(spec_failures)
        if completed.returncode == 0:
            if not _looks_like_pytest_spec(spec, command) or (passed_count == 0 and failed_count == 0):
                passed_count += 1
        else:
            overall_passed = False
            if not _looks_like_pytest_spec(spec, command) or not failures:
                failures.append(_failure_from_output(spec, completed.stdout, "test command failed"))
            if not _looks_like_pytest_spec(spec, command) or (passed_count == 0 and failed_count == 0):
                failed_count += 1
    result = TestResult(
        task_id=str(task.get("id") or ""),
        timestamp=utc_now_iso(),
        test_files=observed_test_specs,
        test_types=infer_test_types(observed_test_specs),
        requirement_ids=[str(value).strip() for value in task.get("requirements", []) if str(value).strip()],
        passed=overall_passed and failed_count == 0,
        passed_count=passed_count,
        failed_count=failed_count,
        failures=failures,
        attempt=attempt,
    )
    _append_result(project_root, result)
    return result


def run_full_suite(project_root: Path | str, *, mode: str = "all") -> list[TestResult]:
    payload = load_work_items(project_root)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    results: list[TestResult] = []
    project_dir = Path(project_root).expanduser().resolve()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") == "cancelled":
            continue
        if str(item.get("task_kind") or "") == "repair" and str(item.get("status") or "") != "verified":
            continue
        if mode == "non-verified" and str(item.get("status") or "") == "verified":
            continue
        results.append(run_task_tests(project_root, item, attempt=1))
    if mode == "all":
        for synthetic_task in _frontend_quality_gate_tasks(project_dir):
            results.append(run_task_tests(project_root, synthetic_task, attempt=1))
    test_results = load_test_results(project_root)
    test_results["full_suite_results"] = {
        "last_run": utc_now_iso(),
        "passed": all(result.passed for result in results),
        "total": sum(result.passed_count + result.failed_count for result in results),
        "passed_count": sum(result.passed_count for result in results),
        "failed_count": sum(result.failed_count for result in results),
        "scores": _scores_from_results(results),
    }
    save_test_results(project_root, test_results)
    summary_result = TestResult(
        task_id=FINAL_VERIFY_TASK_ID,
        timestamp=utc_now_iso(),
        test_files=[spec for result in results for spec in result.test_files],
        test_types=sorted({test_type for result in results for test_type in result.test_types}),
        requirement_ids=sorted({req_id for result in results for req_id in result.requirement_ids}),
        passed=all(result.passed for result in results),
        passed_count=sum(result.passed_count for result in results),
        failed_count=sum(result.failed_count for result in results),
        failures=[failure for result in results for failure in result.failures],
        attempt=1,
    )
    write_test_report(project_root, summary_result)
    return results


def _scores_from_results(results: list[TestResult]) -> dict[str, str]:
    scores: dict[str, str] = {}
    for result in results:
        for test_type in result.test_types:
            existing = scores.get(test_type)
            if existing == "fail":
                continue
            scores[test_type] = "pass" if result.passed else "fail"
    return scores


def test_results_to_summary(results: list[TestResult]) -> str:
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"- {result.task_id}: {status} ({result.passed_count} passed, {result.failed_count} failed)")
        for failure in result.failures[:3]:
            lines.append(f"  - {failure.test}: {failure.message}")
    return "\n".join(lines)


def can_verify(task: dict[str, Any], test_result: TestResult) -> bool:
    return test_result.passed and (task.get("review_status") == "pass")
