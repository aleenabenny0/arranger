"""Posteriors -> note events: a numpy-only port of Basic Pitch's note creation.

Ported from ``basic_pitch/note_creation.py`` and ``basic_pitch/constants.py``
(Basic Pitch 0.4.0, Copyright 2022 Spotify AB, Apache License 2.0,
https://github.com/spotify/basic-pitch). The upstream module imports scipy,
librosa, pretty_midi, mir_eval and resampy at module scope, which is why it is
re-implemented here rather than imported. What is ported:

* ``get_infered_onsets``             -> :func:`infer_onsets`
* ``scipy.signal.argrelmax(axis=0)`` -> :func:`relative_maxima` (order 1,
  strict, clipped edges: the first and last frame are never peaks and plateaus
  are never peaks, the same answers scipy gives)
* ``model_frames_to_time``           -> :func:`model_frames_to_time`
* ``output_to_notes_polyphonic``     -> :func:`output_to_notes_polyphonic`

Deliberate differences from upstream, none of which change the detected notes
for the same inputs:

* The inputs are never mutated (upstream zeroes the caller's arrays).
* Pitch limits are inclusive MIDI numbers instead of frequencies in Hz.
* ``infer_onsets`` does not divide by zero on an all-silent input.
* Onset inference and peak picking run over a few pitch columns at a time (in
  float64, as upstream), so a 12 minute file never needs a float64 copy of its
  whole posterior matrix.
* The melodia pass finds the global maximum through a per-block cache instead
  of a full ``argmax`` per iteration. Ties resolve to the same cell upstream
  picks (first in row-major order), so the notes are identical; it is only
  faster on long files.
* Each note records whether it came from an onset peak or from the melodia
  pass, because the confidence score wants to know.

Pitch bends are not implemented.

This module imports numpy at module scope. It is only ever imported lazily
from :mod:`arranger.adapters.audio`, after the dependency check has passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

# basic_pitch/constants.py
FFT_HOP = 256
AUDIO_SAMPLE_RATE = 22050
AUDIO_WINDOW_LENGTH = 2
ANNOTATIONS_FPS = AUDIO_SAMPLE_RATE // FFT_HOP            # 86
ANNOT_N_FRAMES = ANNOTATIONS_FPS * AUDIO_WINDOW_LENGTH    # 172
AUDIO_N_SAMPLES = AUDIO_SAMPLE_RATE * AUDIO_WINDOW_LENGTH - FFT_HOP  # 43844
N_PITCHES = 88

# basic_pitch/note_creation.py
MIDI_OFFSET = 21
MAX_FREQ_IDX = 87
ENERGY_TOLERANCE_FRAMES = 11

_MELODIA_BLOCK = 256  # frames per block in the max cache


@dataclass(frozen=True)
class FrameNote:
    """A note in frame units. `end` is exclusive, as upstream's tuples are."""

    start: int
    end: int
    pitch: int          # MIDI number
    amplitude: float    # mean note posterior over [start, end)
    from_onset: bool    # False: recovered by the melodia pass, no onset peak


def model_frames_to_time(n_frames: int) -> np.ndarray:
    """Frame index -> seconds, exactly as upstream computes it.

    ``librosa.frames_to_time(i, sr, hop_length)`` is ``i * hop / sr``. The
    per-window correction exists because each 43844-sample window yields 172
    frames (not 171.27), so frame times drift by about 8.5 ms per window
    unless pulled back. The 0.0018 is upstream's own alignment constant.
    """
    idx = np.arange(n_frames)
    original_times = idx * (FFT_HOP / AUDIO_SAMPLE_RATE)
    window_numbers = np.floor(idx / ANNOT_N_FRAMES)
    window_offset = (FFT_HOP / AUDIO_SAMPLE_RATE) * (
        ANNOT_N_FRAMES - (AUDIO_N_SAMPLES / FFT_HOP)
    ) + 0.0018
    return original_times - window_offset * window_numbers


def frame_differences(frames: np.ndarray, n_diff: int = 2) -> np.ndarray:
    """Positive part of min_n(frames[t] - frames[t-n]), n = 1..n_diff, in float64.

    Upstream pads with float64 zeros, so its differences are float64; this
    matches that rather than staying in float32. Columns are independent, so
    this can be called on a block of pitch columns.
    """
    x = frames.astype(np.float64)
    out = None
    for n in range(1, n_diff + 1):
        diff = x.copy()                 # rows before n compare against silence
        diff[n:] -= x[:-n]
        out = diff if out is None else np.minimum(out, diff, out=out)
    assert out is not None
    np.maximum(out, 0.0, out=out)
    out[:n_diff, :] = 0
    return out


