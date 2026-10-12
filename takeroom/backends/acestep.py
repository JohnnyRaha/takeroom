"""Client for a running ACE-Step 1.5 REST server.

ACE-Step ships `start_api_server_macos.sh` (MLX backend) which serves a FastAPI
app on port 8001. FastAPI apps publish /openapi.json, so rather than hard-coding
a request shape that will rot on the next release, this client reads a small
mapping file and gives you a discovery command to regenerate it:

    python -m takeroom.discover

That prints the server's actual endpoints and field names and writes a starting
mapping to data/acestep_mapping.json. Edit that file, not this one, when the
upstream API changes.
"""
from __future__ import annotations

import base64
import binascii
import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .base import Backend, BackendError, GenRequest, GenResult, ProgressFn

# Starting point for ACE-Step 1.5 XL. Keys are our names; values are the server's field names.
DEFAULT_MAPPING: dict[str, Any] = {
    "endpoint": "/release_task",
    "method": "POST",
    "poll_endpoint": "/query_result",
    "poll_method": "POST",
    "poll_id_field": "task_id",
    "fields": {
        "style_prompt": "prompt",
        "lyrics": "lyrics",
        "duration": "audio_duration",
        "seed": "seed",
        "steps": "inference_steps",
        "guidance": "guidance_scale",
    },
    "constants": {
        "model": "acestep-v15-xl-turbo",
        "task_type": "text2music",
        "audio_format": "wav",
    },
    # Where the audio comes back. The client tries each in order.
    "audio_keys": ["file", "audio_paths", "audio_path", "audio_base64", "audio", "url", "file_path", "output_path"],
}


def load_mapping() -> dict[str, Any]:
    path = settings.mapping_path
    if path.exists():
        try:
            user = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise BackendError(f"{path} is not valid JSON: {exc}") from exc
        merged = {**DEFAULT_MAPPING, **user}
        merged["fields"] = {**DEFAULT_MAPPING["fields"], **user.get("fields", {})}
        merged["constants"] = {**DEFAULT_MAPPING["constants"], **user.get("constants", {})}
        return merged
    return json.loads(json.dumps(DEFAULT_MAPPING))


def save_mapping(mapping: dict[str, Any]) -> Path:
    settings.ensure_dirs()
    settings.mapping_path.write_text(json.dumps(mapping, indent=2) + "\n")
    return settings.mapping_path


