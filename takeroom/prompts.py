"""Prompt presets.

A preset is a starting point, not a recipe: it fills the brief, style prompt,
tempo and length, and you edit from there. The value is in the tag vocabulary —
ACE-Step responds to dense comma-separated descriptors naming era, gear and
mix character, and those are tedious to retype.

Built-in libraries live in this package. Anything you write yourself goes in
data/prompts.user.json, which is merged on top and survives updates. Same
shape as the built-in file; a user preset whose id matches a built-in one
replaces it, so you can override without editing shipped files.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import settings

BUILTIN_DIR = Path(__file__).parent / "presets"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"_error": f"{path.name}: {exc}"}
    return data if isinstance(data, dict) else {"_error": f"{path.name}: not an object"}


def _index(categories: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in categories if isinstance(c, dict) and "id" in c}


def library() -> dict[str, Any]:
    """Built-in presets with the user's own merged over the top."""
    categories: list[dict[str, Any]] = []
    errors: list[str] = []

    for path in sorted(BUILTIN_DIR.glob("*.json")):
        data = _load(path)
        if data and "_error" in data:
            errors.append(str(data["_error"]))
            continue
        if data:
            categories.extend(data.get("categories", []))

    user_path = settings.data_dir / "prompts.user.json"
    if user_path.is_file():
        data = _load(user_path)
        if data and "_error" in data:
            errors.append(str(data["_error"]))
        elif data:
            by_id = _index(categories)
            for category in data.get("categories", []):
                if not isinstance(category, dict) or "id" not in category:
                    continue
                existing = by_id.get(category["id"])
                if not existing:
                    categories.append(category)
                    continue
                # merge presets: user ids win, new ones append
                presets = {p["id"]: p for p in existing.get("presets", []) if "id" in p}
                for preset in category.get("presets", []):
                    if isinstance(preset, dict) and "id" in preset:
                        presets[preset["id"]] = preset
                existing["presets"] = list(presets.values())

    total = sum(len(c.get("presets", [])) for c in categories)
    return {
        "categories": categories,
        "count": total,
        "user_file": str(user_path),
        "user_present": user_path.is_file(),
        "errors": errors,
    }


def find(preset_id: str) -> dict[str, Any] | None:
    for category in library()["categories"]:
        for preset in category.get("presets", []):
            if preset.get("id") == preset_id:
                return {**preset, "category": category["id"]}
    return None
