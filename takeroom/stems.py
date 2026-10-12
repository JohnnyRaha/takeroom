"""Stem separation.

The generator produces a mix, not stems — same as the commercial platforms.
Separating afterwards is the only option, so this shells out to Demucs.

Demucs can run from a different interpreter than Takeroom's. That matters on a
Mac: ACE-Step's venv already carries PyTorch with MPS working, and installing a
second copy into Takeroom's venv costs a couple of gigabytes and gives you two
torch versions to keep in step. Set TAKEROOM_DEMUCS_PYTHON to that interpreter
and Takeroom will call it as a subprocess.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .config import settings


def demucs_python() -> str:
    """The interpreter that should run Demucs."""
    configured = settings.demucs_python.strip()
    return configured or sys.executable


def probe() -> dict[str, object]:
    """Why Demucs is or isn't usable. The detail is what the user needs to act on."""
    python = demucs_python()

    if not Path(python).exists() and not shutil.which(python):
        return {"available": False, "python": python,
                "detail": f"interpreter not found: {python} — check TAKEROOM_DEMUCS_PYTHON"}

    result = subprocess.run(
        [python, "-c",
         "import importlib, sys;"
         "m = importlib.import_module('demucs');"
         "print(getattr(m, '__version__', 'unknown'));"
         "print('.'.join(str(v) for v in sys.version_info[:3]))"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        lines = result.stdout.strip().splitlines()
        return {
            "available": True,
            "python": python,
            "version": lines[0] if lines else "unknown",
            "python_version": lines[1] if len(lines) > 1 else "unknown",
            "model": settings.demucs_model,
        }

    stderr = (result.stderr or "").strip()
    detail = f"demucs not importable from {python}"
    if "No module named" in stderr:
        detail += " — not installed there"
    elif stderr:
        detail += f" — {stderr.splitlines()[-1][:200]}"
    return {"available": False, "python": python, "detail": detail}


def available() -> bool:
    return bool(probe()["available"])


def separate(master_path: Path, take_id: str, *, device: str | None = None) -> Path:
    out_root = settings.stem_dir / take_id
    out_root.mkdir(parents=True, exist_ok=True)
    device = device or settings.demucs_device

    cmd = [
        demucs_python(), "-m", settings.demucs_module,
        "-n", settings.demucs_model,
        "-d", device,
        "--out", str(out_root),
        str(master_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        detail = "\n".join(output.splitlines()[-6:])
        if device == "mps" and ("out of memory" in output.lower() or "MPS" in output):
            detail += "\n(MPS failed — retry with device 'cpu', or set TAKEROOM_DEMUCS_DEVICE=cpu)"
        raise RuntimeError(f"demucs failed:\n{detail}")

    produced = sorted(out_root.rglob("*.wav"))
    if not produced:
        raise RuntimeError(f"demucs wrote no stems into {out_root}")

    # Flatten htdemucs/<trackname>/*.wav up to the take directory.
    for wav in produced:
        target = out_root / wav.name
        if wav != target and not target.exists():
            shutil.move(str(wav), target)
    for child in out_root.iterdir():
        if child.is_dir() and not any(child.rglob("*.wav")):
            shutil.rmtree(child, ignore_errors=True)

    return out_root


def list_stems(take_id: str) -> list[str]:
    out_root = settings.stem_dir / take_id
    if not out_root.is_dir():
        return []
    return sorted(p.name for p in out_root.glob("*.wav"))


def stem_paths(take_id: str) -> list[Path]:
    out_root = settings.stem_dir / take_id
    if not out_root.is_dir():
        return []
    return sorted(out_root.glob("*.wav"))


def list_midi(take_id: str) -> list[str]:
    out_root = settings.stem_dir / take_id
    if not out_root.is_dir():
        return []
    return sorted(p.name for p in out_root.glob("*.mid"))
