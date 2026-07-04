from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
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
SEMANTIC_WAIVERS_PATH = "docs/reviews/semantic-waivers.json"
PRODUCTION_DEMO_PATTERN = re.compile(r"\b(DEMO|MOCK_[A-Z0-9_]*|hardcoded|stub|placeholder|NotImplemented)\b", re.IGNORECASE)
STATIC_RESPONSE_NAME_PATTERN = re.compile(r"\b(STUB|DEFAULT|DEMO|MOCK|SAMPLE)[A-Z0-9_]*\b", re.IGNORECASE)
SYNTHETIC_DATA_NAME_PATTERN = re.compile(r"(?:^|_)(?:default|demo|mock|sample|fake|synthetic|fixture)(?:_|$)", re.IGNORECASE)
SERVICE_BACKED_PATTERN = re.compile(r"\b(await|session|db|repository|repo|service|client|gateway|producer|consumer|query|execute|fetch|select|commit|rollback)\b|\b(?:store|manager|registry|provider)\.[A-Za-z_]", re.IGNORECASE)
FASTAPI_ROUTE_PATTERN = re.compile(r"@router\.(get|post|put|patch|delete)\(")
AUTH_DEPENDENCY_PATTERN = re.compile(r"Depends\((?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:require_[A-Za-z0-9_]*|get_current_user|get_authenticated_user|get_user|current_user|auth[A-Za-z0-9_]*|require_role|require_permission)")
PAGE_ROUTE_PATTERN = re.compile(r"\bpage\.route\s*\(")
ROUTE_DECORATOR_METHODS = {"get", "post", "put", "patch", "delete"}
NON_PRODUCTION_BACKEND_PARTS = {"__tests__", "fixtures", "mock", "mocks", "test", "tests"}
PRODUCTION_BACKEND_PACKAGE_NAMES = (
    "api",
    "agents",
    "actions",
    "integrations",
    "providers",
    "services",
    "service",
    "domain",
    "workflows",
    "workflow",
    "webhooks",
)


def _relative(project_dir: Path, path: Path) -> str:
    return str(path.relative_to(project_dir)).replace("\\", "/")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _finding_hash(finding: SemanticFinding) -> str:
    payload = json.dumps(
        {"category": finding.category, "path": finding.path, "message": finding.message},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_semantic_waivers(project_dir: Path) -> list[dict[str, object]]:
    path = project_dir / SEMANTIC_WAIVERS_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("waivers"), list):
        rows = payload["waivers"]
    else:
        return []
    return [row for row in rows if isinstance(row, dict)]


