"""Audio to MIDI.

Turns a separated stem into a MIDI part you can drop on a Cubase track. The
strategy depends on the stem, because one transcriber is wrong for all of them:

    bass, vocals   monophonic pitch tracking (YIN) — one note at a time,
                   which is what these stems actually are. Far more reliable
                   than a polyphonic model, which octave-errors on low bass.
    drums          onset detection, then each hit classified into a General
                   MIDI drum note by where its energy sits.
    other          polyphonic if basic-pitch is installed, otherwise
                   monophonic on the dominant line (and it says so).

Everything here is numpy and the standard library — no new dependencies. The
MIDI writer is ~80 lines because the format is simple and a dependency is not
worth it.

Transcription is an estimate. Bass and drums come out usable; vocals give you
the melody but not the phrasing; dense material needs editing. Treat the output
as a starting part, not a transfer.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

import numpy as np
import soundfile as sf

TICKS_PER_BEAT = 480
PITCH_SR = 22050          # plenty for pitch; halves the analysis cost
StemKind = Literal["bass", "drums", "vocals", "other"]

# General MIDI percussion
KICK, SNARE, CLOSED_HAT, OPEN_HAT, CRASH, TOM = 36, 38, 42, 46, 49, 45


@dataclass
class Note:
    start: float          # seconds
    end: float
    pitch: int            # MIDI note number
    velocity: int = 90

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Transcription:
    notes: list[Note]
    bpm: float
    kind: StemKind
    polyphonic: bool
    method: str


# ===========================================================================
# MIDI file writing
# ===========================================================================

def _varlen(value: int) -> bytes:
    """MIDI variable-length quantity."""
    if value < 0:
        raise ValueError("negative delta time")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def _meta(kind: int, payload: bytes) -> bytes:
    return b"\xFF" + bytes([kind]) + _varlen(len(payload)) + payload


def write_midi(path: Path, notes: Iterable[Note], bpm: float, *,
               track_name: str = "stem", drums: bool = False,
               program: int | None = None) -> Path:
    """Write a type-0 SMF. Drums go on channel 10 (index 9), as the spec wants."""
    channel = 9 if drums else 0
    bpm = max(20.0, min(bpm, 300.0))
    ticks_per_second = TICKS_PER_BEAT * bpm / 60.0

    events: list[tuple[int, int, bytes]] = []   # (tick, order, bytes)
    for note in notes:
        pitch = max(0, min(127, int(note.pitch)))
        velocity = max(1, min(127, int(note.velocity)))
        on = int(round(note.start * ticks_per_second))
        off = max(on + 1, int(round(note.end * ticks_per_second)))
        # note-off sorts before note-on at the same tick, so a repeated pitch
        # retriggers cleanly instead of being swallowed.
        events.append((off, 0, bytes([0x80 | channel, pitch, 0])))
        events.append((on, 1, bytes([0x90 | channel, pitch, velocity])))

    events.sort(key=lambda e: (e[0], e[1]))

    track = bytearray()
    track += _varlen(0) + _meta(0x03, track_name.encode("ascii", "replace")[:127])
    track += _varlen(0) + _meta(0x51, struct.pack(">I", int(60_000_000 / bpm))[1:])
    if program is not None and not drums:
        track += _varlen(0) + bytes([0xC0 | channel, max(0, min(127, program))])

    previous = 0
    for tick, _order, payload in events:
        track += _varlen(tick - previous) + payload
        previous = tick
    track += _varlen(0) + _meta(0x2F, b"")

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, TICKS_PER_BEAT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"MTrk" + struct.pack(">I", len(track)) + bytes(track))
    return path


# ===========================================================================
# shared analysis
# ===========================================================================

def load_mono(path: Path, target_sr: int | None = None) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(path, always_2d=True, dtype="float64")
    mono = audio.mean(axis=1)
    if target_sr and sr != target_sr:
        mono, sr = _decimate(mono, sr, target_sr), target_sr
    peak = np.max(np.abs(mono)) if mono.size else 0.0
    if peak > 0:
        mono = mono / peak
    return mono, sr


def _decimate(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Cheap anti-aliased downsample: box filter then linear interpolate."""
    if sr_out >= sr_in:
        return x
    factor = sr_in / sr_out
    width = max(1, int(factor))
    kernel = np.ones(width) / width
    smoothed = np.convolve(x, kernel, mode="same")
    n_out = int(len(x) / factor)
    idx = np.arange(n_out) * factor
    lo = np.floor(idx).astype(int)
    hi = np.minimum(lo + 1, len(smoothed) - 1)
    frac = idx - lo
    return smoothed[lo] * (1 - frac) + smoothed[hi] * frac