def infer_onsets(
    onsets: np.ndarray,
    frames: np.ndarray,
    n_diff: int = 2,
    *,
    onset_max: float | None = None,
    diff_max: float | None = None,
) -> np.ndarray:
    """Add onsets where the frame posterior jumps, rescaled to the onset range.

    `onset_max` and `diff_max` are the maxima over the *whole* matrices. Pass
    them when calling this on a block of columns; leave them out for a full
    matrix. Returns float64.
    """
    frame_diff = frame_differences(frames, n_diff)
    if onset_max is None:
        onset_max = float(np.max(onsets)) if onsets.size else 0.0
    if diff_max is None:
        diff_max = float(np.max(frame_diff)) if frame_diff.size else 0.0
    onsets = onsets.astype(np.float64)
    if diff_max <= 0.0:                 # upstream divides by zero here
        return onsets
    frame_diff = onset_max * frame_diff / diff_max
    return np.maximum(onsets, frame_diff)


def relative_maxima(x: np.ndarray) -> np.ndarray:
    """Boolean mask of strict local maxima along axis 0.

    Same result as ``scipy.signal.argrelmax(x, axis=0)`` with its defaults
    (``order=1, mode="clip"``): a cell is a peak when it is strictly greater
    than both time-neighbours, and the first and last rows cannot be peaks.
    """
    mask = np.zeros(x.shape, dtype=bool)
    if x.shape[0] >= 3:
        mid = x[1:-1]
        mask[1:-1] = (mid > x[:-2]) & (mid > x[2:])
    return mask


def onset_peaks(
    onsets: np.ndarray,
    frames: np.ndarray,
    onset_thresh: float,
    *,
    lo_bin: int = 0,
    hi_bin: int = N_PITCHES - 1,
    use_inferred_onsets: bool = True,
    block_cols: int = 8,
) -> tuple[list[int], list[int]]:
    """(frame, bin) of every onset peak at or above the threshold, latest first.

    The order is upstream order: ``np.where`` over the whole matrix (time, then
    pitch), reversed. The work is done a few pitch columns at a time in
    float64, which gives upstream numbers without ever holding a float64 copy
    of the whole matrix; only bins in [lo_bin, hi_bin] are looked at.
    """
    blocks = [(c, min(c + block_cols, hi_bin + 1)) for c in range(lo_bin, hi_bin + 1, block_cols)]
    onset_max = diff_max = 0.0
    if use_inferred_onsets and blocks:
        onset_max = float(np.max(onsets[:, lo_bin:hi_bin + 1])) if onsets.shape[0] else 0.0
        for c0, c1 in blocks:
            if frames.shape[0]:
                diff_max = max(diff_max, float(np.max(frame_differences(frames[:, c0:c1]))))

    found: list[tuple[int, int]] = []
    for c0, c1 in blocks:
        if use_inferred_onsets:
            block = infer_onsets(onsets[:, c0:c1], frames[:, c0:c1], onset_max=onset_max, diff_max=diff_max)
        else:
            block = onsets[:, c0:c1].astype(np.float64)
        rows, cols = np.where(relative_maxima(block) & (block >= onset_thresh))
        found.extend(zip(rows.tolist(), (cols + c0).tolist(), strict=True))
    found.sort(reverse=True)
    return [t for t, _ in found], [f for _, f in found]