def _waiver_active(row: dict[str, object]) -> bool:
    expires_at = str(row.get("expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        return dt.date.fromisoformat(expires_at[:10]) >= dt.datetime.now(dt.timezone.utc).date()
    except ValueError:
        return False


def _waives_finding(row: dict[str, object], finding: SemanticFinding) -> bool:
    if not _waiver_active(row):
        return False
    if str(row.get("finding_hash") or "").strip() and str(row.get("finding_hash") or "").strip() != _finding_hash(finding):
        return False
    if str(row.get("category") or "").strip() and str(row.get("category") or "").strip() != finding.category:
        return False
    if str(row.get("path") or "").strip() and str(row.get("path") or "").strip().lstrip("./") != finding.path:
        return False
    message_contains = str(row.get("message_contains") or "").strip()
    if message_contains and message_contains not in finding.message:
        return False
    return bool(str(row.get("finding_hash") or row.get("category") or row.get("path") or "").strip())


def _apply_semantic_waivers(project_dir: Path, findings: list[SemanticFinding]) -> list[SemanticFinding]:
    waivers = _load_semantic_waivers(project_dir)
    if not waivers:
        return findings
    return [finding for finding in findings if not any(_waives_finding(row, finding) for row in waivers)]


def _line_number(text: str, offset: int) -> int:
    return str(text or "")[:max(0, offset)].count("\n") + 1


def _page_route_call_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for match in PAGE_ROUTE_PATTERN.finditer(str(text or "")):
        start = match.start()
        open_paren = text.find("(", match.start())
        if open_paren < 0:
            continue
        depth = 0
        end = len(text)
        for index in range(open_paren, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        blocks.append((_line_number(text, start), text[start:end]))
    return blocks


def mock_only_browser_e2e_issues(project_root: Path | str, output_tests: list[str]) -> list[str]:
    project_dir = Path(project_root).expanduser().resolve()
    issues: list[str] = []
    for spec in output_tests:
        normalized = str(spec or "").strip().lstrip("./")
        if not normalized.startswith("frontend/e2e/"):
            continue
        text = _read(project_dir / normalized)
        for line_number, block in _page_route_call_blocks(text):
            lowered = block.casefold()
            if "/api" not in lowered or "route.fulfill" not in lowered:
                continue
            if any(marker in lowered for marker in ("route.fetch", "route.continue", "route.fallback")):
                continue
            issues.append(
                f"{normalized}:{line_number} page.route for project-owned API calls uses route.fulfill without route.fetch/route.continue/route.fallback passthrough; mocked browser proof is not real backend E2E evidence"
            )
    return issues


def _iter_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for pattern in patterns:
        result.extend(path for path in root.rglob(pattern) if path.is_file())
    return sorted(set(result))


def _backend_package_roots(project_dir: Path, names: tuple[str, ...]) -> list[Path]:
    backend_dir = project_dir / "backend"
    roots: list[Path] = []
    for name in names:
        roots.append(backend_dir / "src" / name)
        if backend_dir.exists():
            roots.extend(backend_dir.glob(f"*/{name}"))
    return sorted({root for root in roots if root.exists() and not _is_non_production_backend_path(project_dir, root)})


def _is_non_production_backend_path(project_dir: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(project_dir / "backend")
    except ValueError:
        return False
    return any(part.casefold() in NON_PRODUCTION_BACKEND_PARTS for part in relative.parts)


def _backend_route_roots(project_dir: Path) -> list[Path]:
    return _backend_package_roots(project_dir, PRODUCTION_BACKEND_PACKAGE_NAMES)


def _frontend_has_api_surface(project_dir: Path) -> bool:
    services_dir = project_dir / "frontend" / "src" / "services"
    if not services_dir.exists():
        return False
    return any("/api" in _read(path) or "apiClient" in _read(path) for path in _iter_files(services_dir, ("*.ts", "*.tsx")))


def _backend_has_api_surface(project_dir: Path) -> bool:
    return any(
        FASTAPI_ROUTE_PATTERN.search(_read(path))
        for root in _backend_route_roots(project_dir)
        for path in _iter_files(root, ("*.py",))
    )


def _scan_mocked_only_e2e(project_dir: Path) -> list[SemanticFinding]:
    e2e_dir = project_dir / "frontend" / "e2e"
    specs = _iter_files(e2e_dir, ("*.ts", "*.tsx", "*.js", "*.jsx"))
    if not specs or not (_frontend_has_api_surface(project_dir) and _backend_has_api_surface(project_dir)):
        return []
    task_level_issues = mock_only_browser_e2e_issues(project_dir, [str(path.relative_to(project_dir)).replace("\\", "/") for path in specs])
    if task_level_issues:
        return [
            SemanticFinding(
                category="real-backend-e2e",
                message=task_level_issues[0],
                path="frontend/e2e",
                repair_hint="Use the framework-provided E2E_BACKEND_CMD/VITE_API_PROXY_TARGET path and avoid page.route fulfillment for the core application API flow.",
            )
        ]
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
    roots = _backend_package_roots(project_dir, PRODUCTION_BACKEND_PACKAGE_NAMES)
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


def _static_data_literal(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return True
    if isinstance(node, ast.Call):
        return any(_static_data_literal(arg) for arg in node.args) or any(
            _static_data_literal(keyword.value) for keyword in node.keywords
        )
    return False


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    result: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            result.append(target.id)
        elif isinstance(target, ast.Tuple):
            result.extend(element.id for element in target.elts if isinstance(element, ast.Name))
    return result


def _scan_synthetic_data_helpers(project_dir: Path) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    roots = _backend_package_roots(project_dir, PRODUCTION_BACKEND_PACKAGE_NAMES)
    for root in roots:
        for path in _iter_files(root, ("*.py",)):
            text = _read(path)
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            rel = _relative(project_dir, path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and SYNTHETIC_DATA_NAME_PATTERN.search(node.name):
                    if any(_static_data_literal(return_node.value) for return_node in ast.walk(node) if isinstance(return_node, ast.Return) and return_node.value is not None):
                        findings.append(
                            SemanticFinding(
                                category="production-fake-data",
                                message=f"Production backend code defines `{node.name}` as a default/demo/mock/sample data helper returning static data.",
                                path=rel,
                                repair_hint="Move fake/default data into explicit test, seed, or mock-server fixtures; production adapters should use provider-backed retrieval or fail loudly when data is unavailable.",
                            )
                        )
                        break
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and _static_data_literal(node.value):
                    names = [name for name in _assigned_names(node) if SYNTHETIC_DATA_NAME_PATTERN.search(name)]
                    if names:
                        findings.append(
                            SemanticFinding(
                                category="production-fake-data",
                                message="Production backend code assigns static data to default/demo/mock/sample fixture-like names: " + ", ".join(names[:4]),
                                path=rel,
                                repair_hint="Move fake/default data into explicit test, seed, or mock-server fixtures; production code should read real configured sources or report an unavailable integration.",
                            )
                        )
                        break
    return findings[:20]


def _route_path(node: ast.AsyncFunctionDef | ast.FunctionDef) -> str | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if decorator.func.attr not in ROUTE_DECORATOR_METHODS:
            continue
        if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
            return decorator.args[0].value
        return ""
    return None


def _returns_static_response(value: ast.AST) -> bool:
    if isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.ListComp, ast.DictComp)):
        return True
    if isinstance(value, ast.Name) and STATIC_RESPONSE_NAME_PATTERN.search(value.id):
        return True
    return False


def _scan_static_route_responses(project_dir: Path) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    for root in _backend_route_roots(project_dir):
        for path in _iter_files(root, ("*.py",)):
            text = _read(path)
            if not FASTAPI_ROUTE_PATTERN.search(text):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            rel = _relative(project_dir, path)
            if rel.endswith("/auth.py"):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                route_path = _route_path(node)
                if route_path is None or "health" in route_path.casefold() or "health" in rel.casefold():
                    continue
                body_text = ast.get_source_segment(text, node) or ""
                if SERVICE_BACKED_PATTERN.search(body_text):
                    continue
                if any(
                    _returns_static_response(return_node.value)
                    for return_node in ast.walk(node)
                    if isinstance(return_node, ast.Return) and return_node.value is not None
                ):
                    findings.append(
                        SemanticFinding(
                            category="production-stub",
                            message=f"Production route `{node.name}` returns static literal/default data without visible service, database, tool, or external-system backing.",
                            path=rel,
                            repair_hint="Replace production-path static responses with service/database/tool-backed behavior, move fake data to explicit mock/dev fixtures, or document the route as a blocking production gap.",
                        )
                    )
    return findings[:20]


def _auth_dependency_in_ast(node: ast.AST, text: str) -> bool:
    return AUTH_DEPENDENCY_PATTERN.search(ast.get_source_segment(text, node) or "") is not None


def _call_has_auth_dependency(call: ast.Call, text: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "dependencies" and _auth_dependency_in_ast(keyword.value, text):
            return True
    return False


def _module_name_for_path(project_dir: Path, path: Path) -> str | None:
    try:
        relative = path.relative_to(project_dir / "backend")
    except ValueError:
        return None
    if relative.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts or parts[-1] == "__init__":
        return None
    return ".".join(parts)


def _route_module_map(project_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for root in _backend_route_roots(project_dir):
        for path in _iter_files(root, ("*.py",)):
            module_name = _module_name_for_path(project_dir, path)
            if module_name:
                result[module_name] = _relative(project_dir, path)
    return result


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = str(alias.name or "").strip()
                if module_name:
                    aliases[str(alias.asname or module_name.split(".")[-1])] = module_name
        elif isinstance(node, ast.ImportFrom):
            base = str(node.module or "").strip()
            if not base:
                continue
            for alias in node.names:
                imported = str(alias.name or "").strip()
                if not imported:
                    continue
                local = str(alias.asname or imported)
                aliases[local] = base if imported == "router" else f"{base}.{imported}"
    return aliases


def _router_expr_module(expr: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Attribute) and expr.attr == "router" and isinstance(expr.value, ast.Name):
        return aliases.get(expr.value.id, expr.value.id)
    if isinstance(expr, ast.Name):
        imported = aliases.get(expr.id)
        if imported and imported.endswith(".router"):
            return imported.rsplit(".", 1)[0]
    return None


def _app_level_secured_route_modules(project_dir: Path) -> set[str]:
    module_to_rel = _route_module_map(project_dir)
    secured: set[str] = set()
    backend_dir = project_dir / "backend"
    for path in _iter_files(backend_dir, ("*.py",)):
        if _is_non_production_backend_path(project_dir, path):
            continue
        text = _read(path)
        if "include_router" not in text or "Depends" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "include_router":
                continue
            if not node.args or not _call_has_auth_dependency(node, text):
                continue
            module_name = _router_expr_module(node.args[0], aliases)
            if module_name and module_name in module_to_rel:
                secured.add(module_to_rel[module_name])
    return secured


def _app_has_global_auth_dependency(project_dir: Path) -> bool:
    backend_dir = project_dir / "backend"
    for path in _iter_files(backend_dir, ("*.py",)):
        if _is_non_production_backend_path(project_dir, path):
            continue
        text = _read(path)
        if "FastAPI" not in text or "Depends" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "FastAPI" and _call_has_auth_dependency(node, text):
                return True
    return False


def _scan_missing_route_auth(project_dir: Path) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    api_roots = _backend_package_roots(project_dir, ("api", "domains", "actions"))
    if _app_has_global_auth_dependency(project_dir):
        return []
    app_secured_routes = _app_level_secured_route_modules(project_dir)
    for root in api_roots:
        for path in _iter_files(root, ("*.py",)):
            text = _read(path)
            if not FASTAPI_ROUTE_PATTERN.search(text):
                continue
            rel = _relative(project_dir, path)
            if "/health" in text or "health" in rel or rel.endswith("/auth.py"):
                continue
            if rel in app_secured_routes:
                continue
            if AUTH_DEPENDENCY_PATTERN.search(text):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "APIRouter" and _call_has_auth_dependency(node, text) for node in ast.walk(tree)):
                continue
            findings.append(
                SemanticFinding(
                    category="security-access-control",
                    message="FastAPI route module exposes non-health routes without visible authentication or access-control dependency.",
                    path=rel,
                    repair_hint="Add the project's declared authentication/access-control dependency and enforce the relevant resource, ownership, tenant, role, policy, or data-scope checks in route or service queries.",
                )
            )
    return findings[:20]


def scan_production_semantics(project_root: Path | str) -> list[SemanticFinding]:
    project_dir = Path(project_root).expanduser().resolve()
    findings: list[SemanticFinding] = []
    findings.extend(_scan_mocked_only_e2e(project_dir))
    findings.extend(_scan_production_demo_markers(project_dir))
    findings.extend(_scan_synthetic_data_helpers(project_dir))
    findings.extend(_scan_static_route_responses(project_dir))
    findings.extend(_scan_missing_route_auth(project_dir))
    return _apply_semantic_waivers(project_dir, findings)


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
    waivers = [row for row in _load_semantic_waivers(project_dir) if _waiver_active(row)]
    if waivers:
        lines.extend(["", "## Active Waivers", ""])
        for row in waivers:
            category = str(row.get("category") or "-").strip() or "-"
            path = str(row.get("path") or "-").strip() or "-"
            reason = str(row.get("reason") or "-").strip() or "-"
            expires_at = str(row.get("expires_at") or "none").strip() or "none"
            lines.append(f"- {category}: {path} - {reason} (expires_at: {expires_at})")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path.relative_to(project_dir))
