#!/usr/bin/env bash
set -euo pipefail

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

usage() {
  cat <<'EOF'
Usage: setup-opc.sh [--runtime claude|opencode] [--opc-home <path>] [--framework-root <path>] [--channel none|telegram]

Prepares an app-delivery runtime under OPC_HOME:
- validates the current host with `python3 -m delivery doctor`
- copies framework runtime assets into OPC_HOME/opc-agents/app-delivery
- deploys Hermes skills into ~/.hermes/skills/opc-agents
- creates wrapper commands under OPC_HOME/bin

EOF
}

RUNTIME=""
OPC_HOME="${OPC_HOME:-$HOME/opc}"
FRAMEWORK_ROOT=""
CHANNEL="none"

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
    --channel)
      CHANNEL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$RUNTIME" ]]; then
  echo "--runtime is required (claude or opencode)" >&2
  usage >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_FRAMEWORK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRAMEWORK_ROOT="${FRAMEWORK_ROOT:-$DEFAULT_FRAMEWORK_ROOT}"
INSTALL_ROOT="$OPC_HOME/opc-agents/app-delivery"
BIN_DIR="$OPC_HOME/bin"
STATE_DIR="$OPC_HOME/state"
PROJECTS_DIR="$OPC_HOME/projects"
SKILLS_DST="$HOME/.hermes/skills/opc-agents"
WATCHDOG_FILE="$STATE_DIR/watchdog-deliver"

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/usr/local/sbin:/opt/homebrew/bin:/opt/homebrew/sbin:/Users/hzg/.local/bin:/Users/hzg/.opencode/bin:/Users/hzg/.npm-global/bin:/Users/hzg/.lmstudio/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

resolve_python_path() {
  local candidate="${1:-}"
  if [[ -z "$candidate" ]]; then
    return 1
  fi
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || return 1
    printf '%s\n' "$candidate"
    return 0
  fi
  command -v "$candidate" 2>/dev/null || return 1
}

python_supports_framework() {
  local candidate="${1:-}"
  [[ -n "$candidate" ]] || return 1
  "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 14) <= sys.version_info[:2] < (3, 15) else 1)
PY
}

resolve_framework_python() {
  local raw_candidate=""
  local candidate_path=""
  local -a candidates=(
    "${APP_DELIVERY_PYTHON:-}"
    "python3"
    "python3.14"
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
  )
  for raw_candidate in "${candidates[@]}"; do
    candidate_path="$(resolve_python_path "$raw_candidate" || true)"
    if [[ -n "$candidate_path" ]] && python_supports_framework "$candidate_path"; then
      printf '%s\n' "$candidate_path"
      return 0
    fi
  done
  fail "Could not find a Python interpreter compatible with app-delivery (requires >=3.14,<3.15)." >&2
  return 1
}

render_doctor_report() {
  local report_file="$1"
  "$PYTHON_BIN" - <<'PY' "$report_file"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
status = str(payload.get("status") or "unknown")
runtime = str(payload.get("runtime") or "unknown")
framework_root = str(payload.get("framework_root") or "")
checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []

print(f"Doctor status: {status}")
print(f"Runtime: {runtime}")
if framework_root:
    print(f"Framework root: {framework_root}")
print("")
for row in checks:
    if not isinstance(row, dict):
        continue
    name = str(row.get("name") or "<unknown>")
    check_status = str(row.get("status") or "unknown")
    message = str(row.get("message") or "")
    marker = {
        "ok": "✓",
        "fail": "✗",
        "missing": "✗",
        "error": "✗",
    }.get(check_status, "•")
    print(f"  {marker} {name}: {message}")
PY
}

mkdir -p "$BIN_DIR" "$STATE_DIR" "$PROJECTS_DIR" "$HOME/.hermes/skills"

PYTHON_BIN="$(resolve_framework_python)"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   App-delivery Environment Setup     ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""
info "Framework root: $FRAMEWORK_ROOT"
info "OPC home:       $OPC_HOME"
info "Runtime:        $RUNTIME"
info "Channel:        $CHANNEL"

header "Step 1: Validating host and framework prerequisites..."
pushd "$FRAMEWORK_ROOT" >/dev/null
DOCTOR_REPORT_FILE="$(mktemp)"
if "$PYTHON_BIN" -m delivery doctor --runtime "$RUNTIME" --framework-root "$FRAMEWORK_ROOT" --opc-home "$OPC_HOME" > "$DOCTOR_REPORT_FILE"; then
  render_doctor_report "$DOCTOR_REPORT_FILE"
  ok "Doctor checks passed"
