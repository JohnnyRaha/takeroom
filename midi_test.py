"""Transcription tests against signals with known ground truth.

    python midi_test.py

Synthesises audio where the correct answer is known by construction — a sine at
a named pitch, a click train at a named tempo, drum-shaped bursts — and checks
the transcriber recovers it. Also validates the MIDI bytes by parsing them back.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

os.environ.setdefault("TAKEROOM_DATA", tempfile.mkdtemp(prefix="midi-test-"))

from takeroom import midi as M  # noqa: E402

SR = 44100
FAILED: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="midi-audio-"))


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not ok:
        FAILED.append(label)


def midi_to_hz(note: int) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def write(name: str, x: np.ndarray) -> Path:
    path = TMP / name
    peak = np.max(np.abs(x)) or 1.0
    sf.write(path, (x / peak * 0.8).astype(np.float32), SR)
    return path


# ---------------------------------------------------------------- MIDI bytes

def parse_midi(path: Path) -> dict:
    """Minimal SMF parser, so the test doesn't trust the writer."""
    data = path.read_bytes()
    assert data[:4] == b"MThd", "bad header"
    _, fmt, ntrks, division = struct.unpack(">IHHH", data[4:14])
    assert data[14:18] == b"MTrk", "bad track"
    (length,) = struct.unpack(">I", data[18:22])
    body = data[22:22 + length]

    pos, tempo, notes_on, pairs, tick = 0, None, {}, [], 0
    names = []
    while pos < len(body):
        value = 0
        while True:
            byte = body[pos]; pos += 1
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        tick += value
        status = body[pos]; pos += 1
        if status == 0xFF:
            kind = body[pos]; pos += 1
            size = 0
            while True:
                byte = body[pos]; pos += 1
                size = (size << 7) | (byte & 0x7F)
                if not byte & 0x80:
                    break
            payload = body[pos:pos + size]; pos += size
            if kind == 0x51:
                tempo = struct.unpack(">I", b"\x00" + payload)[0]
            elif kind == 0x03:
                names.append(payload.decode("ascii", "replace"))
            elif kind == 0x2F:
                break
        elif status & 0xF0 == 0x90:
            pitch, vel = body[pos], body[pos + 1]; pos += 2
            notes_on[pitch] = (tick, vel)
        elif status & 0xF0 == 0x80:
            pitch = body[pos]; pos += 2
            if pitch in notes_on:
                start, vel = notes_on.pop(pitch)
                pairs.append((start, tick, pitch, vel, status & 0x0F))
        elif status & 0xF0 == 0xC0:
            pos += 1
        else:
            pos += 2

    return {"format": fmt, "tracks": ntrks, "division": division,
            "bpm": round(60_000_000 / tempo, 2) if tempo else None,
            "notes": sorted(pairs), "name": names[0] if names else ""}


def test_writer() -> None:
    print("\nMIDI writer")
    path = TMP / "w.mid"
    notes = [M.Note(0.0, 0.5, 60, 100), M.Note(0.5, 1.0, 64, 80), M.Note(0.5, 1.5, 67, 90)]
    M.write_midi(path, notes, 120.0, track_name="probe", program=33)
    parsed = parse_midi(path)

    check("header sane", parsed["division"] == 480 and parsed["tracks"] == 1)
    check("tempo round-trips", parsed["bpm"] == 120.0, str(parsed["bpm"]))
    check("track name", parsed["name"] == "probe", parsed["name"])
    check("all notes present", len(parsed["notes"]) == 3, str(len(parsed["notes"])))
    starts = [n[0] for n in parsed["notes"]]
    check("timing exact at 120bpm", starts == [0, 480, 480], str(starts))
    check("velocity preserved", parsed["notes"][0][3] == 100)
    check("melodic on channel 0", all(n[4] == 0 for n in parsed["notes"]))

    drum_path = TMP / "d.mid"
    M.write_midi(drum_path, [M.Note(0.0, 0.1, 36, 100)], 120.0, drums=True)
    check("drums on channel 10", parse_midi(drum_path)["notes"][0][4] == 9)

    # A repeated pitch must retrigger, not merge.
    rep = TMP / "r.mid"
    M.write_midi(rep, [M.Note(0.0, 0.5, 60), M.Note(0.5, 1.0, 60)], 120.0)
    check("repeated pitch retriggers", len(parse_midi(rep)["notes"]) == 2)

    check("varlen encoding", M._varlen(0) == b"\x00" and M._varlen(128) == b"\x81\x00"
          and M._varlen(0x0FFFFF) == b"\xBF\xFF\x7F")


