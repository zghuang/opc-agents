#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: new-project.sh [--runtime claude|opencode] [--opc-home <path>] [--framework-root <path>] [--force] <project_name> [description]
  new-project.sh [--runtime claude|opencode] [--opc-home <path>] [--framework-root <path>] [--force] [--no-watchdog] <project_name> [description]

Creates a new app-delivery project under OPC_HOME/projects/<project_name>.
The scaffold includes backend, frontend, docs, and an embedded mock-server.
EOF
}

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
info() { echo -e "${CYAN}  →${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; }
die()  { fail "$*"; exit 1; }
header() { echo -e "\n${BOLD}$*${NC}"; }

RUNTIME=""
OPC_HOME="${OPC_HOME:-$HOME/opc}"
FRAMEWORK_ROOT=""
FORCE=0
WATCHDOG=1
POSITIONAL=()

resolve_runtime_from_state() {
  local state_file="${1:-}"
  [[ -n "$state_file" && -f "$state_file" ]] || return 1
  local state_runtime
  state_runtime="$(<"$state_file")"
  state_runtime="${state_runtime##* }"
  state_runtime="${state_runtime//$'\n'/}"
  state_runtime="$(printf '%s' "$state_runtime" | tr '[:upper:]' '[:lower:]')"
  case "$state_runtime" in
    claude|opencode)
      printf '%s\n' "$state_runtime"
      return 0
      ;;
  esac
  return 1
}

normalize_semver_spec() {
  local spec="${1:-}"
  spec="${spec#workspace:}"
  printf '%s' "$spec" | sed -E 's/^[^0-9]*//; s/[^0-9.].*$//'
}

read_playwright_test_spec() {
  local package_json="$1"
  [[ -f "$package_json" ]] || return 0
  command -v node >/dev/null 2>&1 || return 0
  node - "$package_json" <<'NODE'
const fs = require('fs');
const packageJsonPath = process.argv[2];

try {
  const pkg = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
  const spec = (pkg.devDependencies && pkg.devDependencies['@playwright/test']) || '';
  process.stdout.write(spec);
} catch {
  process.stdout.write('');
}
NODE
}

render_init_project_report() {
  local report_file="$1"
  "$PYTHON_BIN" - <<'PY' "$report_file"
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
payload = None
try:
  candidate = json.loads(text)
except json.JSONDecodeError:
  candidate = None
if isinstance(candidate, dict):
  payload = candidate
if payload is None:
  for raw_line in reversed(text.splitlines()):
    line = raw_line.strip()
    if not line or not line.startswith("{"):
      continue
    try:
      candidate = json.loads(line)
    except json.JSONDecodeError:
      continue
    if isinstance(candidate, dict):
      payload = candidate
      break
if payload is None:
  raise SystemExit(f"Could not parse init-project JSON from {sys.argv[1]}")
print(f"Project root:   {payload.get('project_root', '')}")
print(f"Metadata path:  {payload.get('metadata_path', '')}")
print(f"Runtime:        {payload.get('runtime', '')}")
print(f"Stack:          {payload.get('stack', '')}")
print(f"Mock server:    {payload.get('mock_server_path', '')}")
PY
}

render_init_project_error() {
  local report_file="$1"
  "$PYTHON_BIN" - <<'PY' "$report_file"
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
try:
  payload = json.loads(text)
except json.JSONDecodeError:
  print(text.strip() or "init-project failed without output")
  raise SystemExit(0)
if not isinstance(payload, dict):
  print(text.strip() or "init-project failed without output")
  raise SystemExit(0)
message = str(payload.get("message") or "init-project failed").strip()
code = str(payload.get("code") or "").strip()
suggested = str(payload.get("suggested_action") or "").strip()
if code:
  print(f"{code}: {message}")
else:
  print(message)
if suggested:
  print(f"Suggested action: {suggested}")
PY
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --runtime)
      RUNTIME="$2"
      shift 2
      ;;
    --opc-home)
      OPC_HOME="$2"
      shift 2
      ;;
    --framework-root)
      FRAMEWORK_ROOT="$2"
      shift 2
      ;;
    --force|-f)
      FORCE=1
      shift
      ;;
    --watchdog)
      WATCHDOG=1
      shift
      ;;
    --no-watchdog)
      WATCHDOG=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

PROJECT_NAME="${POSITIONAL[0]:-}"
DESCRIPTION="${POSITIONAL[*]:1}"
if [[ -z "$PROJECT_NAME" ]]; then
  usage >&2
  exit 1
fi

STATE_RUNTIME_FILE="$OPC_HOME/state/active-runtime"
if [[ -z "$RUNTIME" ]]; then
  RUNTIME="$(resolve_runtime_from_state "$STATE_RUNTIME_FILE" || true)"
fi
if [[ -z "$RUNTIME" ]]; then
  echo "No runtime provided and no active runtime found at $STATE_RUNTIME_FILE" >&2
  echo "Run setup-opc.sh first, or pass --runtime claude|opencode explicitly." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_FRAMEWORK_ROOT="${OPC_HOME}/opc-agents/app-delivery"
if [[ ! -d "$DEFAULT_FRAMEWORK_ROOT/delivery" ]]; then
  DEFAULT_FRAMEWORK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
FRAMEWORK_ROOT="${FRAMEWORK_ROOT:-$DEFAULT_FRAMEWORK_ROOT}"
PYTHON_BIN="${APP_DELIVERY_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -f "$FRAMEWORK_ROOT/.python-bin" ]]; then
  PYTHON_BIN="$(<"$FRAMEWORK_ROOT/.python-bin")"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
