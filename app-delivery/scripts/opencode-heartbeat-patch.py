#!/usr/bin/env python3
from __future__ import annotations

"""Offline OpenCode heartbeat patch utility.

This script is intentionally not wired into setup-opc. Normal app-delivery
runtime execution uses the machine-level `opencode` binary. Use this utility only
for explicit investigation of OpenCode liveness gaps:

    python3 scripts/opencode-heartbeat-patch.py \
        --opencode-root /path/to/opencode \
        --bun-bin /path/to/bun \
        --output-bin /tmp/opencode-heartbeat

The script temporarily patches the OpenCode source checkout, runs validation,
builds a standalone binary when requested, and restores the source on failure or
after successful validation unless --keep-patch is provided.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

START_MARKER = "// app-delivery-json-heartbeat:start"
END_MARKER = "// app-delivery-json-heartbeat:end"
EVENT_MARKER = "// app-delivery-json-heartbeat:event"
TIMER_MARKER = "// app-delivery-json-heartbeat:timer"

EMIT_BLOCK = '''        function emit(type: string, data: Record<string, unknown>) {
          if (args.format === "json") {
            process.stdout.write(
              JSON.stringify({
                type,
                timestamp: Date.now(),
                sessionID,
                ...data,
              }) + EOL,
            )
            return true
          }
          return false
        }
'''

EVENT_LOOP_LINE = "          for await (const event of events.stream) {\n"
EVENT_LOOP_REPLACEMENT = '''          for await (const event of events.stream) {
            // app-delivery-json-heartbeat:event
            heartbeatLastEventAt = Date.now()
'''

COMPLETED_BLOCK = '''          const completed = loop(client, events).catch((e) => {
            console.error(e)
            process.exitCode = 1
          })
'''

COMPLETED_REPLACEMENT = '''          // app-delivery-json-heartbeat:timer
          const heartbeatTimer = heartbeatEnabled ? setInterval(() => emitHeartbeat("interval"), heartbeatIntervalMs) : undefined
          heartbeatTimer?.unref?.()
          const completed = loop(client, events)
            .finally(() => {
              if (heartbeatTimer) clearInterval(heartbeatTimer)
            })
            .catch((e) => {
              console.error(e)
              process.exitCode = 1
            })
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patch an OpenCode source checkout to emit JSON heartbeats during `opencode run --format json`.")
    parser.add_argument("--opencode-root", required=True, help="Path to an OpenCode source checkout")
    parser.add_argument("--snippet", default=str(Path(__file__).resolve().parent / "opencode-patches" / "json-heartbeat-snippet.ts"))
    parser.add_argument("--output-bin", help="Copy the built patched opencode binary to this path")
    parser.add_argument("--state-file", help="Write the patched binary path here after a successful build")
    parser.add_argument("--bun-bin", default="", help="Path to bun. Defaults to BUN_BIN or PATH lookup.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and show planned changes without editing")
    parser.add_argument("--skip-typecheck", action="store_true", help="Skip `bun run --cwd packages/opencode typecheck`")
    parser.add_argument("--skip-build", action="store_true", help="Skip standalone binary build")
    parser.add_argument("--keep-patch", action="store_true", help="Keep source patched after successful validation")
    return parser


def target_run_ts(root: Path) -> Path:
    return root / "packages" / "opencode" / "src" / "cli" / "cmd" / "run.ts"


def platform_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        os_name = "darwin"
    elif system == "linux":
        os_name = "linux"
    else:
        os_name = system
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x64"
    else:
        arch = machine
    return f"{os_name}-{arch}"


def built_binary(root: Path) -> Path:
    return root / "packages" / "opencode" / "dist" / f"opencode-{platform_name()}" / "bin" / "opencode"


def patch_text(original: str, snippet: str) -> str:
    if START_MARKER in original or EVENT_MARKER in original or TIMER_MARKER in original:
        return original
    if EMIT_BLOCK not in original:
        raise RuntimeError("target run.ts does not contain the expected emit() block")
    if EVENT_LOOP_LINE not in original:
        raise RuntimeError("target run.ts does not contain the expected event loop line")
    if COMPLETED_BLOCK not in original:
        raise RuntimeError("target run.ts does not contain the expected non-interactive completed block")
    patched = original.replace(EMIT_BLOCK, EMIT_BLOCK + "\n" + snippet.rstrip() + "\n", 1)
    patched = patched.replace(EVENT_LOOP_LINE, EVENT_LOOP_REPLACEMENT, 1)
    patched = patched.replace(COMPLETED_BLOCK, COMPLETED_REPLACEMENT, 1)
    return patched


def run(cmd: list[str], cwd: Path) -> None:
    print("+ " + " ".join(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=cwd, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed with exit code {completed.returncode}: {' '.join(cmd)}")


def resolve_bun(raw: str) -> str:
    candidate = raw or os.environ.get("BUN_BIN") or "bun"
    if "/" in candidate:
        path = Path(candidate).expanduser()
        if path.exists() and path.is_file():
            return str(path)
        raise RuntimeError(f"bun executable not found: {path}. Install bun or pass --bun-bin /path/to/bun.")
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    raise RuntimeError("bun executable not found on PATH. Install bun or pass --bun-bin /path/to/bun.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.opencode_root).expanduser().resolve()
    run_ts = target_run_ts(root)
    snippet_path = Path(args.snippet).expanduser().resolve()
    if not run_ts.exists():
        raise SystemExit(f"OpenCode run.ts not found: {run_ts}")
    if not snippet_path.exists():
        raise SystemExit(f"heartbeat snippet not found: {snippet_path}")

    original = run_ts.read_text(encoding="utf-8")
    snippet = snippet_path.read_text(encoding="utf-8")
    patched = patch_text(original, snippet)
    already_patched = patched == original and START_MARKER in original
    bun_bin = None if args.skip_typecheck and args.skip_build else resolve_bun(args.bun_bin)

    if args.dry_run:
        print(f"opencode_root={root}")
        print(f"target={run_ts}")
        print(f"already_patched={already_patched}")
        print(f"would_modify={patched != original}")
        print(f"would_typecheck={not args.skip_typecheck}")
        print(f"would_build={not args.skip_build}")
        print(f"bun_bin={bun_bin or 'not-needed'}")
        return 0

    source_changed = patched != original
    try:
        if source_changed:
            run_ts.write_text(patched, encoding="utf-8")
            print(f"patched {run_ts}")
        else:
            print(f"source already patched: {run_ts}")

        if not args.skip_typecheck:
            assert bun_bin is not None
            run([bun_bin, "run", "--cwd", "packages/opencode", "typecheck"], root)
        if not args.skip_build:
            assert bun_bin is not None
            run([bun_bin, "./packages/opencode/script/build.ts", "--single"], root)
            binary = built_binary(root)
            if not binary.exists():
                raise RuntimeError(f"expected built binary was not created: {binary}")
            output_bin = Path(args.output_bin).expanduser().resolve() if args.output_bin else binary
            if output_bin != binary:
                output_bin.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(binary, output_bin)
                output_bin.chmod(0o755)
            if args.state_file:
                state_file = Path(args.state_file).expanduser().resolve()
                state_file.parent.mkdir(parents=True, exist_ok=True)
                state_file.write_text(str(output_bin) + "\n", encoding="utf-8")
            print(f"patched_binary={output_bin}")
    except Exception:
        if source_changed:
            run_ts.write_text(original, encoding="utf-8")
            print(f"rolled back {run_ts}", file=sys.stderr)
        raise
    else:
        if source_changed and not args.keep_patch:
            run_ts.write_text(original, encoding="utf-8")
            print(f"restored source after successful validation: {run_ts}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"opencode-heartbeat-patch: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
