"""End-to-end smoke test. Runs the whole pipeline on the mock backend.

    python smoke_test.py

Exercises: job queue, writer fallback, mock generation, mastering, library,
audio serving, reroll, star, delete. No model and no network required.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ["TAKEROOM_BACKEND"] = "mock"
DATA = tempfile.mkdtemp(prefix="takeroom-test-")
os.environ["TAKEROOM_DATA"] = DATA

from fastapi.testclient import TestClient  # noqa: E402

from takeroom.main import app  # noqa: E402

FAILED = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}{'  — ' + detail if detail and not condition else ''}")
    if not condition:
        FAILED.append(label)


def wait_ready(client: TestClient, take_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/takes/{take_id}").json()
        if last["status"] in {"ready", "failed"}:
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    with TestClient(app) as client:
        print("\nstatus")
        status = client.get("/api/status").json()
        check("server responds", status["backend"] == "mock")
        check("mock generator healthy", status["generator"]["ok"])

        print("\ngenerate")
        resp = client.post("/api/takes", json={
            "brief": "late night drive through wet streets",
            "style_prompt": "synthwave, 96 bpm, A minor, analogue pads",
            "lyrics": "[verse]\nheadlights on the dual carriageway\n[chorus]\nkeep driving",
            "duration": 20, "count": 2, "seed": 4242,
        })
        check("accepted", resp.status_code == 200, resp.text[:200])
        created = resp.json()["takes"]
        check("two takes queued", len(created) == 2)
        check("seeds differ", created[0]["seed"] != created[1]["seed"],
              f"{created[0]['seed']} vs {created[1]['seed']}")

        print("\nrender")
        first = wait_ready(client, created[0]["id"])
        check("reached ready", first["status"] == "ready", first.get("error", "")[:300])
        if first["status"] != "ready":
            return 1

        print("\nmastering")
        target = status["target_lufs"]
        check("loudness measured", first["input_lufs"] is not None)
        check(f"output within 1.5 LU of {target}",
              abs(first["output_lufs"] - target) < 1.5, f"got {first['output_lufs']}")
        check("peak under ceiling", first["peak_dbfs"] <= -0.99, f"got {first['peak_dbfs']}")
        check("render timed", (first["render_seconds"] or 0) > 0)

        print("\nfiles")
        check("master written", first["has_master"])
        audio = client.get(f"/api/takes/{first['id']}/audio?kind=master")
        check("wav serves", audio.status_code == 200 and len(audio.content) > 100_000,
              f"{audio.status_code}, {len(audio.content)} bytes")
        check("wav is riff", audio.content[:4] == b"RIFF")
        if first["has_mp3"]:
            mp3 = client.get(f"/api/takes/{first['id']}/audio?kind=mp3")
            check("mp3 serves", mp3.status_code == 200 and len(mp3.content) > 10_000)
        else:
            print("  skip  mp3 (ffmpeg not installed)")

        print("\nreproducibility")
        check("seed recorded", first["seed"] == 4242, str(first["seed"]))
        check("prompt recorded", first["style_prompt"].startswith("synthwave"))
        check("params recorded", first["params"]["duration"] == 20)

        print("\nactions")
        rr = client.post(f"/api/takes/{first['id']}/reroll", json={})
        check("reroll queued", rr.status_code == 200, rr.text[:200])
        child = rr.json()["takes"][0]
        check("reroll links to parent", child["parent_id"] == first["id"])
        check("reroll keeps words", child["style_prompt"] == first["style_prompt"])
        check("reroll changes seed", child["seed"] != first["seed"])

        st = client.post(f"/api/takes/{first['id']}/star", json={"starred": True})
        check("star", st.status_code == 200 and st.json()["starred"])

        print("\nwriter fallback")
        blank = client.post("/api/takes", json={"brief": "", "style_prompt": ""})
        check("empty brief rejected", blank.status_code == 400)

        print("\nui")
        for path, needle in (("/", "Takeroom"), ("/static/styles.css", "--signal"),
                             ("/static/app.js", "refreshTakes")):
            r = client.get(path)
            check(f"{path} serves", r.status_code == 200 and needle in r.text)

        print("\ncleanup")
        second = wait_ready(client, created[1]["id"])
        check("second take also ready", second["status"] == "ready", second.get("error", "")[:200])
        rm = client.delete(f"/api/takes/{first['id']}")
        check("delete", rm.status_code == 200)
        check("gone", client.get(f"/api/takes/{first['id']}").status_code == 404)

    shutil.rmtree(DATA, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
