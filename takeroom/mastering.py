"""Mastering.

Raw generator output is quiet, inconsistent take-to-take, and often has a DC
offset and a hard cut at both ends. This does more for perceived quality than
most prompt tweaking: measure, normalise to a target LUFS, catch the peaks with
a lookahead limiter, and top and tail.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from .config import settings

TARGET_SR = 44100    # only used when a rate is explicitly configured


@dataclass
class MasterReport:
    master_path: Path
    mp3_path: Path | None
    input_lufs: float
    output_lufs: float
    peak_dbfs: float
    sample_rate: int = 0
    profile: str = "song"


def master(raw_path: Path, out_path: Path, *, target_lufs: float | None = None,
           ceiling_dbfs: float | None = None, profile: str | None = None) -> MasterReport:
    target_lufs = settings.target_lufs if target_lufs is None else target_lufs
    ceiling_dbfs = settings.ceiling_dbfs if ceiling_dbfs is None else ceiling_dbfs
    profile = (profile or settings.master_profile).lower()

    audio, sr = sf.read(raw_path, always_2d=True, dtype="float64")
    audio = _to_stereo(audio)
    audio = audio - audio.mean(axis=0, keepdims=True)      # kill DC

    # Only convert when a rate is explicitly configured. A needless 48k -> 44.1k
    # pass through the fallback resampler costs most of the top octave, which is
    # exactly where hi-hats and cymbals are judged.
    wanted = settings.target_sr
    if wanted and sr != wanted:
        audio = _resample(audio, sr, wanted)
        sr = wanted

    meter = pyln.Meter(sr)
    input_lufs = float(meter.integrated_loudness(audio))
    if not np.isfinite(input_lufs):
        input_lufs = float("-inf")

    ceiling = _db_to_lin(ceiling_dbfs)

    if profile == "sample":
        # Loops get peak-normalised and nothing else. Gain-riding a two-bar
        # break to a loudness target, then limiting it, rounds off the very
        # transients you are about to slice on.
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio *= ceiling / peak
    else:
        if np.isfinite(input_lufs):
            audio *= 10 ** ((target_lufs - input_lufs) / 20)
        audio = _limit(audio, sr, ceiling=ceiling)

    audio = _fade(audio, sr, settings.fade_in_ms, settings.fade_out_ms)

    output_lufs = float(meter.integrated_loudness(audio))
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    peak_dbfs = 20 * np.log10(peak) if peak > 0 else -np.inf

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, audio, sr, subtype="PCM_24")

    mp3_path = _encode_mp3(out_path) if settings.export_mp3 else None
    return MasterReport(out_path, mp3_path, input_lufs, output_lufs, peak_dbfs, sr, profile)


# -- chain ------------------------------------------------------------------

def _to_stereo(audio: np.ndarray) -> np.ndarray:
    if audio.shape[1] == 1:
        return np.repeat(audio, 2, axis=1)
    return audio[:, :2]


def _db_to_lin(db: float) -> float:
    return float(10 ** (db / 20))


def _limit(audio: np.ndarray, sr: int, *, ceiling: float, lookahead_ms: float = 3.0,
           release_ms: float = 90.0) -> np.ndarray:
    """Lookahead peak limiter with a smoothed gain envelope.

    Linked across channels so the stereo image doesn't wander when one side
    is louder.
    """
    if audio.size == 0:
        return audio
    look = max(1, int(sr * lookahead_ms / 1000))
    peak = np.max(np.abs(audio), axis=1)

    # Sliding max over the lookahead window: the gain must already be down by
    # the time the peak arrives.
    padded = np.concatenate([peak, np.zeros(look)])
    windowed = np.max(
        np.lib.stride_tricks.sliding_window_view(padded, look + 1), axis=1
    )[: peak.size]

    target_gain = np.where(windowed > ceiling, ceiling / np.maximum(windowed, 1e-12), 1.0)

    # One-pole release so gain recovers smoothly; attack is instant (lookahead
    # already bought us the time).
    release = float(np.exp(-1.0 / (sr * release_ms / 1000)))
    gain = np.empty_like(target_gain)
    g = 1.0
    for i, t in enumerate(target_gain):
        g = t if t < g else t + (g - t) * release
        gain[i] = g

    delayed = np.concatenate([np.zeros((look, audio.shape[1])), audio[:-look]]) if look < len(audio) else audio
    out = delayed * gain[:, None]
    return np.clip(out, -ceiling, ceiling)


def _fade(audio: np.ndarray, sr: int, in_ms: int, out_ms: int) -> np.ndarray:
    n = len(audio)
    n_in = min(int(sr * in_ms / 1000), n // 2)
    n_out = min(int(sr * out_ms / 1000), n // 2)
    if n_in > 0:
        audio[:n_in] *= np.linspace(0, 1, n_in)[:, None] ** 2
    if n_out > 0:
        audio[-n_out:] *= np.linspace(1, 0, n_out)[:, None] ** 2
    return audio


def _resample(audio: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Linear resample. Only fires if the model's rate differs from 44.1k;
    for a real rate conversion in anger, use soxr."""
    try:
        import soxr  # type: ignore

        return soxr.resample(audio, sr_in, sr_out, quality="VHQ")
    except ImportError:
        pass
    n_out = int(round(len(audio) * sr_out / sr_in))
    x_in = np.linspace(0, 1, len(audio))
    x_out = np.linspace(0, 1, n_out)
    return np.stack([np.interp(x_out, x_in, audio[:, c]) for c in range(audio.shape[1])], axis=1)


def _encode_mp3(wav_path: Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    mp3_path = wav_path.with_suffix(".mp3")
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path),
         "-codec:a", "libmp3lame", "-b:a", "320k", str(mp3_path)],
        capture_output=True,
    )
    return mp3_path if result.returncode == 0 and mp3_path.exists() else None
