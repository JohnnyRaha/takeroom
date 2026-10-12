from __future__ import annotations

from ..config import settings
from .base import Backend, BackendError, GenRequest, GenResult

__all__ = ["Backend", "BackendError", "GenRequest", "GenResult", "get_backend"]

_cache: dict[str, Backend] = {}


def get_backend(name: str | None = None) -> Backend:
    name = (name or settings.backend).lower()
    if name not in _cache:
        if name == "mock":
            from .mock import MockBackend

            _cache[name] = MockBackend()
        elif name == "acestep":
            from .acestep import AceStepBackend

            _cache[name] = AceStepBackend()
        else:
            raise BackendError(f"unknown backend {name!r} (expected 'acestep' or 'mock')")
    return _cache[name]