# ------------------------------------------------------------------- pitch

def test_bass() -> None:
    print("\nbass — monophonic pitch")
    # E1 G1 A1 E1, 0.5s each, sawtooth-ish so it has harmonics like a real bass
    sequence = [28, 31, 33, 28]
    x = np.zeros(0)
    for note in sequence:
        t = np.arange(int(0.5 * SR)) / SR
        f = midi_to_hz(note)
        wave = sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 8))
        env = np.minimum(1.0, np.exp(-t * 1.2) * 3)
        x = np.concatenate([x, wave * env])
    path = write("bass.wav", x)

    result = M.transcribe(path, kind="bass")
    pitches = [n.pitch for n in result.notes]
    check("found four notes", len(result.notes) == 4, f"got {len(result.notes)}: {pitches}")
    check("pitches correct", pitches == sequence, f"{pitches} vs {sequence}")
    if result.notes:
        check("first note starts near zero", result.notes[0].start < 0.08,
              f"{result.notes[0].start:.3f}")
        check("durations sane", all(0.3 < n.duration < 0.6 for n in result.notes),
              str([round(n.duration, 2) for n in result.notes]))
    check("reported monophonic", not result.polyphonic)


def test_vocal_glide() -> None:
    print("\nvocals — vibrato must not split the note")
    t = np.arange(int(1.2 * SR)) / SR
    base = midi_to_hz(64)                       # E4
    vibrato = base * (1 + 0.02 * np.sin(2 * np.pi * 5.5 * t))
    phase = 2 * np.pi * np.cumsum(vibrato) / SR
    x = np.sin(phase) + 0.3 * np.sin(2 * phase)
    path = write("vox.wav", x)

    result = M.transcribe(path, kind="vocals")
    check("one sustained note", len(result.notes) == 1, f"got {len(result.notes)}")
    if result.notes:
        check("pitch is E4", result.notes[0].pitch == 64, str(result.notes[0].pitch))


def test_octave_stability() -> None:
    print("\nlow bass — no octave errors")
    for note in (24, 28, 31, 36, 40):          # C1 to E2
        t = np.arange(int(0.8 * SR)) / SR
        f = midi_to_hz(note)
        x = sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 10))
        path = write(f"n{note}.wav", x)
        result = M.transcribe(path, kind="bass")
        got = result.notes[0].pitch if result.notes else None
        check(f"MIDI {note} ({round(f,1)} Hz)", got == note, f"got {got}")


# ------------------------------------------------------------------- tempo

def test_tempo_and_onsets() -> None:
    print("\ntempo and onsets")
    for bpm in (90.0, 120.0, 140.0):
        beat = 60.0 / bpm
        n_beats = 24
        x = np.zeros(int(n_beats * beat * SR) + SR)
        for b in range(n_beats):
            start = int(b * beat * SR)
            t = np.arange(int(0.15 * SR)) / SR
            click = np.exp(-t * 45) * np.sin(2 * np.pi * 180 * t)
            x[start:start + click.size] += click
        path = write(f"t{int(bpm)}.wav", x)
        audio, sr = M.load_mono(path)
        env, rate = M.onset_envelope(audio, sr)
        got = M.estimate_tempo(env, rate)
        onsets = M.pick_onsets(env, rate)
        check(f"{bpm} bpm detected", abs(got - bpm) < 2.0, f"got {got}")
        check(f"{bpm} bpm onset count", len(onsets) == n_beats,
              f"got {len(onsets)} of {n_beats}")


# ------------------------------------------------------------------- drums

def drum_hit(kind: str) -> np.ndarray:
    rng = np.random.default_rng(0)
    if kind == "kick":
        t = np.arange(int(0.25 * SR)) / SR
        f = 55 * np.exp(-t * 28) + 42
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 14)
    if kind == "snare":
        t = np.arange(int(0.18 * SR)) / SR
        body = np.sin(2 * np.pi * 200 * t) * np.exp(-t * 26)
        noise = rng.normal(0, 1, t.size) * np.exp(-t * 20)
        spec = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(t.size, 1 / SR)
        spec[(freqs < 250) | (freqs > 4000)] = 0
        return 0.5 * body + np.fft.irfft(spec, t.size)
    # closed hat
    t = np.arange(int(0.06 * SR)) / SR
    noise = rng.normal(0, 1, t.size) * np.exp(-t * 130)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(t.size, 1 / SR)
    spec[freqs < 7000] = 0
    return np.fft.irfft(spec, t.size)


