from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class GenRequest:
    style_prompt: str
    lyrics: str = ""
    duration: float = 120.0
    seed: int | None = None
    steps: int = 24
    guidance: float = 7.0
    instrumental: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenResult:
    audio_path: Path
    sample_rate: int | None = None
    seed: int | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


ProgressFn = Callable[[str], None]


class Backend:
    name = "base"

    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    def generate(self, req: GenRequest, out_path: Path, progress: ProgressFn) -> GenResult:
        raise NotImplementedError


class BackendError(RuntimeError):
    pass
