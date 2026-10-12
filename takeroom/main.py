"""Takeroom — a local song generation studio.

Runs entirely on your machine: ACE-Step for audio, Ollama for words, Demucs for
stems, SQLite for the library. Nothing is uploaded anywhere.
"""
from __future__ import annotations

import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import midi as midi_mod
from . import prompts as prompts_mod
from . import stems as stems_mod
from . import writer
from .backends import get_backend
from .config import settings
from .jobs import Job, Runner, job_params
from .library import Library, as_public

STATIC = Path(__file__).parent / "static"

settings.ensure_dirs()
library = Library(settings.db_path)
runner = Runner(library)

@asynccontextmanager
async def lifespan(app: FastAPI):
    orphans = library.reset_orphans()
    if orphans:
        print(f"[takeroom] marked {orphans} interrupted take(s) as failed")
    runner.start()
    print(f"[takeroom] backend={settings.backend}  data={settings.data_dir}")
    print(f"[takeroom] open http://{settings.host}:{settings.port}  (http, not https)")
    yield


app = FastAPI(title="Takeroom", version="1.0", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class NewTake(BaseModel):
    brief: str = ""
    style_prompt: str = ""
    lyrics: str = ""
    title: str = ""
    structure: str = ""
    duration: float = Field(default=120.0, ge=8, le=360)
    steps: int = Field(default=24, ge=4, le=120)
    guidance: float = Field(default=7.0, ge=0.5, le=20)
    seed: int | None = None
    instrumental: bool = False
    count: int = Field(default=2, ge=1, le=4)
    backend: str = ""
    profile: str = ""          # "song" or "sample"; blank uses the default


class WriteRequest(BaseModel):
    brief: str
    structure: str = ""
    instrumental: bool = False


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

@app.get("/api/status")
def status() -> dict[str, Any]:
    try:
        health = get_backend().health()
    except Exception as exc:  # noqa: BLE001
        health = {"ok": False, "detail": str(exc)}
    return {
        "backend": settings.backend,
        "generator": health,
        "writer": {"available": writer.available(), "model": settings.ollama_model},
        "stems": stems_mod.probe(),
        "midi": {"available": True, "polyphonic": midi_mod.basic_pitch_available()},
        "mastering": {"profile": settings.master_profile,
                      "target_lufs": settings.target_lufs,
                      "target_sr": settings.target_sr or "source"},
        "queue_depth": runner.depth,
        "current": runner.current,
        "target_lufs": settings.target_lufs,
    }


# --------------------------------------------------------------------------
# takes
# --------------------------------------------------------------------------

@app.get("/api/takes")
def list_takes(limit: int = 200) -> dict[str, Any]:
    takes = []
    for take in library.recent(limit):
        # Only hit the filesystem for takes that have been separated; a glob
        # per take would otherwise cost 200 directory reads per poll.
        if take.get("stem_dir"):
            take["stems"] = stems_mod.list_stems(take["id"])
            take["midi"] = stems_mod.list_midi(take["id"])
        else:
            take["stems"] = []
            take["midi"] = []
        takes.append(as_public(take))
    return {"takes": takes}


@app.get("/api/takes/{take_id}")
def get_take(take_id: str) -> dict[str, Any]:
    take = library.get(take_id)
    if not take:
        raise HTTPException(404, "no such take")
    take["stems"] = stems_mod.list_stems(take_id)
    take["midi"] = stems_mod.list_midi(take_id)
    return as_public(take)


@app.post("/api/takes")
def create_takes(spec: NewTake) -> dict[str, Any]:
    if not (spec.brief.strip() or spec.style_prompt.strip()):
        raise HTTPException(400, "give me a brief or a style prompt")

    created = []
    for i in range(spec.count):
        seed = spec.seed if spec.seed is not None else random.getrandbits(31)
        if spec.seed is not None and i > 0:
            seed = spec.seed + i
        created.append(_enqueue(spec, seed))
    return {"takes": created}


@app.post("/api/takes/{take_id}/reroll")
def reroll(take_id: str, seed: int | None = Body(default=None, embed=True)) -> dict[str, Any]:
    """Same words, new seed. The one control that reliably changes a take."""
    take = library.get(take_id)
    if not take:
        raise HTTPException(404, "no such take")
    params = take.get("params", {})
    spec = NewTake(
        brief=take["brief"],
        style_prompt=take["style_prompt"],
        lyrics=take["lyrics"],
        title=take["title"],
        structure=params.get("structure", ""),
        duration=params.get("duration", take["duration"] or 120),
        steps=params.get("steps", 24),
        guidance=params.get("guidance", 7.0),
        instrumental=params.get("instrumental", False),
        count=1,
    )
    return {"takes": [_enqueue(spec, seed if seed is not None else random.getrandbits(31),
                               parent_id=take_id)]}


@app.post("/api/takes/{take_id}/star")
def star(take_id: str, starred: bool = Body(embed=True)) -> dict[str, Any]:
    if not library.get(take_id):
        raise HTTPException(404, "no such take")
    library.update(take_id, starred=int(starred))
    return {"id": take_id, "starred": starred}


@app.delete("/api/takes/{take_id}")
def delete_take(take_id: str) -> dict[str, Any]:
    take = library.get(take_id)
    if not take:
        raise HTTPException(404, "no such take")
    for key in ("raw_path", "master_path", "mp3_path"):
        path = take.get(key)
        if path:
            Path(path).unlink(missing_ok=True)
    library.delete(take_id)
    return {"deleted": take_id}


def _enqueue(spec: NewTake, seed: int, parent_id: str | None = None) -> dict[str, Any]:
    job_kwargs = dict(
        brief=spec.brief,
        style_prompt=spec.style_prompt,
        lyrics=spec.lyrics,
        duration=spec.duration,
        seed=seed,
        steps=spec.steps,
        guidance=spec.guidance,
        instrumental=spec.instrumental,
        structure=spec.structure,
        backend=spec.backend or settings.backend,
        parent_id=parent_id,
        profile=spec.profile,
    )
    take_id = library.create(
        title=spec.title or _title_from(spec),
        brief=spec.brief,
        style_prompt=spec.style_prompt,
        lyrics=spec.lyrics,
        duration=spec.duration,
        seed=seed,
        backend=job_kwargs["backend"],
        parent_id=parent_id,
        params={
            "duration": spec.duration, "steps": spec.steps, "guidance": spec.guidance,
            "instrumental": spec.instrumental, "structure": spec.structure,
            "profile": spec.profile or settings.master_profile,
        },
    )
    runner.submit(Job(take_id=take_id, **job_kwargs))
    return as_public(library.get(take_id) or {})


def _title_from(spec: NewTake) -> str:
    source = (spec.brief or spec.style_prompt).strip()
    words = source.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

@app.get("/api/takes/{take_id}/audio")
def audio(take_id: str, kind: str = "master"):
    take = library.get(take_id)
    if not take:
        raise HTTPException(404, "no such take")
    key = {"master": "master_path", "mp3": "mp3_path", "raw": "raw_path"}.get(kind)
    if not key:
        raise HTTPException(400, "kind must be master, mp3 or raw")
    path = take.get(key)
    if not path or not Path(path).is_file():
        raise HTTPException(404, f"no {kind} render for this take")
    media = "audio/mpeg" if kind == "mp3" else "audio/wav"
    filename = f"{_slug(take['title'] or take_id)}-{take_id}{Path(path).suffix}"
    return FileResponse(path, media_type=media, filename=filename)


# --------------------------------------------------------------------------
# stems
# --------------------------------------------------------------------------

@app.post("/api/takes/{take_id}/stems")
def make_stems(take_id: str, device: str = Body(default="", embed=True),
               force: bool = Body(default=False, embed=True)) -> dict[str, Any]:
    take = library.get(take_id)
    if not take:
        raise HTTPException(404, "no such take")
    if not take.get("master_path"):
        raise HTTPException(409, "take has no master yet")

    # Separation takes minutes. If it has already been done, hand back what is
    # on disk rather than doing it again; pass force to redo it deliberately.
    existing = stems_mod.list_stems(take_id)
    if existing and not force:
        return {"id": take_id, "stems": existing, "midi": stems_mod.list_midi(take_id),
                "cached": True}
    status = stems_mod.probe()
    if not status["available"]:
        raise HTTPException(503, str(status["detail"]))
    try:
        out = stems_mod.separate(Path(take["master_path"]), take_id, device=device or None)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    library.update(take_id, stem_dir=str(out))
    return {"id": take_id, "stems": stems_mod.list_stems(take_id),
            "midi": stems_mod.list_midi(take_id), "cached": False}


@app.get("/api/takes/{take_id}/stems/{name}")
def stem_file(take_id: str, name: str):
    path = (settings.stem_dir / take_id / name).resolve()
    if not str(path).startswith(str(settings.stem_dir.resolve())) or not path.is_file():
        raise HTTPException(404, "no such stem")
    return FileResponse(path, media_type="audio/wav", filename=f"{take_id}-{name}")


# --------------------------------------------------------------------------
# prompt presets
# --------------------------------------------------------------------------

@app.get("/api/prompts")
def prompts() -> dict[str, Any]:
    return prompts_mod.library()


@app.get("/api/prompts/{preset_id}")
def prompt(preset_id: str) -> dict[str, Any]:
    found = prompts_mod.find(preset_id)
    if not found:
        raise HTTPException(404, "no such preset")
    return found


# --------------------------------------------------------------------------
# midi
# --------------------------------------------------------------------------

class MidiRequest(BaseModel):
    stems: list[str] = []          # filenames; empty means every stem
    quantize: int = 0              # 0 off, 4 = 1/16, 2 = 1/8, 1 = 1/4
    quantize_strength: float = Field(default=1.0, ge=0.0, le=1.0)
    polyphonic: bool = True        # use basic-pitch on 'other' if installed
    bpm: float | None = None       # override; otherwise taken from the drums stem


@app.post("/api/takes/{take_id}/midi")
def make_midi(take_id: str, req: MidiRequest = Body(default=MidiRequest())) -> dict[str, Any]:
    """Transcribe separated stems to MIDI. Requires stems to exist first."""
    if not library.get(take_id):
        raise HTTPException(404, "no such take")

    paths = stems_mod.stem_paths(take_id)
    if not paths:
        raise HTTPException(409, "separate the stems first — no stems for this take")
    if req.stems:
        wanted = {s if s.endswith(".wav") else f"{s}.wav" for s in req.stems}
        paths = [p for p in paths if p.name in wanted]
        if not paths:
            raise HTTPException(404, f"none of {sorted(wanted)} exist for this take")

    # One tempo for the whole set, from the drums, so the parts line up on
    # import. Falls back to per-stem estimation when there is no drum stem.
    bpm = req.bpm or midi_mod.session_tempo(stems_mod.stem_paths(take_id))

    results, errors = [], []
    for stem_path in paths:
        try:
            results.append(midi_mod.stem_to_midi(
                stem_path, stem_path.with_suffix(".mid"),
                quantize_division=req.quantize,
                quantize_strength=req.quantize_strength,
                polyphonic=req.polyphonic,
                bpm=bpm,
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stem": stem_path.name, "error": str(exc)[:300]})

    if not results and errors:
        raise HTTPException(500, f"transcription failed: {errors[0]['error']}")

    return {
        "id": take_id,
        "parts": results,
        "errors": errors,
        "bpm": bpm,
        "polyphonic_available": midi_mod.basic_pitch_available(),
    }


@app.get("/api/takes/{take_id}/midi/{name}")
def midi_file(take_id: str, name: str):
    path = (settings.stem_dir / take_id / name).resolve()
    if not str(path).startswith(str(settings.stem_dir.resolve())) or not path.is_file():
        raise HTTPException(404, "no such midi file")
    return FileResponse(path, media_type="audio/midi", filename=f"{take_id}-{name}")


# --------------------------------------------------------------------------
# writing (without generating)
# --------------------------------------------------------------------------

@app.post("/api/write")
def write_only(req: WriteRequest) -> dict[str, Any]:
    if not writer.available():
        raise HTTPException(503, f"ollama not reachable at {settings.ollama_url}")
    result = writer.write(req.brief, instrumental=req.instrumental, structure=req.structure)
    return {"style_prompt": result.style_prompt, "lyrics": result.lyrics}


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    return "".join(keep).strip("-")[:48] or "take"


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    run()
