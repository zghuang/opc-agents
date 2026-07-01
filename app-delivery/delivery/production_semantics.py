from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SemanticFinding:
    category: str
    message: str
    path: str
    severity: str = "blocking"
    repair_hint: str = ""


SEMANTIC_REPORT_PATH = "docs/reviews/production-semantic-scan.md"
PRODUCTION_DEMO_PATTERN = re.compile(r"\b(DEMO|MOCK_[A-Z0-9_]*|hardcoded|stub|placeholder|NotImplemented)\b", re.IGNORECASE)
FASTAPI_ROUTE_PATTERN = re.compile(r"@router\.(get|post|put|patch|delete)\(")
AUTH_DEPENDENCY_PATTERN = re.compile(r"Depends\((?:require_user|require_role|get_current_user)")


def _relative(project_dir: Path, path: Path) -> str:
    return str(path.relative_to(project_dir)).replace("\\", "/")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _iter_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for pattern in patterns:
        result.extend(path for path in root.rglob(pattern) if path.is_file())
    return sorted(set(result))


def _frontend_has_api_surface(project_dir: Path) -> bool:
    services_dir = project_dir / "frontend" / "src" / "services"
    if not services_dir.exists():
        return False
    return any("/api" in _read(path) or "apiClient" in _read(path) for path in _iter_files(services_dir, ("*.ts", "*.tsx")))


def _backend_has_api_surface(project_dir: Path) -> bool:
    return (project_dir / "backend" / "src").exists() and bool(_iter_files(project_dir / "backend" / "src", ("*.py",)))


def _scan_mocked_only_e2e(project_dir: Path) -> list[SemanticFinding]:
    e2e_dir = project_dir / "frontend" / "e2e"
    specs = _iter_files(e2e_dir, ("*.ts", "*.tsx", "*.js", "*.jsx"))
    if not specs or not (_frontend_has_api_surface(project_dir) and _backend_has_api_surface(project_dir)):
        return []
    api_specs = [path for path in specs if "api" in _read(path).casefold() or "/api/" in _read(path).casefold() or "page.route" in _read(path)]
    real_backend_specs = [path for path in api_specs if "page.route" not in _read(path) and ("/api/" in _read(path).casefold() or "api" in _read(path).casefold())]
    if real_backend_specs:
        return []
    return [
        SemanticFinding(
            category="real-backend-e2e",
            message="Frontend/backend project has API-dependent browser tests but no real-backend E2E spec; API specs are mocked or smoke-only.",
            path="frontend/e2e",
            repair_hint="Add a Playwright project/spec that starts the backend, avoids page.route for core API calls, and proves at least one real user flow.",
        )
    ]


def _scan_production_demo_markers(project_dir: Path) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    roots = [project_dir / "backend" / "src" / "api", project_dir / "backend" / "src" / "agents", project_dir / "backend" / "src" / "actions"]
    for root in roots:
        for path in _iter_files(root, ("*.py",)):
            rel = _relative(project_dir, path)
            text = _read(path)
            if PRODUCTION_DEMO_PATTERN.search(text):
                findings.append(
                    SemanticFinding(
                        category="production-stub",
                        message="Production-path backend code contains demo/mock/stub markers.",
                        path=rel,
                        repair_hint="Replace static demo or mock data with service/database/tool-backed behavior, or move demo-only code out of the production route/agent path.",
                    )
                )
    return findings[:20]


def _scan_missing_route_auth(project_dir: Path) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    api_roots = [project_dir / "backend" / "src" / "api", project_dir / "backend" / "src" / "domains", project_dir / "backend" / "src" / "actions"]
    for root in api_roots:
        for path in _iter_files(root, ("*.py",)):
            text = _read(path)
            if not FASTAPI_ROUTE_PATTERN.search(text):
                continue
            rel = _relative(project_dir, path)
            if "/health" in text or "health" in rel:
                continue
            if AUTH_DEPENDENCY_PATTERN.search(text):
                continue
            findings.append(
                SemanticFinding(
                    category="security-rbac",
                    message="FastAPI route module exposes non-health routes without visible auth/RBAC dependency.",
                    path=rel,
                    repair_hint="Add require_user/require_role dependencies and enforce site/customer/business-line scoping in route or service queries.",
                )
            )
    return findings[:20]


def scan_production_semantics(project_root: Path | str) -> list[SemanticFinding]:
    project_dir = Path(project_root).expanduser().resolve()
    findings: list[SemanticFinding] = []
    findings.extend(_scan_mocked_only_e2e(project_dir))
    findings.extend(_scan_production_demo_markers(project_dir))
    findings.extend(_scan_missing_route_auth(project_dir))
    return findings


def write_semantic_scan_report(project_root: Path | str, findings: list[SemanticFinding]) -> str:
    project_dir = Path(project_root).expanduser().resolve()
    report_path = project_dir / SEMANTIC_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Production Semantic Scan",
        "",
        f"status: {'fail' if findings else 'pass'}",
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("- none")
    else:
        for finding in findings:
            hint = f" Repair: {finding.repair_hint}" if finding.repair_hint else ""
            lines.append(f"- [{finding.severity}] {finding.category}: {finding.path} - {finding.message}{hint}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(project_dir))
