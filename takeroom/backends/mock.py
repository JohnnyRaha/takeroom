"""A stand-in generator.

Renders a short chord-loop bounce so you can exercise the queue, mastering,
library, stems and UI without waiting on the model. It is deliberately crude —
if a mock take ever sounds good, something has gone wrong elsewhere.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from .base import Backend, GenRequest, GenResult, ProgressFn

SR = 44100
_PROGRESSIONS = [
    [0, 5, 7, 5],
    [0, -3, 5, 7],
    [0, 7, 9, 5],
    [0, 3, 5, 10],
]


class MockBackend(Backend):
    name = "mock"

    def health(self) -> dict:
        return {"ok": True, "url": "in-process", "endpoint": "mock", "endpoint_present": True}

    def generate(self, req: GenRequest, out_path: Path, progress: ProgressFn) -> GenResult:
        seed = req.seed
        if seed is None:
            seed = int(hashlib.sha256(req.style_prompt.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)

        duration = max(8.0, min(req.duration, 300.0))
        n = int(duration * SR)
        t = np.arange(n) / SR

        root = 110.0 * 2 ** (rng.integers(-2, 5) / 12)
        prog = _PROGRESSIONS[seed % len(_PROGRESSIONS)]
        bar = duration / len(prog)

        mix = np.zeros(n, dtype=np.float64)
        for i, semitones in enumerate(prog):
            start, end = int(i * bar * SR), int(min((i + 1) * bar, duration) * SR)
            seg = t[start:end] - t[start]
            f = root * 2 ** (semitones / 12)
            env = np.exp(-seg * 0.6)
            for k, amp in ((1, 1.0), (2, 0.4), (3, 0.22), (5, 0.1)):
                mix[start:end] += amp * env * np.sin(2 * np.pi * f * k * seg)
            progress(f"rendering bar {i + 1}/{len(prog)}")
            time.sleep(0.15)

        # A kick on every beat so the limiter has something to do.
        beat = 0.5
        for b in range(int(duration / beat)):
            start = int(b * beat * SR)
            seg = np.arange(min(int(0.12 * SR), n - start)) / SR
            if seg.size == 0:
                continue
            mix[start:start + seg.size] += 1.6 * np.exp(-seg * 32) * np.sin(
                2 * np.pi * (58 * np.exp(-seg * 22) + 38) * seg
            )

        if not req.instrumental and req.lyrics.strip():
            # A formant-ish drone standing in for a vocal.
            mix += 0.18 * np.sin(2 * np.pi * root * 4 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.7 * t))

        mix += rng.normal(0, 0.0015, n)
        mix /= np.max(np.abs(mix)) + 1e-9
        mix *= 0.35

        stereo = np.stack([mix, np.roll(mix, 220)], axis=1)
        sf.write(out_path, stereo, SR, subtype="PCM_24")
        return GenResult(audio_path=out_path, sample_rate=SR, seed=int(seed))