def _stft_magnitude(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    window = np.hanning(n_fft)
    n_frames = 1 + (len(x) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n_frames, n_fft), strides=(x.strides[0] * hop, x.strides[0])
    )
    return np.abs(np.fft.rfft(frames * window, axis=1))


def onset_envelope(x: np.ndarray, sr: int, hop: int = 256,
                   n_fft: int = 1024) -> tuple[np.ndarray, float]:
    """Spectral flux: sum of positive frame-to-frame magnitude change.

    Frames are centred — frame j is centred on sample j*hop — so an onset is
    timestamped where it happens rather than up to a window early. The flux of
    the first frame is taken against silence, not against itself, so a hit at
    t=0 registers instead of being swallowed.
    """
    half = n_fft // 2
    padded = np.concatenate([np.zeros(half), x, np.zeros(n_fft)])
    mag = _stft_magnitude(padded, n_fft, hop)
    flux = np.diff(mag, axis=0, prepend=np.zeros_like(mag[:1]))
    env = np.sum(np.maximum(flux, 0.0), axis=1)
    if env.size and env.max() > 0:
        env = env / env.max()
    return env, sr / hop


def estimate_tempo(env: np.ndarray, frame_rate: float,
                   lo_bpm: float = 60.0, hi_bpm: float = 190.0) -> float:
    """Autocorrelate the onset envelope; the strongest lag in range is the beat."""
    if env.size < 16:
        return 120.0
    centred = env - env.mean()
    corr = np.correlate(centred, centred, mode="full")[len(centred) - 1:]
    lag_lo = max(1, int(frame_rate * 60.0 / hi_bpm))
    lag_hi = min(len(corr) - 1, int(frame_rate * 60.0 / lo_bpm))
    if lag_hi <= lag_lo:
        return 120.0
    window = corr[lag_lo:lag_hi]
    if not np.any(window > 0):
        return 120.0
    best = int(np.argmax(window)) + lag_lo
    bpm = 60.0 * frame_rate / best

    # Prefer the octave that lands nearest 100-140, where most music sits.
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return round(float(bpm), 2)


def pick_onsets(env: np.ndarray, frame_rate: float, *, delta: float = 0.08,
                min_gap_s: float = 0.045) -> np.ndarray:
    """Peak-pick against a local mean, with a refractory period.

    The neighbour comparison is padded with -inf so the first and last frames
    can win — otherwise a stem that opens on a downbeat loses it.
    """
    if env.size == 0:
        return np.array([])
    window = max(1, min(int(frame_rate * 0.10), env.size))
    kernel = np.ones(window) / window
    local_mean = np.convolve(env, kernel, mode="same")[:env.size]
    threshold = local_mean + delta

    padded = np.concatenate([[-np.inf], env, [-np.inf]])
    left, centre, right = padded[:-2], padded[1:-1], padded[2:]
    peaks = np.where((centre > threshold) & (centre >= left) & (centre > right))[0]

    min_gap = int(frame_rate * min_gap_s)
    kept: list[int] = []
    for p in peaks:
        if not kept or p - kept[-1] >= min_gap:
            kept.append(int(p))
        elif env[p] > env[kept[-1]]:
            kept[-1] = int(p)
    return np.array(kept) / frame_rate


# ===========================================================================
# monophonic pitch (YIN)
# ===========================================================================

