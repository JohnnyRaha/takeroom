"""The writing stage.

Turns a loose brief into (a) a dense style prompt the generator can use and
(b) sectioned lyrics. Runs against a local Ollama model so nothing leaves the
machine. If Ollama isn't running, the brief is passed through unchanged rather
than failing the job — you can always write the prompt and lyrics yourself.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .config import settings

STYLE_SYSTEM = """You write style prompts for a music generation model.
Turn the user's brief into ONE line of comma-separated descriptors, 15-30 words.
Cover, in this order: genre and subgenre, tempo in BPM, key or mode, lead
instrumentation, rhythm section character, vocal type and delivery, production
era or mix character.
Use concrete musical terms. No sentences, no adjectives about how good it is,
no artist names, no song titles. Output the line and nothing else."""

LYRIC_SYSTEM = """You write song lyrics for a music generation model.
Use section tags on their own lines: [intro] [verse] [pre-chorus] [chorus]
[bridge] [outro]. Keep to the structure the user asks for, or verse/chorus/
verse/chorus/bridge/chorus if they don't say.
Lines should be singable: 6-10 syllables, consistent stress, simple rhyme.
Write about the brief's subject concretely. No title, no commentary, no
explanation. Output only the tagged lyrics."""


@dataclass
class WriterResult:
    style_prompt: str
    lyrics: str
    used_model: bool


def available() -> bool:
    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=2.0) as c:
            return c.get("/api/tags").status_code == 200
    except httpx.HTTPError:
        return False


def _chat(system: str, user: str, *, max_tokens: int = 700) -> str | None:
    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=180.0) as c:
            r = c.post("/api/chat", json={
                "model": settings.ollama_model,
                "stream": False,
                "think": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {"temperature": 0.8, "num_predict": max_tokens},
            })
            if r.status_code != 200:
                return None
            content = r.json().get("message", {}).get("content", "")
            return _strip_reasoning(content).strip() or None
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def _strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)


def write(brief: str, *, style_prompt: str = "", lyrics: str = "",
          instrumental: bool = False, structure: str = "") -> WriterResult:
    """Fill in whatever the user left blank. Anything they typed is left alone."""
    used = False

    if not style_prompt.strip():
        generated = _chat(STYLE_SYSTEM, brief, max_tokens=120)
        if generated:
            style_prompt, used = _one_line(generated), True
        else:
            style_prompt = brief.strip()

    if not instrumental and not lyrics.strip():
        ask = brief if not structure else f"{brief}\n\nStructure: {structure}"
        generated = _chat(LYRIC_SYSTEM, ask)
        if generated:
            lyrics, used = _clean_lyrics(generated), True

    return WriterResult(style_prompt=style_prompt, lyrics=lyrics, used_model=used)


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    line = re.sub(r'^["\'`]+|["\'`]+$', "", line)
    return line[:400]


def _clean_lyrics(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    # Drop any preamble before the first section tag.
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\[", ln):
            lines = lines[i:]
            break
    return "\n".join(lines).strip()
