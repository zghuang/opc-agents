#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: app-delivery-preflight.sh --project <path> --requirements <path> [--runtime claude|opencode] [--framework-root <path>] [--opc-home <path>]
EOF
}

PROJECT=""
REQUIREMENTS=""
RUNTIME=""
OPC_HOME="${OPC_HOME:-$HOME/opc}"
FRAMEWORK_ROOT=""

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

resolve_runtime_from_project() {
  local project_root="${1:-}"
  local metadata_path="$project_root/docs/project-bootstrap.json"
  [[ -f "$metadata_path" ]] || return 1
  command -v node >/dev/null 2>&1 || return 1
  node - "$metadata_path" <<'NODE'
const fs = require('fs');
const metadataPath = process.argv[2];

try {
  const payload = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
  const runtime = String(payload.runtime || '').trim().toLowerCase();
  if (runtime === 'claude' || runtime === 'opencode') {
    process.stdout.write(runtime);
    process.exit(0);
  }
} catch {}
process.exit(1);
NODE
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT="$2"
      shift 2
      ;;
    --requirements)
      REQUIREMENTS="$2"
      shift 2
      ;;
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

if [[ -z "$PROJECT" || -z "$REQUIREMENTS" ]]; then
  usage >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_FRAMEWORK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRAMEWORK_ROOT="${FRAMEWORK_ROOT:-$DEFAULT_FRAMEWORK_ROOT}"
PYTHON_BIN="${APP_DELIVERY_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -f "$FRAMEWORK_ROOT/.python-bin" ]]; then
  PYTHON_BIN="$(<"$FRAMEWORK_ROOT/.python-bin")"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -z "$RUNTIME" ]]; then
  RUNTIME="$(resolve_runtime_from_project "$PROJECT" || true)"
fi
if [[ -z "$RUNTIME" ]]; then
  RUNTIME="$(resolve_runtime_from_state "$OPC_HOME/state/active-runtime" || true)"
fi
if [[ -z "$RUNTIME" ]]; then
  RUNTIME="claude"
fi

pushd "$FRAMEWORK_ROOT" >/dev/null
"$PYTHON_BIN" -m delivery preflight --project "$PROJECT" --requirements "$REQUIREMENTS" --runtime "$RUNTIME"
popd >/dev/null
