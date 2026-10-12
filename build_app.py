#!/usr/bin/env python3
"""Build Takeroom.app — a launcher bundle for macOS.

    python3 build_app.py                      # build into ~/Applications
    python3 build_app.py --out /Applications  # somewhere else
    python3 build_app.py --check              # validate the setup, build nothing

The bundle REFERENCES your existing install rather than copying it. That is
deliberate:

  * Your venvs and the ~10 GB of ACE-Step checkpoints stay where they are.
    A copying bundle duplicates all of it, and duplicates it again if you
    build twice.
  * A copied venv is fragile — pyvenv.cfg and console-script shebangs carry
    absolute paths from where they were created.
  * Most importantly, a copy goes stale. Edit takeroom/main.py and a
    referencing bundle picks it up on next launch; a copying one doesn't
    until you rebuild and quietly runs old code in the meantime.

So the bundle is a launcher: it starts ACE-Step, starts Takeroom, waits for
both, opens the browser, and shuts both down when you quit. Roughly 200 KB.

It is not signed or notarized, so it is for this machine, not distribution.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

APP_NAME = "Takeroom.app"
BUNDLE_ID = "com.takeroom.studio"
VERSION = "1.1.0"

HERE = Path(__file__).resolve().parent


# ===========================================================================
# discovery and validation
# ===========================================================================

def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Setup:
    """Everything the launcher needs to know, resolved and checked."""

    def __init__(self, takeroom_dir: Path, acestep_dir: Path | None) -> None:
        self.takeroom_dir = takeroom_dir.resolve()
        self.env = read_env_file(self.takeroom_dir / ".env")

        guess = acestep_dir or Path(self.env.get("ACESTEP_DIR", Path.home() / "ACE-Step-1.5"))
        self.acestep_dir = Path(guess).expanduser().resolve()

        self.takeroom_python = self.takeroom_dir / ".venv" / "bin" / "python"
        self.demucs_python = self.env.get("TAKEROOM_DEMUCS_PYTHON", "")
        self.takeroom_port = self.env.get("TAKEROOM_PORT", "8800")
        self.acestep_port = "8001"

        url = self.env.get("ACESTEP_URL", "")
        if ":" in url:
            tail = url.rsplit(":", 1)[-1].strip("/")
            if tail.isdigit():
                self.acestep_port = tail

        self.problems: list[str] = []
        self.warnings: list[str] = []

    def check(self) -> bool:
        package = self.takeroom_dir / "takeroom" / "main.py"
        if not package.is_file():
            self.problems.append(
                f"no takeroom package at {self.takeroom_dir / 'takeroom'} — "
                f"point --takeroom at the directory containing run.sh")

        if not self.takeroom_python.is_file():
            self.problems.append(
                f"no venv at {self.takeroom_python} — run ./run.sh once to create it")

        if not (self.acestep_dir / "acestep").is_dir():
            self.problems.append(
                f"no ACE-Step at {self.acestep_dir} — pass --acestep /path/to/ACE-Step-1.5")
        elif not shutil.which("uv"):
            self.warnings.append("uv not on PATH — the launcher needs it to start ACE-Step")

        if self.demucs_python:
            if not Path(self.demucs_python).is_file():
                self.warnings.append(
                    f"TAKEROOM_DEMUCS_PYTHON points at {self.demucs_python}, which does not "
                    f"exist — stems will be unavailable")
        else:
            self.warnings.append(
                "TAKEROOM_DEMUCS_PYTHON is not set in .env — stems will use Takeroom's own "
                "interpreter, which probably has no demucs")

        index = self.takeroom_dir / "takeroom" / "static" / "index.html"
        if index.is_file() and "midi.js" not in index.read_text():
            self.warnings.append(
                'index.html has no midi.js include — add '
                '<script src="/static/midi.js"></script> before </body> for the MIDI button')

        if not (self.takeroom_dir / "data" / "acestep_mapping.json").is_file():
            self.warnings.append(
                "no data/acestep_mapping.json — run takeroom.discover once, or generation "
                "will 404")

        return not self.problems

    def report(self) -> None:
        print(f"  takeroom    {self.takeroom_dir}")
        print(f"  python      {self.takeroom_python}")
        print(f"  acestep     {self.acestep_dir}")
        print(f"  ports       takeroom {self.takeroom_port}, acestep {self.acestep_port}")
        print(f"  demucs      {self.demucs_python or '(not configured)'}")
        for warning in self.warnings:
            print(f"  warn        {warning}")
        for problem in self.problems:
            print(f"  ERROR       {problem}")


# ===========================================================================
# icon — minimal PNG encoder, no dependencies
# ===========================================================================

def _png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                       # filter type 0
        raw += rgba[y * stride:(y + 1) * stride]

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _icon_pixels(size: int) -> bytes:
    """A painted-steel square with a teal signal mark — the app's own palette."""
    panel = (0x19, 0x1C, 0x1A)
    signal = (0x7F, 0xB7, 0xA8)
    lamp = (0xE3, 0x9B, 0x2F)

    radius = size * 0.22
    out = bytearray(size * size * 4)

    for y in range(size):
        for x in range(size):
            # rounded-rect mask
            dx = max(radius - x, 0, x - (size - 1 - radius))
            dy = max(radius - y, 0, y - (size - 1 - radius))
            inside = (dx * dx + dy * dy) <= radius * radius

            r, g, b, a = 0, 0, 0, 0
            if inside:
                shade = 1.0 + 0.12 * (1.0 - y / size)
                r, g, b, a = (min(255, int(panel[0] * shade)),
                              min(255, int(panel[1] * shade)),
                              min(255, int(panel[2] * shade)), 255)

                # three horizontal bars of differing length: a level meter
                bars = [(0.38, 0.72), (0.52, 0.52), (0.66, 0.84)]
                for centre, length in bars:
                    top = size * (centre - 0.035)
                    bottom = size * (centre + 0.035)
                    left = size * 0.16
                    right = size * (0.16 + 0.68 * length)
                    if top <= y <= bottom and left <= x <= right:
                        r, g, b = signal

                # a single lamp, top left
                lx, ly, lr = size * 0.22, size * 0.22, size * 0.055
                if (x - lx) ** 2 + (y - ly) ** 2 <= lr * lr:
                    r, g, b = lamp

            i = (y * size + x) * 4
            out[i:i + 4] = bytes((r, g, b, a))
    return bytes(out)


