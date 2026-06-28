#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: app-delivery-doctor.sh [--runtime claude|opencode] [--opc-home <path>] [--framework-root <path>]
EOF
}

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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_FRAMEWORK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRAMEWORK_ROOT="${FRAMEWORK_ROOT:-$DEFAULT_FRAMEWORK_ROOT}"
PYTHON_BIN="${APP_DELIVERY_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -f "$FRAMEWORK_ROOT/.python-bin" ]]; then
  PYTHON_BIN="$(<"$FRAMEWORK_ROOT/.python-bin")"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -z "$RUNTIME" ]]; then
  RUNTIME="$(resolve_runtime_from_state "$OPC_HOME/state/active-runtime" || true)"
fi
if [[ -z "$RUNTIME" ]]; then
  RUNTIME="claude"
fi

pushd "$FRAMEWORK_ROOT" >/dev/null
"$PYTHON_BIN" -m delivery doctor --runtime "$RUNTIME" --framework-root "$FRAMEWORK_ROOT" --opc-home "$OPC_HOME"
popd >/dev/null