def yin_f0(x: np.ndarray, sr: int, *, fmin: float, fmax: float,
           frame: int = 2048, hop: int = 256,
           threshold: float = 0.15) -> tuple[np.ndarray, np.ndarray, float]:
    """Return (f0 per frame, voiced mask, frame_rate). Unvoiced frames are 0."""
    tau_max = min(int(sr / fmin) + 1, frame // 2)
    tau_min = max(2, int(sr / fmax))
    if tau_max <= tau_min or len(x) < frame:
        return np.zeros(0), np.zeros(0, dtype=bool), sr / hop

    width = frame - tau_max
    n_frames = 1 + (len(x) - frame) // hop
    f0 = np.zeros(n_frames)
    voiced = np.zeros(n_frames, dtype=bool)
    n_fft = 1 << (2 * frame - 1).bit_length()

    for i in range(n_frames):
        seg = x[i * hop: i * hop + frame]
        if np.max(np.abs(seg)) < 1e-4:
            continue

        power = seg ** 2
        cumulative = np.concatenate([[0.0], np.cumsum(power)])
        term1 = cumulative[width]
        term2 = cumulative[width: width + tau_max] - cumulative[:tau_max]

        spec = np.fft.rfft(seg[:width], n_fft)
        spec_full = np.fft.rfft(seg, n_fft)
        corr = np.fft.irfft(np.conj(spec) * spec_full, n_fft)[:tau_max]

        diff = term1 + term2 - 2 * corr
        diff[0] = 0.0

        running = np.cumsum(diff[1:])
        cmnd = np.ones(tau_max)
        nonzero = running > 0
        cmnd[1:][nonzero] = diff[1:][nonzero] * np.arange(1, tau_max)[nonzero] / running[nonzero]

        candidates = np.where(cmnd[tau_min:] < threshold)[0]
        if candidates.size:
            tau = int(candidates[0]) + tau_min
            # walk to the local minimum of this dip
            while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
        else:
            tau = int(np.argmin(cmnd[tau_min:])) + tau_min
            if cmnd[tau] > 0.6:          # no periodicity worth trusting
                continue

        # parabolic refinement around the minimum
        if 0 < tau < tau_max - 1:
            a, b, c = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
            denom = a - 2 * b + c
            if abs(denom) > 1e-12:
                tau = tau + 0.5 * (a - c) / denom

        if tau > 0:
            f0[i] = sr / tau
            voiced[i] = True

    return f0, voiced, sr / hop


def _hz_to_midi(hz: np.ndarray | float) -> np.ndarray | float:
    return 69 + 12 * np.log2(np.maximum(hz, 1e-9) / 440.0)


def notes_from_f0(f0: np.ndarray, voiced: np.ndarray, frame_rate: float,
                  envelope: np.ndarray | None = None, *,
                  min_duration: float = 0.055,
                  split_semitones: float = 0.7) -> list[Note]:
    """Segment a pitch track into notes.

    A note ends when the track goes unvoiced or the pitch moves far enough to
    be a different note. Each note's pitch is the median of its frames, which
    ignores the scoops and vibrato that would otherwise split it.
    """
    if f0.size == 0:
        return []
    midi = _hz_to_midi(f0)
    notes: list[Note] = []

    start = None
    buffer: list[float] = []

    def flush(end_frame: int) -> None:
        if start is None or not buffer:
            return
        duration = (end_frame - start) / frame_rate
        if duration < min_duration:
            return
        pitch = int(round(float(np.median(buffer))))
        velocity = 90
        if envelope is not None and envelope.size:
            lo = min(start, envelope.size - 1)
            hi = min(max(end_frame, lo + 1), envelope.size)
            velocity = int(np.clip(40 + 87 * float(np.max(envelope[lo:hi])), 25, 127))
        notes.append(Note(start / frame_rate, end_frame / frame_rate, pitch, velocity))

    for i, is_voiced in enumerate(voiced):
        if not is_voiced:
            flush(i)
            start, buffer = None, []
            continue
        if start is None:
            start, buffer = i, [midi[i]]
            continue
        if abs(midi[i] - np.median(buffer)) > split_semitones:
            flush(i)
            start, buffer = i, [midi[i]]
        else:
            buffer.append(midi[i])
    flush(len(voiced))

    return _merge_repeats(notes)


def _merge_repeats(notes: list[Note], gap: float = 0.035) -> list[Note]:
    """Join same-pitch notes separated by a dropout rather than a rest."""
    merged: list[Note] = []
    for note in notes:
        if merged and merged[-1].pitch == note.pitch and note.start - merged[-1].end <= gap:
            merged[-1].end = note.end
        else:
            merged.append(note)
    return merged


# ===========================================================================
# drums
# ===========================================================================

def _band_energy(seg: np.ndarray, sr: int, lo: float, hi: float) -> float:
    if seg.size < 32:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    freqs = np.fft.rfftfreq(seg.size, 1 / sr)
    band = (freqs >= lo) & (freqs < hi)
    return float(np.sum(spec[band] ** 2))


def classify_hit(x: np.ndarray, sr: int, onset: float,
                 next_onset: float | None) -> tuple[int, int]:
    """Return (GM drum note, velocity) for one hit."""
    start = int(onset * sr)
    window = x[start: start + int(0.060 * sr)]
    if window.size < 64:
        return SNARE, 80

    low = _band_energy(window, sr, 20, 130)
    low_mid = _band_energy(window, sr, 130, 400)
    mid = _band_energy(window, sr, 400, 3000)
    high = _band_energy(window, sr, 6000, min(16000, sr / 2 - 1))
    total = low + low_mid + mid + high + 1e-12

    peak = float(np.max(np.abs(window)))
    velocity = int(np.clip(45 + 82 * peak, 25, 127))

    # Decay length separates a crash/open hat from a closed hat.
    tail = x[start: start + int(0.30 * sr)]
    decay = 0.0
    if tail.size > int(0.05 * sr):
        head = float(np.sqrt(np.mean(tail[: int(0.03 * sr)] ** 2))) + 1e-9
        late = float(np.sqrt(np.mean(tail[int(0.15 * sr):] ** 2)))
        decay = late / head

    if high / total > 0.35:
        if decay > 0.35:
            return (CRASH if high / total > 0.6 else OPEN_HAT), velocity
        return CLOSED_HAT, velocity
    if low / total > 0.45:
        return KICK, velocity
    if low_mid / total > 0.5 and mid / total < 0.25:
        return TOM, velocity
    return SNARE, velocity


def transcribe_drums(x: np.ndarray, sr: int) -> tuple[list[Note], float]:
    env, frame_rate = onset_envelope(x, sr)
    bpm = estimate_tempo(env, frame_rate)
    onsets = pick_onsets(env, frame_rate)

    notes: list[Note] = []
    for i, onset in enumerate(onsets):
        nxt = float(onsets[i + 1]) if i + 1 < len(onsets) else None
        pitch, velocity = classify_hit(x, sr, float(onset), nxt)
        notes.append(Note(float(onset), float(onset) + 0.08, pitch, velocity))
    return notes, bpm


# ===========================================================================
# polyphonic (optional)
# ===========================================================================

def basic_pitch_available() -> bool:
    try:
        import basic_pitch  # noqa: F401
        return True
    except Exception:
        return False


def transcribe_polyphonic(path: Path) -> list[Note] | None:
    """Use Spotify's basic-pitch if the user installed it. Returns None if not."""
    try:
        from basic_pitch.inference import predict
        from basic_pitch import ICASSP_2022_MODEL_PATH
    except Exception:
        return None
    try:
        _model_output, _midi, note_events = predict(str(path), ICASSP_2022_MODEL_PATH)
    except Exception:
        return None
    notes = []
    for event in note_events:
        start, end, pitch, amplitude = event[0], event[1], int(event[2]), float(event[3])
        notes.append(Note(start, end, pitch, int(np.clip(30 + 97 * amplitude, 25, 127))))
    return notes


# ===========================================================================
# quantisation
# ===========================================================================

def quantize(notes: list[Note], bpm: float, division: int,
             strength: float = 1.0) -> list[Note]:
    """Pull note starts toward a grid. division 4 = 1/16 notes, 2 = 1/8, 1 = 1/4.

    strength 1.0 snaps hard; 0.5 halves the distance, which keeps some feel.
    """
    if division <= 0 or not notes:
        return notes
    step = (60.0 / bpm) / division
    out = []
    for note in notes:
        target = round(note.start / step) * step
        start = note.start + (target - note.start) * strength
        out.append(Note(max(0.0, start), max(start + step * 0.25, note.end + (start - note.start)),
                        note.pitch, note.velocity))
    return out


# ===========================================================================
# entry point
# ===========================================================================

def kind_from_name(name: str) -> StemKind:
    lowered = Path(name).stem.lower()
    for kind in ("drums", "bass", "vocals"):
        if kind in lowered:
            return kind  # type: ignore[return-value]
    return "other"


def transcribe(path: Path, *, kind: StemKind | None = None,
               polyphonic: bool = True, bpm: float | None = None) -> Transcription:
    """Transcribe one stem. Pass bpm to override the per-stem tempo estimate."""
    kind = kind or kind_from_name(path.name)

    if kind == "drums":
        x, sr = load_mono(path)
        notes, detected = transcribe_drums(x, sr)
        return Transcription(notes, bpm or detected, kind, False,
                             "onset + band classification")

    # tempo comes from the full-rate signal; pitch from a decimated copy
    full, sr_full = load_mono(path)
    env, frame_rate = onset_envelope(full, sr_full)
    if bpm is None:
        bpm = estimate_tempo(env, frame_rate)

    if kind == "other" and polyphonic:
        poly = transcribe_polyphonic(path)
        if poly is not None:
            return Transcription(poly, bpm, kind, True, "basic-pitch")

    ranges: dict[str, tuple[float, float]] = {
        "bass": (30.0, 400.0),
        "vocals": (70.0, 1100.0),
        "other": (60.0, 1600.0),
    }
    fmin, fmax = ranges[kind]

    x, sr = load_mono(path, PITCH_SR)
    f0, voiced, pitch_rate = yin_f0(x, sr, fmin=fmin, fmax=fmax)

    # Resample the onset envelope onto the pitch frame grid for velocities.
    env_on_pitch = None
    if env.size and f0.size:
        idx = np.linspace(0, env.size - 1, f0.size)
        env_on_pitch = np.interp(idx, np.arange(env.size), env)

    notes = notes_from_f0(f0, voiced, pitch_rate, env_on_pitch,
                          min_duration=0.08 if kind == "bass" else 0.055)
    method = "YIN monophonic"
    if kind == "other":
        method += " (install basic-pitch for polyphonic)"
    return Transcription(notes, bpm, kind, False, method)


DEFAULT_PROGRAMS = {"bass": 33, "vocals": 65, "other": 0}


def stem_to_midi(stem_path: Path, out_path: Path, *, quantize_division: int = 0,
                 quantize_strength: float = 1.0,
                 polyphonic: bool = True, bpm: float | None = None) -> dict:
    """Transcribe one stem file and write the MIDI beside it."""
    result = transcribe(stem_path, polyphonic=polyphonic, bpm=bpm)
    notes = result.notes
    if quantize_division:
        notes = quantize(notes, result.bpm, quantize_division, quantize_strength)

    write_midi(
        out_path, notes, result.bpm,
        track_name=stem_path.stem,
        drums=(result.kind == "drums"),
        program=DEFAULT_PROGRAMS.get(result.kind),
    )

    pitches = [n.pitch for n in notes]
    return {
        "stem": stem_path.name,
        "midi": out_path.name,
        "kind": result.kind,
        "notes": len(notes),
        "bpm": result.bpm,
        "polyphonic": result.polyphonic,
        "method": result.method,
        "lowest": min(pitches) if pitches else None,
        "highest": max(pitches) if pitches else None,
        "quantized": bool(quantize_division),
    }


def session_tempo(stem_paths: list[Path]) -> float | None:
    """Tempo for a whole set of stems, taken from drums if there are any.

    Percussion gives a far cleaner onset envelope than a legato bass or vocal,
    so one tempo from the drums beats four disagreeing estimates — and it means
    the parts land on the same tempo map when you import them together.
    """
    for path in stem_paths:
        if kind_from_name(path.name) == "drums":
            x, sr = load_mono(path)
            env, frame_rate = onset_envelope(x, sr)
            return estimate_tempo(env, frame_rate)
    return None