def test_drums() -> None:
    print("\ndrums — classification")
    bpm, beat = 120.0, 0.5
    bars = 4
    x = np.zeros(int(bars * 4 * beat * SR) + SR)
    expected: list[tuple[float, int]] = []

    for bar in range(bars):
        for beat_index in range(4):
            at = (bar * 4 + beat_index) * beat
            if beat_index in (0, 2):
                hit, note = drum_hit("kick"), M.KICK
            else:
                hit, note = drum_hit("snare"), M.SNARE
            start = int(at * SR)
            x[start:start + hit.size] += hit
            expected.append((at, note))
            # eighth-note hats
            for offset in (0.0, beat / 2):
                hat = drum_hit("hat") * 0.5
                s = int((at + offset) * SR)
                x[s:s + hat.size] += hat
                expected.append((at + offset, M.CLOSED_HAT))

    path = write("drums.wav", x)
    result = M.transcribe(path, kind="drums")
    check("tempo detected", abs(result.bpm - bpm) < 3, f"got {result.bpm}")
    check("hits found", len(result.notes) >= 24, f"got {len(result.notes)}")

    # Every kick position should carry a kick; likewise snare.
    def has(note: int, at: float, tol: float = 0.06) -> bool:
        return any(n.pitch == note and abs(n.start - at) < tol for n in result.notes)

    kicks = [at for at, note in expected if note == M.KICK]
    snares = [at for at, note in expected if note == M.SNARE]
    check("kicks on 1 and 3", sum(has(M.KICK, at) for at in kicks) == len(kicks),
          f"{sum(has(M.KICK, at) for at in kicks)}/{len(kicks)}")
    check("snares on 2 and 4", sum(has(M.SNARE, at) for at in snares) >= len(snares) - 1,
          f"{sum(has(M.SNARE, at) for at in snares)}/{len(snares)}")
    check("hats present", any(n.pitch == M.CLOSED_HAT for n in result.notes))
    check("drum kind flagged", result.kind == "drums")


# -------------------------------------------------------------- quantise

def test_quantize() -> None:
    print("\nquantisation")
    bpm = 120.0
    sixteenth = (60 / bpm) / 4
    sloppy = [M.Note(sixteenth * i + (0.018 if i % 2 else -0.014), sixteenth * i + 0.1, 60 + i)
              for i in range(8)]
    snapped = M.quantize(sloppy, bpm, 4, 1.0)
    error = max(abs(n.start - round(n.start / sixteenth) * sixteenth) for n in snapped)
    check("snaps to 1/16 grid", error < 1e-9, f"max error {error:.6f}")

    half = M.quantize(sloppy, bpm, 4, 0.5)
    before = abs(sloppy[1].start - sixteenth)
    after = abs(half[1].start - sixteenth)
    check("50% strength halves the distance", abs(after - before / 2) < 1e-9,
          f"{before:.4f} -> {after:.4f}")
    check("pitches untouched", [n.pitch for n in snapped] == [n.pitch for n in sloppy])


# ------------------------------------------------------------------- misc

def test_edges() -> None:
    print("\nedge cases")
    silence = write("silence.wav", np.zeros(SR))
    for kind in ("bass", "drums", "vocals"):
        result = M.transcribe(silence, kind=kind)
        check(f"silence -> no notes ({kind})", result.notes == [], str(len(result.notes)))

    tiny = write("tiny.wav", np.sin(2 * np.pi * 440 * np.arange(500) / SR))
    ok = True
    try:
        M.transcribe(tiny, kind="vocals")
        M.transcribe(tiny, kind="drums")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("    ", exc)
    check("very short file doesn't crash", ok)

    out = TMP / "empty.mid"
    M.write_midi(out, [], 120.0)
    check("empty MIDI still valid", parse_midi(out)["notes"] == [])

    check("kind from filename", [M.kind_from_name(n) for n in
          ("drums.wav", "bass.wav", "vocals.wav", "other.wav", "htdemucs_bass.wav")]
          == ["drums", "bass", "vocals", "other", "bass"])


def main() -> int:
    test_writer()
    test_bass()
    test_vocal_glide()
    test_octave_stability()
    test_tempo_and_onsets()
    test_drums()
    test_quantize()
    test_edges()
    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all transcription checks passed'}\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