class _BlockMax:
    """Global argmax of a matrix that only ever has cells zeroed.

    Keeps the maximum of each block of rows. After rows are changed, only the
    blocks they fall in are recomputed. `argmax` returns the first maximal
    cell in row-major order, which is what ``np.argmax`` on the whole matrix
    returns, so swapping this in cannot change which note is picked next.
    """

    def __init__(self, mat: np.ndarray, block: int = _MELODIA_BLOCK):
        self.mat = mat
        self.block = block
        n_blocks = (mat.shape[0] + block - 1) // block
        self.maxes = np.array(
            [mat[b * block:(b + 1) * block].max() for b in range(n_blocks)], dtype=mat.dtype
        )

    def refresh(self, row_lo: int, row_hi: int) -> None:
        """Recompute the blocks covering rows [row_lo, row_hi]."""
        row_lo = max(0, row_lo)
        row_hi = min(self.mat.shape[0] - 1, row_hi)
        for b in range(row_lo // self.block, row_hi // self.block + 1):
            self.maxes[b] = self.mat[b * self.block:(b + 1) * self.block].max()

    def max(self) -> float:
        return float(self.maxes.max()) if self.maxes.size else 0.0

    def argmax(self) -> tuple[int, int]:
        b = int(np.argmax(self.maxes))
        sub = self.mat[b * self.block:(b + 1) * self.block]
        row, col = np.unravel_index(int(np.argmax(sub)), sub.shape)
        return b * self.block + int(row), int(col)


def output_to_notes_polyphonic(
    frames: np.ndarray,
    onsets: np.ndarray,
    *,
    onset_thresh: float,
    frame_thresh: float,
    min_note_len: int,
    min_pitch: int = 21,
    max_pitch: int = 108,
    use_inferred_onsets: bool = True,
    melodia_trick: bool = True,
    energy_tol: int = ENERGY_TOLERANCE_FRAMES,
    checkpoint: Callable[[], None] | None = None,
) -> list[FrameNote]:
    """Decode note and onset posteriors, shape (n_frames, 88), into note events.

    `checkpoint`, if given, is called every so often and may raise to abandon
    the work (that is how cancellation reaches these loops).
    """
    if frames.ndim != 2 or frames.shape != onsets.shape or frames.shape[1] != N_PITCHES:
        raise ValueError(
            f"expected two (n_frames, {N_PITCHES}) matrices, got {frames.shape} and {onsets.shape}"
        )

    n_frames = frames.shape[0]
    frame_thresh = float(frame_thresh)
    lo_bin = max(0, int(min_pitch) - MIDI_OFFSET)
    hi_bin = min(N_PITCHES - 1, int(max_pitch) - MIDI_OFFSET)
    if lo_bin > hi_bin or n_frames == 0:
        return []

    starts, bins = onset_peaks(
        onsets, frames, float(onset_thresh),
        lo_bin=lo_bin, hi_bin=hi_bin, use_inferred_onsets=use_inferred_onsets,
    )

    # Upstream zeroes out-of-range pitches in `frames` itself; zeroing them in
    # the working copy is equivalent, because amplitudes are only ever read at
    # in-range pitches. Thresholds are compared as Python floats so that a
    # float32 posterior meets a float64 threshold exactly as it does upstream.
    remaining_energy = frames.astype(np.float32, copy=True)
    remaining_energy[:, :lo_bin] = 0
    remaining_energy[:, hi_bin + 1:] = 0
    notes: list[FrameNote] = []

    for count, (start, freq_idx) in enumerate(zip(starts, bins, strict=True)):
        if checkpoint is not None and count % 512 == 0:
            checkpoint()
        if start >= n_frames - 1:
            continue

        # Walk forward until the posterior has stayed under the threshold for
        # `energy_tol` frames in a row, then step back to the last frame above.
        column = remaining_energy[:, freq_idx]
        i = start + 1
        k = 0
        while i < n_frames - 1 and k < energy_tol:
            if float(column[i]) < frame_thresh:
                k += 1
            else:
                k = 0
            i += 1
        i -= k

        if i - start <= min_note_len:
            continue

        remaining_energy[start:i, freq_idx] = 0
        if freq_idx < MAX_FREQ_IDX:
            remaining_energy[start:i, freq_idx + 1] = 0
        if freq_idx > 0:
            remaining_energy[start:i, freq_idx - 1] = 0

        notes.append(
            FrameNote(start, i, freq_idx + MIDI_OFFSET, float(np.mean(frames[start:i, freq_idx])), True)
        )

    if melodia_trick:
        cache = _BlockMax(remaining_energy)
        count = 0
        while cache.max() > frame_thresh:
            count += 1
            if checkpoint is not None and count % 64 == 0:
                checkpoint()
            i_mid, freq_idx = cache.argmax()
            remaining_energy[i_mid, freq_idx] = 0

            # forward pass
            i = i_mid + 1
            k = 0
            while i < n_frames - 1 and k < energy_tol:
                if float(remaining_energy[i, freq_idx]) < frame_thresh:
                    k += 1
                else:
                    k = 0
                remaining_energy[i, freq_idx] = 0
                if freq_idx < MAX_FREQ_IDX:
                    remaining_energy[i, freq_idx + 1] = 0
                if freq_idx > 0:
                    remaining_energy[i, freq_idx - 1] = 0
                i += 1
            i_end = i - 1 - k
            touched_hi = i

            # backward pass
            i = i_mid - 1
            k = 0
            while i > 0 and k < energy_tol:
                if float(remaining_energy[i, freq_idx]) < frame_thresh:
                    k += 1
                else:
                    k = 0
                remaining_energy[i, freq_idx] = 0
                if freq_idx < MAX_FREQ_IDX:
                    remaining_energy[i, freq_idx + 1] = 0
                if freq_idx > 0:
                    remaining_energy[i, freq_idx - 1] = 0
                i -= 1
            i_start = i + 1 + k
            cache.refresh(i, touched_hi)

            if i_end - i_start <= min_note_len:
                continue
            notes.append(
                FrameNote(
                    i_start,
                    i_end,
                    freq_idx + MIDI_OFFSET,
                    float(np.mean(frames[i_start:i_end, freq_idx])),
                    False,
                )
            )

    return notes