PROJECT_ROOT="$OPC_HOME/projects/$PROJECT_NAME"
LEGACY_WATCHDOG_SCRIPT="$HOME/.hermes/scripts/opc-watchdog-$PROJECT_NAME.sh"

mkdir -p "$OPC_HOME/projects"

if [[ -f "$LEGACY_WATCHDOG_SCRIPT" ]]; then
  warn "Found existing Hermes watchdog script for the same project name: $LEGACY_WATCHDOG_SCRIPT"
  warn "This can conflict with app-delivery runs and may report stale runtime/framework state (for example an old Claude watchdog against a new OpenCode project)."
  warn "Recommended: remove or disable the old watchdog before using this project name again."
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║       App-delivery New Project       ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""
info "Project name: $PROJECT_NAME"
info "Runtime:      $RUNTIME"
info "Location:     $PROJECT_ROOT"

header "Step 1: Scaffolding project files..."
pushd "$FRAMEWORK_ROOT" >/dev/null
ARGS=(init-project --project "$PROJECT_ROOT" --description "${DESCRIPTION:-App-delivery project}" --runtime "$RUNTIME" --framework-root "$FRAMEWORK_ROOT")
if [[ "$FORCE" == "1" ]]; then
  ARGS+=(--force)
fi
if [[ "$WATCHDOG" == "1" ]]; then
  ARGS+=(--watchdog)
fi
INIT_REPORT_FILE="$(mktemp)"
if ! "$PYTHON_BIN" -m delivery "${ARGS[@]}" > "$INIT_REPORT_FILE" 2>&1; then
  render_init_project_error "$INIT_REPORT_FILE" >&2
  rm -f "$INIT_REPORT_FILE"
  popd >/dev/null
  exit 1
fi
render_init_project_report "$INIT_REPORT_FILE"
rm -f "$INIT_REPORT_FILE"
popd >/dev/null
ok "Project scaffold created"

FRONTEND_DIR="$PROJECT_ROOT/frontend"
if [[ -f "$FRONTEND_DIR/package.json" ]]; then
  header "Step 2: Installing frontend dependencies..."
  if [[ "${APP_DELIVERY_SKIP_FRONTEND_INSTALL:-0}" == "1" ]]; then
    warn "APP_DELIVERY_SKIP_FRONTEND_INSTALL=1 -> skipping npm install and Playwright browser install"
  elif ! command -v npm >/dev/null 2>&1; then
    die "npm not found. setup-opc.sh should have validated Node/npm; fix the environment and rerun new-project.sh."
  else
    pushd "$FRONTEND_DIR" >/dev/null
    npm install --silent
    popd >/dev/null
    ok "Frontend npm dependencies installed"

    header "Step 3: Ensuring Playwright Chromium is available..."
    PROJECT_PLAYWRIGHT_SPEC="$(read_playwright_test_spec "$FRONTEND_DIR/package.json")"
    PROJECT_PLAYWRIGHT_VERSION="$(normalize_semver_spec "$PROJECT_PLAYWRIGHT_SPEC")"

    PW_INSTALL_OPTS=()
    if command -v apt-get >/dev/null 2>&1; then
      PW_INSTALL_OPTS+=(--with-deps)
    fi

    PW_INSTALL_MODE="${APP_DELIVERY_PLAYWRIGHT_INSTALL_MODE:-}"
    if [[ -z "$PW_INSTALL_MODE" ]]; then
      if [[ "$(uname -s)" == "Linux" ]]; then
        PW_INSTALL_MODE="headless-shell"
      else
        PW_INSTALL_MODE="full"
      fi
    fi

    case "$PW_INSTALL_MODE" in
      headless-shell)
        PW_INSTALL_OPTS+=(--only-shell)
        info "Installing Playwright Chromium headless shell (server-friendly default)."
        ;;
      full)
        info "Installing full Playwright Chromium browser."
        ;;
      *)
        die "Unsupported APP_DELIVERY_PLAYWRIGHT_INSTALL_MODE: $PW_INSTALL_MODE. Use full or headless-shell."
        ;;
    esac

    if [[ -n "$PROJECT_PLAYWRIGHT_VERSION" ]]; then
      info "Preparing Playwright Chromium for @playwright/test $PROJECT_PLAYWRIGHT_VERSION"
    else
      info "Preparing Playwright Chromium for this project"
    fi

    PW_INSTALL_CMD=(npx playwright install chromium)
    if [[ ${#PW_INSTALL_OPTS[@]} -gt 0 ]]; then
      PW_INSTALL_CMD=(npx playwright install "${PW_INSTALL_OPTS[@]}" chromium)
    fi

    pushd "$FRONTEND_DIR" >/dev/null
    if "${PW_INSTALL_CMD[@]}"; then
      popd >/dev/null
      ok "Playwright Chromium is ready for this project"
    else
      popd >/dev/null
      die "Playwright browser install failed. Fix the environment and rerun new-project.sh."
    fi
  fi
fi

header "Step 4: Next actions"

cat <<EOF
project created
PROJECT_ROOT: $PROJECT_ROOT
RUNTIME: $RUNTIME
Next:
  1. Prepare requirements input
  2. Run app-delivery-preflight.sh --project "$PROJECT_ROOT" --requirements <requirements-file>
  3. Use /app-delivery-start or run: app-delivery start --project "$PROJECT_ROOT" --requirements <requirements-file>
EOF
