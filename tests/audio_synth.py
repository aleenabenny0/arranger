"""Deterministic additive synthesiser for the audio-transcription tests.

No external assets: every test clip is rendered here from a list of
``(pitch, onset_s, duration_s, velocity)`` tuples, so the ground truth is
known exactly. The timbre is a crude struck-string: a handful of harmonics
with 1/k-ish amplitudes, higher partials decaying faster, a short raised-cosine
attack and an exponential decay, plus a short release so notes do not click.

This is *not* a piano. It is a clean, dry, perfectly tuned source, which is
the easiest case a transcriber will ever see. Accuracy measured on it is an
upper bound, not an estimate of real-recording accuracy.

The default timbre has a full harmonic series (16 partials, amplitude
1/k^1.5). That matters: with the series cut off after 4-6 partials, which no
acoustic instrument does, the model reports ghost notes at the octave or at the
top partial. `n_harmonics` and `rolloff` are exposed so a test can provoke that
on purpose and check the ghosts come back with low confidence.

Requires numpy (and soundfile for the ``*_bytes`` helpers); both are part of
the optional ``audio`` extra, so this module is only imported by tests that
have already checked ``audio_support()``.
"""

from __future__ import annotations

import io
from typing import Iterable, Sequence

import numpy as np

NoteSpec = tuple[int, float, float, int]  # (midi pitch, onset s, duration s, velocity 1-127)

DEFAULT_SR = 22050


def midi_to_hz(pitch: float) -> float:
    return 440.0 * 2.0 ** ((pitch - 69) / 12.0)


def synth(
    notes: Iterable[NoteSpec],
    *,
    sr: int = DEFAULT_SR,
    tail: float = 0.5,
    lead: float = 0.0,
    n_harmonics: int = 16,
    rolloff: float = 1.5,
    decay_s: float = 1.2,
    attack_s: float = 0.006,
    release_s: float = 0.04,
    peak: float = 0.5,
) -> np.ndarray:
    """Render notes to mono float32 at `sr`. Output peak is normalised to `peak`.

    `lead` shifts every onset later by that many seconds (the ground truth the
    caller compares against must be shifted the same way).
    """
    notes = list(notes)
    if not notes:
        raise ValueError("nothing to synthesise")
    end = max(on + dur for _, on, dur, _ in notes) + lead + release_s + tail
    out = np.zeros(int(np.ceil(end * sr)), dtype=np.float64)
    nyquist = sr / 2.0
    for pitch, onset, dur, vel in notes:
        f0 = midi_to_hz(pitch)
        n = int(round((dur + release_s) * sr))
        t = np.arange(n, dtype=np.float64) / sr
        tone = np.zeros(n, dtype=np.float64)
        for k in range(1, n_harmonics + 1):
            fk = f0 * k
            if fk >= nyquist * 0.95:
                break
            # Higher partials are quieter and die sooner, like a struck string.
            amp = 1.0 / (k ** rolloff)
            tone += amp * np.exp(-t * k ** 0.5 / decay_s) * np.sin(2 * np.pi * fk * t)
        env = np.ones(n, dtype=np.float64)
        a = max(1, int(round(attack_s * sr)))
        env[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / a)
        r = max(1, int(round(release_s * sr)))
        env[-r:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(r) / r)
        start = int(round((onset + lead) * sr))
        out[start : start + n] += (vel / 127.0) * tone * env
    top = float(np.max(np.abs(out)))
    if top > 0:
        out *= peak / top
    return out.astype(np.float32)


def white_noise(seconds: float, *, sr: int = DEFAULT_SR, rms: float = 0.1, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * sr)) * rms).astype(np.float32)


def add_noise(signal: np.ndarray, snr_db: float, *, seed: int = 0) -> np.ndarray:
    """Mix white noise under `signal` at the given signal-to-noise ratio."""
    rng = np.random.default_rng(seed)
    sig_rms = float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))
    noise_rms = sig_rms / (10.0 ** (snr_db / 20.0))
    noisy = signal + rng.standard_normal(signal.shape[0]).astype(np.float32) * noise_rms
    top = float(np.max(np.abs(noisy)))
    if top > 0.99:
        noisy = noisy * (0.99 / top)
    return noisy.astype(np.float32)


def to_bytes(
    samples: np.ndarray, *, sr: int = DEFAULT_SR, fmt: str = "WAV", subtype: str | None = "PCM_16"
) -> bytes:
    """Encode mono `(n,)` or multichannel `(n, channels)` samples with soundfile."""
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, sr, format=fmt, subtype=subtype)
    return buf.getvalue()


def wav_bytes(notes: Sequence[NoteSpec], *, sr: int = DEFAULT_SR, **kwargs) -> bytes:
    return to_bytes(synth(notes, sr=sr, **kwargs), sr=sr)
