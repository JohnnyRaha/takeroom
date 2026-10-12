"""Configuration. Everything is env-overridable so you can keep a .env per machine."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # --- where things live -------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_env("TAKEROOM_DATA", str(ROOT / "data"))).expanduser())

    # --- generator backend -------------------------------------------------
    # "acestep" talks to a running ACE-Step 1.5 REST server.
    # "mock"    synthesises a placeholder bounce so you can exercise the whole
    #           pipeline (queue, mastering, library, UI) with no model loaded.
    backend: str = field(default_factory=lambda: _env("TAKEROOM_BACKEND", "acestep"))
    acestep_url: str = field(default_factory=lambda: _env("ACESTEP_URL", "http://127.0.0.1:8001").rstrip("/"))
    acestep_api_key: str = field(default_factory=lambda: _env("ACESTEP_API_KEY", ""))
    acestep_timeout: float = field(default_factory=lambda: _env_float("ACESTEP_TIMEOUT", 900.0))

    # --- lyric / prompt assistant (Ollama) ---------------------------------
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen3:8b"))

    # --- mastering ---------------------------------------------------------
    target_lufs: float = field(default_factory=lambda: _env_float("TAKEROOM_TARGET_LUFS", -11.0))
    # 0 means keep whatever the generator produced. ACE-Step outputs 48 kHz;
    # converting that to 44.1 buys nothing and costs the top octave unless
    # soxr is installed. Set a rate only if your session demands one.
    target_sr: int = field(default_factory=lambda: _env_int("TAKEROOM_TARGET_SR", 0))
    # "song"   measure LUFS, normalise, limit — right for finished tracks.
    # "sample" peak-normalise only, no limiting — right for loops you will
    #          chop, where the limiter is eating the transients you need.
    master_profile: str = field(default_factory=lambda: _env("TAKEROOM_MASTER_PROFILE", "song"))
    ceiling_dbfs: float = field(default_factory=lambda: _env_float("TAKEROOM_CEILING_DBFS", -1.0))
    fade_in_ms: int = field(default_factory=lambda: _env_int("TAKEROOM_FADE_IN_MS", 15))
    fade_out_ms: int = field(default_factory=lambda: _env_int("TAKEROOM_FADE_OUT_MS", 180))
    export_mp3: bool = field(default_factory=lambda: _env_bool("TAKEROOM_EXPORT_MP3", True))

    # --- stems -------------------------------------------------------------
    demucs_model: str = field(default_factory=lambda: _env("TAKEROOM_DEMUCS_MODEL", "htdemucs"))
    # Which interpreter runs Demucs. Defaults to ours; point it at ACE-Step's
    # venv to reuse the PyTorch that is already installed there rather than
    # carrying a second multi-gigabyte copy.
    demucs_python: str = field(default_factory=lambda: _env("TAKEROOM_DEMUCS_PYTHON", ""))
    # Module to invoke. Upstream is demucs.separate; the Python 3.12+ forks
    # keep the same entry point, so this rarely needs changing.
    demucs_module: str = field(default_factory=lambda: _env("TAKEROOM_DEMUCS_MODULE", "demucs.separate"))
    demucs_device: str = field(default_factory=lambda: _env("TAKEROOM_DEMUCS_DEVICE", "mps"))

    # --- server ------------------------------------------------------------
    host: str = field(default_factory=lambda: _env("TAKEROOM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("TAKEROOM_PORT", 8800))

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def master_dir(self) -> Path:
        return self.data_dir / "masters"

    @property
    def stem_dir(self) -> Path:
        return self.data_dir / "stems"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.sqlite3"

    @property
    def mapping_path(self) -> Path:
        return self.data_dir / "acestep_mapping.json"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.master_dir, self.stem_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
