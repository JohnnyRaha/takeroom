"""API-level test for the MIDI endpoints, plus validation with a real parser.

    python midi_api_test.py

Fakes a set of stems on disk, calls the endpoints, and (if mido is installed)
re-opens the written files with an independent parser so we're not only
trusting our own writer.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import numpy as np
import soundfile as sf

os.environ["TAKEROOM_BACKEND"] = "mock"
DATA = tempfile.mkdtemp(prefix="takeroom-midi-api-")
os.environ["TAKEROOM_DATA"] = DATA

from fastapi.testclient import TestClient  # noqa: E402

from takeroom.config import settings  # noqa: E402
from takeroom.main import app  # noqa: E402

SR = 44100
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not ok:
        FAILED.append(label)


def fake_stems(take_id: str) -> None:
    """Write bass/drums/vocals/other stems with known content."""
    out = settings.stem_dir / take_id
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)

    # bass: E1 A1 alternating crotchets at 120 bpm
    x = np.zeros(0)
    for note in (28, 33, 28, 33):
        t = np.arange(int(0.5 * SR)) / SR
        f = 440 * 2 ** ((note - 69) / 12)
        x = np.concatenate([x, sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 8))
                            * np.exp(-t * 1.0)])
    sf.write(out / "bass.wav", (x / np.max(np.abs(x)) * 0.8).astype("float32"), SR)

    # drums: kick on 1 and 3, snare on 2 and 4
    d = np.zeros(int(2.0 * SR) + SR)
    for i in range(4):
        at = int(i * 0.5 * SR)
        t = np.arange(int(0.2 * SR)) / SR
        if i % 2 == 0:
            hit = np.sin(2 * np.pi * np.cumsum(55 * np.exp(-t * 28) + 42) / SR) * np.exp(-t * 14)
        else:
            hit = rng.normal(0, 1, t.size) * np.exp(-t * 22)
        d[at:at + hit.size] += hit
    sf.write(out / "drums.wav", (d / np.max(np.abs(d)) * 0.8).astype("float32"), SR)

    # vocals: a held A4
    t = np.arange(int(2.0 * SR)) / SR
    v = np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 880 * t)
    sf.write(out / "vocals.wav", (v / np.max(np.abs(v)) * 0.8).astype("float32"), SR)

    sf.write(out / "other.wav", (v * 0.5).astype("float32"), SR)


def main() -> int:
    with TestClient(app) as client:
        print("\nsetup")
        resp = client.post("/api/takes", json={"brief": "midi probe", "duration": 10, "count": 1})
        take_id = resp.json()["takes"][0]["id"]
        check("take created", resp.status_code == 200)

        status = client.get("/api/status").json()
        check("status reports midi", "midi" in status, str(sorted(status)))
        print(f"    polyphonic (basic-pitch) available: {status['midi']['polyphonic']}")

        print("\nguards")
        early = client.post(f"/api/takes/{take_id}/midi", json={})
        check("refuses before stems exist", early.status_code == 409, str(early.status_code))
        missing = client.post("/api/takes/nope/midi", json={})
        check("unknown take is 404", missing.status_code == 404)

        fake_stems(take_id)

        print("\ntranscription")
        resp = client.post(f"/api/takes/{take_id}/midi", json={})
        check("accepted", resp.status_code == 200, resp.text[:200])
        body = resp.json()
        parts = {p["stem"]: p for p in body["parts"]}
        check("all four stems transcribed", len(parts) == 4, str(sorted(parts)))
        check("no errors", not body["errors"], str(body["errors"])[:200])

        for name, part in sorted(parts.items()):
            print(f"    {name:12} {part['kind']:7} {part['notes']:4d} notes  "
                  f"{part['bpm']:6.1f} bpm  range {part['lowest']}-{part['highest']}  "
                  f"{part['method']}")

        check("bass found notes", parts["bass.wav"]["notes"] >= 3, str(parts["bass.wav"]["notes"]))
        check("bass in bass register", parts["bass.wav"]["lowest"] is not None
              and parts["bass.wav"]["lowest"] < 45, str(parts["bass.wav"]["lowest"]))
        check("drums found hits", parts["drums.wav"]["notes"] >= 4, str(parts["drums.wav"]["notes"]))
        check("drums use GM map", parts["drums.wav"]["lowest"] >= 35
              and parts["drums.wav"]["highest"] <= 81, str(parts["drums.wav"]))
        check("vocals found the note", parts["vocals.wav"]["notes"] >= 1)
        check("vocals pitch is A4", parts["vocals.wav"]["lowest"] == 69,
              str(parts["vocals.wav"]["lowest"]))

        print("\ndownload")
        listing = client.get(f"/api/takes/{take_id}").json()
        check("midi listed on take", len(listing["midi"]) == 4, str(listing["midi"]))
        got = client.get(f"/api/takes/{take_id}/midi/bass.mid")
        check("serves the file", got.status_code == 200 and got.content[:4] == b"MThd",
              f"{got.status_code}, {got.content[:4]!r}")
        check("content-type", got.headers["content-type"] == "audio/midi",
              got.headers.get("content-type", ""))
        check("traversal blocked",
              client.get(f"/api/takes/{take_id}/midi/..%2F..%2Flibrary.sqlite3").status_code == 404)

        print("\nselective + quantise")
        one = client.post(f"/api/takes/{take_id}/midi",
                          json={"stems": ["drums"], "quantize": 4})
        check("single stem by name", one.status_code == 200
              and len(one.json()["parts"]) == 1, one.text[:150])
        check("marked quantised", one.json()["parts"][0]["quantized"] is True)
        bad = client.post(f"/api/takes/{take_id}/midi", json={"stems": ["piano"]})
        check("unknown stem is 404", bad.status_code == 404)

        print("\nindependent parser")
        try:
            import mido  # noqa: F401
        except ImportError:
            print("    skip  mido not installed")
        else:
            import mido
            for name in ("bass.mid", "drums.mid"):
                path = settings.stem_dir / take_id / name
                try:
                    mid = mido.MidiFile(path)
                    notes = sum(1 for m in mid if m.type == "note_on" and m.velocity > 0)
                    tempos = [m.tempo for m in mid if m.type == "set_tempo"]
                    channels = {m.channel for m in mid if m.type in ("note_on", "note_off")}
                    check(f"mido opens {name}", notes > 0,
                          f"{notes} notes, {mido.tempo2bpm(tempos[0]):.1f} bpm, ch {channels}")
                    if name == "drums.mid":
                        check("mido sees drums on ch 9", channels == {9}, str(channels))
                except Exception as exc:  # noqa: BLE001
                    check(f"mido opens {name}", False, str(exc)[:200])

    shutil.rmtree(DATA, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all midi api checks passed'}\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