def build_icon(resources: Path) -> bool:
    """Write AppIcon.icns if iconutil is available, else skip quietly."""
    if not shutil.which("iconutil"):
        return False
    iconset = resources / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    for size in (16, 32, 64, 128, 256, 512):
        data = _png(size, size, _icon_pixels(size))
        (iconset / f"icon_{size}x{size}.png").write_bytes(data)
        half = size // 2
        if half >= 16:
            (iconset / f"icon_{half}x{half}@2x.png").write_bytes(data)

    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")],
        capture_output=True, text=True)
    shutil.rmtree(iconset, ignore_errors=True)
    if result.returncode != 0:
        print(f"  note: iconutil failed ({result.stderr.strip()[:120]}) — using default icon")
        return False
    return True


# ===========================================================================
# bundle contents
# ===========================================================================

LAUNCHER = r'''#!/bin/bash
# Takeroom.app launcher. Starts ACE-Step and Takeroom, opens the UI, and stops
# whatever it started when you quit. Bash 3.2 compatible — macOS ships 2007 bash.
set -uo pipefail

RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"
. "$RESOURCES/config.sh"

LOG_DIR="$TAKEROOM_DIR/logs"
mkdir -p "$LOG_DIR"
exec >> "$LOG_DIR/app.log" 2>&1
echo "=== $(date) launch ==="

ACESTEP_PID=""
TAKEROOM_PID=""
CLEANED=""
EXIT_CODE=0

note() { osascript -e "display notification \"$1\" with title \"Takeroom\"" >/dev/null 2>&1; }

fail() {
  echo "FATAL: $1"
  osascript -e "display dialog \"$1\" with title \"Takeroom\" buttons {\"OK\"} default button 1 with icon stop" >/dev/null 2>&1
  EXIT_CODE=1
  exit 1
}

cleanup() {
  # The trap is set on INT, TERM and EXIT, and exiting from a signal handler
  # fires EXIT too — so guard against running this twice.
  [ -n "$CLEANED" ] && return
  CLEANED=1
  echo "--- shutting down ---"
  # Only stop what this launcher started; anything you ran by hand stays up.
  if [ -n "$TAKEROOM_PID" ]; then kill "$TAKEROOM_PID" 2>/dev/null; fi
  if [ -n "$ACESTEP_PID" ]; then kill "$ACESTEP_PID" 2>/dev/null; fi
  sleep 1
  if [ -n "$TAKEROOM_PID" ]; then kill -9 "$TAKEROOM_PID" 2>/dev/null; fi
  if [ -n "$ACESTEP_PID" ]; then kill -9 "$ACESTEP_PID" 2>/dev/null; fi
  exit "$EXIT_CODE"
}
trap cleanup INT TERM EXIT

up() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$1$2"; }

wait_for() {
  local port="$1" path="$2" name="$3" limit="$4" waited=0
  while [ "$waited" -lt "$limit" ]; do
    if up "$port" "$path"; then echo "$name ready after ${waited}s"; return 0; fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

[ -d "$TAKEROOM_DIR" ] || fail "Takeroom is not at $TAKEROOM_DIR. Rebuild the app with build_app.py."
[ -x "$TAKEROOM_PYTHON" ] || fail "No Python at $TAKEROOM_PYTHON. Run ./run.sh once, then rebuild the app."

# --- ACE-Step -------------------------------------------------------------
if up "$ACESTEP_PORT" "/openapi.json"; then
  echo "acestep already running on $ACESTEP_PORT"
else
  [ -d "$ACESTEP_DIR" ] || fail "ACE-Step is not at $ACESTEP_DIR."
  command -v uv >/dev/null 2>&1 || fail "uv is not on PATH, so ACE-Step cannot start. Install it, or start ACE-Step yourself before opening this app."
  note "Starting ACE-Step — first launch loads several GB"
  ( cd "$ACESTEP_DIR" && ACESTEP_LM_BACKEND=mlx exec uv run acestep-api \
      --host 127.0.0.1 --port "$ACESTEP_PORT" ) >> "$LOG_DIR/acestep.log" 2>&1 &
  ACESTEP_PID=$!
  if ! wait_for "$ACESTEP_PORT" "/openapi.json" "acestep" "$ACESTEP_TIMEOUT"; then
    tail -20 "$LOG_DIR/acestep.log"
    fail "ACE-Step did not start within ${ACESTEP_TIMEOUT}s. See logs/acestep.log."
  fi
fi

# --- Takeroom -------------------------------------------------------------
if up "$TAKEROOM_PORT" "/api/status"; then
  echo "takeroom already running on $TAKEROOM_PORT"
else
  # run.sh sources .env before starting; do the same here, or settings that
  # work from the terminal are silently ignored when launched from the app.
  ( cd "$TAKEROOM_DIR"
    if [ -f .env ]; then set -a; . ./.env; set +a; fi
    exec "$TAKEROOM_PYTHON" -m takeroom.main ) >> "$LOG_DIR/takeroom.log" 2>&1 &
  TAKEROOM_PID=$!
  if ! wait_for "$TAKEROOM_PORT" "/api/status" "takeroom" 90; then
    tail -20 "$LOG_DIR/takeroom.log"
    fail "Takeroom did not start. See logs/takeroom.log."
  fi
fi

note "Studio ready"
open "http://127.0.0.1:$TAKEROOM_PORT"

# Stay alive so quitting the app stops the servers.
while true; do
  sleep 5
  if [ -n "$TAKEROOM_PID" ] && ! kill -0 "$TAKEROOM_PID" 2>/dev/null; then
    echo "takeroom exited"; break
  fi
done
'''


