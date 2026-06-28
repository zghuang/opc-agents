#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${APP_DELIVERY_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -f "$INSTALL_ROOT/.python-bin" ]]; then
	PYTHON_BIN="$(<"$INSTALL_ROOT/.python-bin")"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" "$SCRIPT_DIR/oc-exec.py" "$@"