class AceStepBackend(Backend):
    name = "acestep"

    def __init__(self) -> None:
        self.base_url = settings.acestep_url
        self.mapping = load_mapping()

    # -- plumbing -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.acestep_api_key:
            headers["Authorization"] = f"Bearer {settings.acestep_api_key}"
            headers["X-API-Key"] = settings.acestep_api_key
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=settings.acestep_timeout,
                            headers=self._headers())

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=5.0, headers=self._headers()) as c:
                spec = c.get("/openapi.json")
                if spec.status_code != 200:
                    return {"ok": False, "detail": f"openapi.json returned {spec.status_code}"}
                paths = sorted(spec.json().get("paths", {}))
                endpoint = self.mapping["endpoint"]
                return {
                    "ok": True,
                    "url": self.base_url,
                    "endpoint": endpoint,
                    "endpoint_present": endpoint in paths,
                    "paths": paths[:40],
                }
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    # -- generation ---------------------------------------------------------

    def _payload(self, req: GenRequest) -> dict[str, Any]:
        f = self.mapping["fields"]
        values = {
            "style_prompt": req.style_prompt,
            "lyrics": "" if req.instrumental else req.lyrics,
            "duration": req.duration,
            "seed": req.seed,
            "steps": req.steps,
            "guidance": req.guidance,
        }
        payload: dict[str, Any] = dict(self.mapping["constants"])
        for ours, theirs in f.items():
            if not theirs:
                continue
            value = values.get(ours)
            if value is None:
                continue
            payload[theirs] = value
        payload.update(req.extra)
        return payload

    def generate(self, req: GenRequest, out_path: Path, progress: ProgressFn) -> GenResult:
        payload = self._payload(req)
        progress("sending to me (acestep xl-turbo)")
        with self._client() as client:
            try:
                resp = client.request(
                    self.mapping.get("method", "POST"), self.mapping["endpoint"], json=payload
                )
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"could not reach ACE-Step at {self.base_url} — is start_api_server_macos.sh "
                    f"running? ({type(exc).__name__}: {exc})"
                ) from exc

            if resp.status_code >= 400:
                raise BackendError(
                    f"ACE-Step returned {resp.status_code}: {resp.text[:500]}\n"
                    f"Sent: {json.dumps(payload)[:300]}\n"
                    f"If the field names look wrong, run: python -m takeroom.discover"
                )

            # Some deployments stream the file straight back.
            ctype = resp.headers.get("content-type", "")
            if ctype.startswith("audio/") or ctype == "application/octet-stream":
                out_path.write_bytes(resp.content)
                return GenResult(audio_path=out_path, seed=req.seed)

            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise BackendError(f"unexpected non-JSON response ({ctype})") from exc

            if self.mapping.get("poll_endpoint"):
                data = self._poll(client, data, progress)

            progress("collecting audio")
            self._extract_audio(client, data, out_path)

        seed = _first_int(data, ("seed", "actual_seed", "used_seed")) or req.seed
        return GenResult(audio_path=out_path, seed=seed, raw_response=_trim(data))

    def _poll(self, client: httpx.Client, data: dict[str, Any], progress: ProgressFn) -> dict[str, Any]:
        # Extract task_id from payload, unwrap if wrapped e.g. {"data": {"task_id": "..."}}
        raw_data = data.get("data") if isinstance(data.get("data"), dict) else data
        job_id = raw_data.get(self.mapping["poll_id_field"]) or raw_data.get("task_id") or raw_data.get("id")
        if not job_id:
            raise BackendError(f"no job id in response: {json.dumps(data)[:300]}")

        endpoint = self.mapping["poll_endpoint"]
        method = self.mapping.get("poll_method", "POST")
        deadline = time.time() + settings.acestep_timeout

        while time.time() < deadline:
            try:
                if method.upper() == "POST":
                    r = client.post(endpoint, json={"task_id_list": [job_id]})
                else:
                    r = client.get(endpoint.format(id=job_id))
            except httpx.HTTPError as exc:
                raise BackendError(f"poll request failed: {exc}") from exc

            if r.status_code >= 400:
                raise BackendError(f"poll failed {r.status_code}: {r.text[:300]}")

            body = r.json()
            items = body.get("data") if isinstance(body, dict) else body
            if isinstance(items, str):
                try: items = json.loads(items)
                except Exception: items = []

            item = items[0] if isinstance(items, list) and items else (body if isinstance(body, dict) else {})
            status = item.get("status")

            # status == 1 or 'succeeded'
            if status == 1 or str(status).lower() in {"succeeded", "success", "complete", "completed", "done", "finished"}:
                res = item.get("result")
                if isinstance(res, str):
                    try:
                        parsed_res = json.loads(res)
                        if isinstance(parsed_res, list) and parsed_res:
                            item = {**item, **parsed_res[0]}
                    except Exception:
                        pass
                elif isinstance(res, list) and res:
                    item = {**item, **res[0]}
                return item

            if status == 2 or str(status).lower() in {"failed", "error", "cancelled"}:
                err = item.get("error") or json.dumps(body)[:300]
                raise BackendError(f"ACE-Step job failed: {err}")

            prog_text = item.get("progress_text") or item.get("stage") or "working"
            progress(f"generating ({prog_text})")
            time.sleep(2.0)

        raise BackendError("timed out waiting for ACE-Step")

    def _extract_audio(self, client: httpx.Client, data: dict[str, Any], out_path: Path) -> None:
        for key in self.mapping["audio_keys"]:
            value = _deep_get(data, key)
            if not value:
                continue
            if isinstance(value, list):
                value = value[0] if value else None
            if not isinstance(value, str):
                continue

            # A path on this machine (the common case — same box, same filesystem).
            candidate = Path(value).expanduser()
            if candidate.is_file():
                shutil.copyfile(candidate, out_path)
                return

            if value.startswith("http://") or value.startswith("https://"):
                r = client.get(value)
                r.raise_for_status()
                out_path.write_bytes(r.content)
                return

            if value.startswith("/") and not candidate.is_file():
                r = client.get(value)
                if r.status_code == 200 and len(r.content) > 1024:
                    out_path.write_bytes(r.content)
                    return

            payload = value.split(",", 1)[-1] if value.startswith("data:") else value
            if len(payload) > 1024:
                try:
                    out_path.write_bytes(base64.b64decode(payload, validate=True))
                    return
                except (binascii.Error, ValueError):
                    pass

        raise BackendError(
            "could not find audio in the ACE-Step response. Keys seen: "
            f"{sorted(data)[:20]}. Add the right key to 'audio_keys' in "
            f"{settings.mapping_path}"
        )


def _deep_get(obj: Any, key: str, depth: int = 4) -> Any:
    if depth < 0:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_get(v, key, depth - 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj[:5]:
            found = _deep_get(v, key, depth - 1)
            if found:
                return found
    return None


def _first_int(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _deep_get(data, key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _trim(data: Any, limit: int = 2000) -> dict[str, Any]:
    """Keep the response for the record, without stashing a base64 blob in SQLite."""
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = f"<{len(v)} chars elided>"
        elif isinstance(v, (dict, list)) and len(json.dumps(v, default=str)) > limit:
            out[k] = "<elided>"
        else:
            out[k] = v
    return out
