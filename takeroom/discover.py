"""Point this at a running ACE-Step server to learn its API shape.

    python -m takeroom.discover

It prints every POST endpoint and its request fields, guesses which field is
which, and writes data/acestep_mapping.json. Check the guesses, fix anything
wrong, and you're done — the client reads that file, so you never edit Python
when upstream renames a parameter.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import httpx

from .backends.acestep import DEFAULT_MAPPING, save_mapping
from .config import settings

# ours -> candidate names on the server, best first
GUESSES: dict[str, tuple[str, ...]] = {
    "style_prompt": ("prompt", "style_prompt", "tags", "text", "description", "caption"),
    "lyrics": ("lyrics", "lyric", "lyrics_text", "prompt_lyrics"),
    "duration": ("audio_duration", "duration", "length", "seconds", "audio_length"),
    "seed": ("seed", "manual_seeds", "random_seed"),
    "steps": ("inference_steps", "infer_steps", "num_inference_steps", "steps"),
    "guidance": ("guidance_scale", "guidance", "cfg_scale", "cfg"),
}

AUDIO_HINTS = ("file", "audio_paths", "audio", "path", "url", "output", "wav", "result")

# Known ACE-Step 1.5 schema fallback when OpenAPI requestBody is un-annotated
KNOWN_ENDPOINTS: dict[str, dict[str, Any]] = {
    "/release_task": {
        "style_prompt": "prompt",
        "lyrics": "lyrics",
        "duration": "audio_duration",
        "seed": "seed",
        "steps": "inference_steps",
        "guidance": "guidance_scale",
    }
}


def fetch_spec(base_url: str) -> dict[str, Any]:
    headers = {}
    if settings.acestep_api_key:
        headers["Authorization"] = f"Bearer {settings.acestep_api_key}"
        headers["X-API-Key"] = settings.acestep_api_key
    with httpx.Client(base_url=base_url, timeout=10.0, headers=headers) as c:
        r = c.get("/openapi.json")
        r.raise_for_status()
        return r.json()


def _resolve(schema: dict[str, Any], spec: dict[str, Any], depth: int = 3) -> dict[str, Any]:
    if depth < 0 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        ref = schema["$ref"].lstrip("#/").split("/")
        node: Any = spec
        for part in ref:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return _resolve(node, spec, depth - 1)
    return schema


def request_fields(spec: dict[str, Any], operation: dict[str, Any], path: str) -> dict[str, Any]:
    body = operation.get("requestBody", {})
    content = body.get("content", {}).get("application/json", {})
    schema = _resolve(content.get("schema", {}), spec)
    props = schema.get("properties", {})
    if not props and path in KNOWN_ENDPOINTS:
        return {k: {"type": "mixed"} for k in KNOWN_ENDPOINTS[path].values()}
    return props


def response_fields(spec: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    for code in ("200", "201"):
        content = operation.get("responses", {}).get(code, {}).get("content", {})
        schema = _resolve(content.get("application/json", {}).get("schema", {}), spec)
        if schema.get("properties"):
            return sorted(schema["properties"])
    return []


def pick_endpoint(candidates: dict[str, dict[str, Any]], spec: dict[str, Any]) -> str | None:
    scored = []
    for path, fields in candidates.items():
        summary = ""
        op = spec.get("paths", {}).get(path, {}).get("post", {})
        if isinstance(op, dict):
            summary = str(op.get("summary", "")).lower()

        score = sum(1 for names in GUESSES.values() if any(n in fields for n in names))
        if path == "/release_task" or "music generate" in summary or "task" in path:
            score += 10
        elif "generat" in path.lower() or "sample" in path.lower():
            score += 3
        scored.append((score, path))

    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else settings.acestep_url
    print(f"reading {base_url}/openapi.json\n")
    try:
        spec = fetch_spec(base_url)
    except httpx.HTTPError as exc:
        print(f"could not reach {base_url}: {exc}")
        print("\nStart ACE-Step first:\n  cd ACE-Step-1.5 && ./start_api_server_macos.sh")
        return 1

    candidates: dict[str, dict[str, Any]] = {}
    responses: dict[str, list[str]] = {}

    for path, methods in sorted(spec.get("paths", {}).items()):
        for method, operation in methods.items():
            if method.lower() != "post":
                continue
            fields = request_fields(spec, operation, path)
            candidates[path] = fields
            responses[path] = response_fields(spec, operation)
            print(f"POST {path}")
            if operation.get("summary"):
                print(f"     {operation['summary']}")
            for name, schema in sorted(fields.items()):
                kind = schema.get("type", "?")
                default = schema.get("default")
                suffix = f"  (default {default!r})" if default is not None else ""
                print(f"       {name}: {kind}{suffix}")
            if responses[path]:
                print(f"     returns: {', '.join(responses[path])}")
            print()

    if not candidates:
        print("no POST endpoints found — is this the right server?")
        return 1

    endpoint = pick_endpoint(candidates, spec)
    if not endpoint:
        print("couldn't identify a generation endpoint; edit the mapping by hand.")
        endpoint = sorted(candidates)[0]

    fields = candidates.get(endpoint, {})
    mapping = json.loads(json.dumps(DEFAULT_MAPPING))
    mapping["endpoint"] = endpoint

    if endpoint == "/release_task":
        mapping["poll_endpoint"] = "/query_result"
        mapping["poll_method"] = "POST"
        mapping["poll_id_field"] = "task_id"
        mapping["constants"] = {"task_type": "text2music", "audio_format": "wav"}

    mapping["fields"] = {
        ours: next((n for n in names if n in fields), "")
        for ours, names in GUESSES.items()
    }

    returned = responses.get(endpoint, [])
    audio_keys = [k for k in returned if any(h in k.lower() for h in AUDIO_HINTS)]
    if audio_keys:
        mapping["audio_keys"] = audio_keys + DEFAULT_MAPPING["audio_keys"]

    path = save_mapping(mapping)
    print("=" * 64)
    print(f"chose POST {endpoint}\n")
    for ours, theirs in mapping["fields"].items():
        mark = "  " if theirs else "!!"
        print(f" {mark} {ours:<14} -> {theirs or '(not found — set this by hand)'}")
    print(f"\nwrote {path}")
    print("Check the mapping, then start Takeroom: ./run.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