else
  render_doctor_report "$DOCTOR_REPORT_FILE" || true
  rm -f "$DOCTOR_REPORT_FILE"
  die "Doctor checks failed. Fix the reported environment issues and rerun setup-opc.sh."
fi
rm -f "$DOCTOR_REPORT_FILE"
popd >/dev/null

header "Step 2: Installing framework runtime into OPC_HOME..."
rm -rf "$INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT"
cp -R "$FRAMEWORK_ROOT/delivery" "$INSTALL_ROOT/"
cp -R "$FRAMEWORK_ROOT/project_temp" "$INSTALL_ROOT/"
cp -R "$FRAMEWORK_ROOT/skills" "$INSTALL_ROOT/"
cp -R "$FRAMEWORK_ROOT/mock-server" "$INSTALL_ROOT/"
cp -R "$FRAMEWORK_ROOT/scripts" "$INSTALL_ROOT/"
cp "$FRAMEWORK_ROOT/pyproject.toml" "$INSTALL_ROOT/"
cp "$FRAMEWORK_ROOT/AGENTS.md" "$INSTALL_ROOT/"
cp "$FRAMEWORK_ROOT/CLAUDE.md" "$INSTALL_ROOT/"
printf '%s\n' "$PYTHON_BIN" > "$INSTALL_ROOT/.python-bin"
chmod +x "$INSTALL_ROOT/scripts"/*.sh "$INSTALL_ROOT/scripts"/*.py 2>/dev/null || true

mkdir -p "$OPC_HOME/scripts"
cat > "$OPC_HOME/scripts/opc-pre-tool-guard.py" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$PYTHON_BIN" "$INSTALL_ROOT/scripts/app_delivery_pre_tool_guard.py" "\$@"
EOF
chmod +x "$OPC_HOME/scripts/opc-pre-tool-guard.py"

cat > "$BIN_DIR/app-delivery" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export APP_DELIVERY_ENFORCE_OPC_PROJECT_ROOT=1
cd "$INSTALL_ROOT"
exec "$PYTHON_BIN" -m delivery "\$@"
EOF

cat > "$BIN_DIR/new-project.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/scripts/new-project.sh" --opc-home "$OPC_HOME" --framework-root "$INSTALL_ROOT" "\$@"
EOF

cat > "$BIN_DIR/app-delivery-doctor.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/scripts/app-delivery-doctor.sh" --opc-home "$OPC_HOME" --framework-root "$INSTALL_ROOT" "\$@"
EOF

cat > "$BIN_DIR/app-delivery-preflight.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export APP_DELIVERY_ENFORCE_OPC_PROJECT_ROOT=1
exec "$INSTALL_ROOT/scripts/app-delivery-preflight.sh" --opc-home "$OPC_HOME" --framework-root "$INSTALL_ROOT" "\$@"
EOF

rm -f "$BIN_DIR/app-delivery-start.sh" "$BIN_DIR/app-delivery-build-loop.sh"

chmod +x "$BIN_DIR/app-delivery" "$BIN_DIR/new-project.sh" "$BIN_DIR/app-delivery-doctor.sh" "$BIN_DIR/app-delivery-preflight.sh"
ok "Wrapper commands installed under $BIN_DIR"
ok "Hermes pre-tool guard compatibility wrapper installed under $OPC_HOME/scripts"

rm -rf "$SKILLS_DST"
mkdir -p "$SKILLS_DST"
cp -R "$INSTALL_ROOT/skills/." "$SKILLS_DST/"
ok "Hermes skills deployed to $SKILLS_DST"

# Experimental only: scripts/opencode-heartbeat-patch.py can build a patched
# OpenCode binary offline for investigation, but setup-opc intentionally does
# not enable it. Runtime tasks use the machine-level `opencode` command unless
# an operator explicitly sets APP_DELIVERY_OPENCODE_BIN outside this script.

printf '%s\n' "$RUNTIME" > "$STATE_DIR/active-runtime"
if [[ "$CHANNEL" == "telegram" ]]; then
  printf '%s\n' 'telegram:-1003960076129:486' > "$WATCHDOG_FILE"
else
  printf '%s\n' 'local' > "$WATCHDOG_FILE"
fi

header "Step 3: Final status"

cat <<EOF
app-delivery runtime installed
OPC_HOME: $OPC_HOME
FRAMEWORK_ROOT: $INSTALL_ROOT
RUNTIME: $RUNTIME
HERMES_SKILLS: $SKILLS_DST
PATH hint: export PATH="$BIN_DIR:\$PATH"
EOF
