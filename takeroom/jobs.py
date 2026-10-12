"""Job runner.

Strictly one generation at a time. The model owns the GPU; running two jobs
concurrently on a Mac just means both swap and both are slow. Everything the
worker does is written back to the library as it goes, so the UI can poll.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any

from .backends import BackendError, GenRequest, get_backend
from .config import settings
from .library import Library
from .mastering import master
from . import writer


@dataclass
class Job:
    take_id: str
    brief: str
    style_prompt: str
    lyrics: str
    duration: float
    seed: int | None
    steps: int
    guidance: float
    instrumental: bool
    structure: str
    backend: str
    parent_id: str | None = None
    profile: str = ""          # "" uses the configured default


class Runner:
    def __init__(self, library: Library) -> None:
        self.library = library
        self._queue: queue.Queue[Job] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="takeroom-worker")
        self._current: str | None = None
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def submit(self, job: Job) -> None:
        self._queue.put(job)

    @property
    def depth(self) -> int:
        return self._queue.qsize() + (1 if self._current else 0)

    @property
    def current(self) -> str | None:
        return self._current

    # -- worker -------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            self._current = job.take_id
            try:
                self._run(job)
            except BackendError as exc:
                self._fail(job, str(exc))
            except Exception:  # noqa: BLE001 - worker must never die
                self._fail(job, traceback.format_exc(limit=4))
            finally:
                self._current = None
                self._queue.task_done()

    def _stage(self, job: Job, stage: str) -> None:
        self.library.update(job.take_id, status="running", stage=stage)

    def _fail(self, job: Job, message: str) -> None:
        self.library.update(job.take_id, status="failed", stage="", error=message.strip()[:4000])

    def _run(self, job: Job) -> None:
        started = time.time()
        settings.ensure_dirs()

        # 1. writing ---------------------------------------------------------
        style_prompt, lyrics = job.style_prompt, job.lyrics
        if not style_prompt.strip() or (not job.instrumental and not lyrics.strip()):
            self._stage(job, "writing")
            result = writer.write(
                job.brief, style_prompt=style_prompt, lyrics=lyrics,
                instrumental=job.instrumental, structure=job.structure,
            )
            style_prompt, lyrics = result.style_prompt, result.lyrics
        self.library.update(job.take_id, style_prompt=style_prompt, lyrics=lyrics)

        # 2. generation ------------------------------------------------------
        backend = get_backend(job.backend)
        raw_path = settings.raw_dir / f"{job.take_id}.wav"
        req = GenRequest(
            style_prompt=style_prompt,
            lyrics=lyrics,
            duration=job.duration,
            seed=job.seed,
            steps=job.steps,
            guidance=job.guidance,
            instrumental=job.instrumental,
        )
        self._stage(job, "generating")
        result = backend.generate(req, raw_path, lambda s: self._stage(job, s))
        self.library.update(job.take_id, raw_path=str(result.audio_path), seed=result.seed)

        # 3. mastering -------------------------------------------------------
        self._stage(job, "mastering")
        report = master(result.audio_path, settings.master_dir / f"{job.take_id}.wav",
                        profile=job.profile or None)

        self.library.update(
            job.take_id,
            status="ready",
            stage="",
            master_path=str(report.master_path),
            mp3_path=str(report.mp3_path) if report.mp3_path else "",
            input_lufs=_finite(report.input_lufs),
            output_lufs=_finite(report.output_lufs),
            peak_dbfs=_finite(report.peak_dbfs),
            render_seconds=round(time.time() - started, 1),
        )


def _finite(value: float) -> float | None:
    return round(value, 2) if value == value and abs(value) != float("inf") else None


def job_params(job: Job) -> dict[str, Any]:
    return {
        "duration": job.duration,
        "steps": job.steps,
        "guidance": job.guidance,
        "instrumental": job.instrumental,
        "structure": job.structure,
        "backend": job.backend,
    }