def config_sh(setup: Setup) -> str:
    lines = [
        "# Generated by build_app.py — rebuild rather than editing.",
        f'TAKEROOM_DIR="{setup.takeroom_dir}"',
        f'TAKEROOM_PYTHON="{setup.takeroom_python}"',
        f'ACESTEP_DIR="{setup.acestep_dir}"',
        f'TAKEROOM_PORT="{setup.takeroom_port}"',
        f'ACESTEP_PORT="{setup.acestep_port}"',
        'ACESTEP_TIMEOUT="300"',
        "",
        "# uv and brew live here; a Finder launch does not inherit your shell PATH.",
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"',
        'export TOKENIZERS_PARALLELISM="false"',
    ]
    return "\n".join(lines) + "\n"


def info_plist(has_icon: bool) -> bytes:
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "Takeroom",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Takeroom",
        "CFBundleDisplayName": "Takeroom",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleSignature": "????",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        # No network server entitlement is needed for localhost, but declaring
        # the usage description keeps the first-launch prompt intelligible.
        "NSLocalNetworkUsageDescription":
            "Takeroom runs its studio server on this machine only.",
    }
    if has_icon:
        plist["CFBundleIconFile"] = "AppIcon"
    return plistlib.dumps(plist)


def build(setup: Setup, out_dir: Path) -> Path:
    app = out_dir / APP_NAME
    if app.exists():
        shutil.rmtree(app)

    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    (resources / "config.sh").write_text(config_sh(setup))

    launcher = macos / "Takeroom"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)

    has_icon = build_icon(resources)
    (app / "Contents" / "Info.plist").write_bytes(info_plist(has_icon))
    (app / "Contents" / "PkgInfo").write_text("APPL????")

    # Unsigned bundles built locally still get quarantined if they were ever
    # downloaded; clearing it here saves a Gatekeeper argument later.
    if shutil.which("xattr"):
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)],
                       capture_output=True)

    return app


# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Build Takeroom.app")
    parser.add_argument("--takeroom", type=Path, default=HERE,
                        help="Takeroom project directory (default: this one)")
    parser.add_argument("--acestep", type=Path, default=None,
                        help="ACE-Step-1.5 directory (default: ~/ACE-Step-1.5)")
    parser.add_argument("--out", type=Path, default=Path.home() / "Applications",
                        help="where to put the bundle")
    parser.add_argument("--check", action="store_true",
                        help="validate only, build nothing")
    args = parser.parse_args()

    if sys.platform != "darwin" and not args.check:
        print("This builds a macOS bundle; --check works anywhere.")
        return 1

    setup = Setup(args.takeroom, args.acestep)
    ok = setup.check()

    print("\nresolved:")
    setup.report()

    if not ok:
        print("\nfix the errors above and run again.\n")
        return 1
    if args.check:
        print("\nsetup looks buildable.\n")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    app = build(setup, args.out)

    print(f"\nbuilt {app}")
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"  {size / 1024:.0f} KB — it references your install, it does not copy it")
    print(f"  logs: {setup.takeroom_dir / 'logs'}/app.log")
    print("\nFirst launch: right-click the app and choose Open, once, to get past")
    print("Gatekeeper. After that it opens normally. Quitting it stops both servers.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
