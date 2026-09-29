"""Audio adapter: a recording in, a `Score` of transcribed notes out.

This is automatic music transcription with Spotify's Basic Pitch model
(ICASSP 2022), run through ONNX Runtime on the CPU. The result is a *draft*:
real recordings come back with wrong, missing and extra notes, and every note
carries a `confidence` so a person can find and fix the doubtful ones before
anything is arranged from it. Nothing here touches an `ArrangementPlan`.

Attribution
-----------
The windowing in `_run_model` and the note creation in `_basic_pitch_notes`
are ports of ``basic_pitch/inference.py`` and ``basic_pitch/note_creation.py``
from Basic Pitch 0.4.0 (Copyright 2022 Spotify AB, Apache License 2.0,
https://github.com/spotify/basic-pitch). The model weights are the
``icassp_2022/nmp.onnx`` file shipped inside the ``basic-pitch`` wheel, used
unmodified. The `basic_pitch` Python package itself is never imported: on
Python >= 3.12 its TensorFlow dependency has no wheels, and its modules import
TensorFlow/librosa/scipy at the top. Only the model file is read.

Paper: Bittner, Bosch, Rubinstein, Meseguer-Brocal, Ewert, "A Lightweight
Instrument-Agnostic Model for Polyphonic Note Transcription and Multipitch
Estimation", ICASSP 2022.

Rules this module keeps
-----------------------
* Third-party imports (numpy, soundfile, soxr, onnxruntime) are lazy, so
  ``import arranger.adapters.audio`` works without the ``audio`` extra.
  `audio_support()` says what is missing.
* No canned output, no fallback. If the model cannot run, `AudioUnavailable`
  names the missing prerequisite.
* Every upload is from a stranger: size is checked before the container is
  opened, the declared duration before any sample is decoded, and the decoded
  duration again while decoding, because headers lie.

Two documented departures from upstream inference, see
``docs/audio-transcription.md``: the unwrapped output is trimmed to the number
of frames that really covers the audio (upstream trims to ``seconds * 86`` and
so drops the last ~0.7% of the recording), and the contour head is not
evaluated into notes (no pitch bends).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import math
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..ir import Note, Score, TrackInfo
from ..limits import (
    DEFAULT_LIMITS,
    EmptyScore,
    ImportLimits,
    LimitExceeded,
    MalformedFile,
    ScoreImportError,
    UnsupportedFormat,
)
from ..timeline import Timeline

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

__all__ = [
    "AudioInfo",
    "AudioSupport",
    "AudioUnavailable",
    "TranscriptionCancelled",
    "TranscriptionOptions",
    "TranscriptionResult",
    "audio_support",
    "probe_audio",
    "transcribe_audio",
]

MODEL_NAME = "basic-pitch-icassp-2022-onnx"
# sha256 of nmp.onnx as shipped in the basic-pitch 0.4.0 wheel. A different
# file still runs (its hash is reported), but the result carries a warning,
# because the accuracy figures in the docs were measured with this one.
KNOWN_MODEL_SHA256 = "2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec"
# Deployments that ship only the model file can point at it directly.
MODEL_PATH_ENV = "ARRANGER_BASIC_PITCH_ONNX"

# basic_pitch/constants.py and basic_pitch/inference.py
AUDIO_SAMPLE_RATE = 22050
FFT_HOP = 256
AUDIO_N_SAMPLES = AUDIO_SAMPLE_RATE * 2 - FFT_HOP      # 43844
N_OVERLAPPING_FRAMES = 30
OVERLAP_LEN = N_OVERLAPPING_FRAMES * FFT_HOP           # 7680 samples
HOP_SIZE = AUDIO_N_SAMPLES - OVERLAP_LEN               # 36164 samples
WINDOW_FRAMES = 172                                    # frames the model emits per window
KEPT_FRAMES = WINDOW_FRAMES - N_OVERLAPPING_FRAMES     # 142 survive the unwrap
N_PITCHES = 88
N_CONTOUR_BINS = 264

# Confidence. See `_note_confidence`.
CONFIDENCE_NOTE_WEIGHT = 0.6
CONFIDENCE_ONSET_WEIGHT = 0.4
LOW_CONFIDENCE = 0.5
DENSE_POLYPHONY = 6

# Tempo. See `_estimate_tempo`.
TEMPO_MIN_BPM = 50.0
TEMPO_MAX_BPM = 200.0
TEMPO_MIN_CONFIDENCE = 0.5
TEMPO_MIN_SECONDS = 4.0
TEMPO_MIN_ONSETS = 6
FALLBACK_BPM = 120.0

# Input hygiene.
_MIN_SAMPLE_RATE = 4_000
_MAX_SAMPLE_RATE = 384_000
_MAX_CHANNELS = 16
_DECODE_BLOCK_SAMPLES = 1 << 18          # samples (frames x channels) per read
_CLIP_LEVEL = 0.999
_CLIP_FRACTION = 1e-3
_LOW_RMS = 0.005                         # about -46 dBFS
_SAMPLE_CEILING = 8.0                    # float files may exceed 1.0; not by this much

# Containers accepted, by libsndfile major-format name. Decided by content.
# (RF64 and W64 exist for files over 4 GB; with a 40 MiB cap they have no use.)
_ACCEPTED_FORMATS = ("WAV", "WAVEX", "FLAC", "OGG", "MP3", "AIFF")
_REPORTED_FORMATS = ("WAV", "FLAC", "OGG", "MP3", "AIFF")

# Leading bytes of things people upload by mistake. Anything listed here is
# "not audio we read" rather than "broken audio".
_FOREIGN_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "a PNG image"),
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"GIF8", "a GIF image"),
    (b"%PDF", "a PDF document"),
    (b"PK\x03\x04", "a ZIP archive (or .mxl/.docx)"),
    (b"MThd", "a MIDI file - import it as MIDI, not as audio"),
    (b"<?xml", "an XML document"),
    (b"\x1aE\xdf\xa3", "a Matroska/WebM file"),
)


# --------------------------------------------------------------------------
# Errors and result types
# --------------------------------------------------------------------------


class AudioUnavailable(ScoreImportError):
    """Transcription cannot run here. The message names what is missing."""

    code = "audio_unavailable"


class TranscriptionCancelled(Exception):
    """`should_cancel()` returned true. Not an import error: the file may be fine."""

    code = "cancelled"


@dataclass(frozen=True)
class AudioSupport:
    available: bool
    missing: list[str]
    model_path: str | None
    detail: str


@dataclass(frozen=True)
class AudioInfo:
    """What the container's header says. Nothing has been decoded yet."""

    format: str            # libsndfile major format: "WAV", "FLAC", "OGG", "MP3", ...
    subtype: str           # e.g. "PCM_16", "VORBIS", "MPEG_LAYER_III"
    channels: int
    sample_rate: int
    frames: int            # as declared; re-checked during decoding
    duration_seconds: float
    byte_size: int
    warnings: tuple[str, ...] = ()   # things the container itself gives away


@dataclass(frozen=True)
class TranscriptionOptions:
    onset_threshold: float = 0.5
    frame_threshold: float = 0.3
    min_note_ms: float = 58.0
    min_pitch: int = 21
    max_pitch: int = 108
    melodia_trick: bool = True
    estimate_tempo: bool = True


@dataclass
class TranscriptionResult:
    score: Score
    duration_seconds: float
    sample_rate: int                   # of the file as uploaded
    overall_confidence: float          # duration-weighted mean of note confidence
    low_confidence_note_ids: list[str]
    tempo_bpm: float | None            # None: no tempo could be trusted
    tempo_confidence: float | None
    warnings: list[str]
    model: str
    model_sha256: str
    max_polyphony: int = 0
    info: AudioInfo | None = field(default=None, repr=False)


# --------------------------------------------------------------------------
# Capability check
# --------------------------------------------------------------------------

# (import name, name to put in `missing` / pip name)
_REQUIRED_MODULES = (
    ("numpy", "numpy"),
    ("soundfile", "soundfile"),
    ("soxr", "soxr"),
    ("onnxruntime", "onnxruntime"),
)


def _find_model_path() -> tuple[Path | None, str]:
    """Locate nmp.onnx without importing `basic_pitch`. Returns (path, why-not)."""
    override = os.environ.get(MODEL_PATH_ENV, "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path, ""
        return None, f"{MODEL_PATH_ENV}={override!r} is not a file"
    try:
        spec = importlib.util.find_spec("basic_pitch")
    except (ImportError, ValueError):
        spec = None
    if spec is None or not spec.submodule_search_locations:
        return None, "the basic-pitch package is not installed"
    for root in spec.submodule_search_locations:
        path = Path(root) / "saved_models" / "icassp_2022" / "nmp.onnx"
        if path.is_file():
            return path, ""
    return None, "basic-pitch is installed but saved_models/icassp_2022/nmp.onnx is not in it"


def audio_support() -> AudioSupport:
    """Can this process transcribe audio, and if not, exactly what is missing."""
    missing: list[str] = []
    versions: list[str] = []
    modules: dict[str, Any] = {}
    for import_name, label in _REQUIRED_MODULES:
        try:
            module = importlib.import_module(import_name)
        except Exception as exc:  # ImportError, or a broken native library
            missing.append(label)
            versions.append(f"{label}: not importable ({type(exc).__name__})")
            continue
        modules[import_name] = module
        versions.append(f"{label} {getattr(module, '__version__', '?')}")

    model_path, why = _find_model_path()
    if model_path is None:
        missing.append(f"basic-pitch model file nmp.onnx ({why})")

    parts = ["; ".join(versions)]
    sf = modules.get("soundfile")
    if sf is not None:
        try:
            formats = sf.available_formats()
            found = [name for name in _REPORTED_FORMATS if name in formats]
            absent = [name for name in _REPORTED_FORMATS if name not in formats]
            text = f"libsndfile {getattr(sf, '__libsndfile_version__', '?')} decodes: {', '.join(found)}"
            if absent:
                text += f" (not in this build: {', '.join(absent)})"
            parts.append(text)
        except Exception as exc:
            parts.append(f"libsndfile format list unavailable ({type(exc).__name__})")
    parts.append(f"model: {model_path}" if model_path else f"model: not found ({why})")
    if missing:
        parts.append(
            'install with: pip install "numpy>=2.0" "onnxruntime>=1.20" "soundfile>=0.13" '
            '"soxr>=0.5" && pip install --no-deps "basic-pitch>=0.4"'
        )
    return AudioSupport(
        available=not missing,
        missing=missing,
        model_path=str(model_path) if model_path else None,
        detail=". ".join(parts),
    )


def _require(import_name: str) -> Any:
    try:
        return importlib.import_module(import_name)
    except Exception as exc:
        raise AudioUnavailable(
            f"Audio transcription is not available on this server: the '{import_name}' "
            "package is not installed.",
            detail=f"import {import_name} failed: {exc!r}",
        ) from exc


# --------------------------------------------------------------------------
# Probe: validate the container without decoding samples
# --------------------------------------------------------------------------


def _foreign_kind(data: bytes) -> str | None:
    head = data[:16]
    for magic, label in _FOREIGN_SIGNATURES:
        if head.startswith(magic):
            return label
    if head[4:8] == b"ftyp":
        return "an MP4/M4A/AAC file (convert it to WAV, FLAC, OGG or MP3)"
    return None


def _check_riff_not_truncated(data: bytes) -> None:
    """A WAV whose `data` chunk promises more bytes than were uploaded is cut short.

    libsndfile quietly shortens such a file and carries on, which would hand
    the user half a transcription with no explanation. Streaming writers that
    never patched the header (size 0 or 0xFFFFFFFF) are let through.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if chunk_id == b"data":
            available = len(data) - (pos + 8)
            if size not in (0, 0xFFFFFFFF) and size > available:
                raise MalformedFile(
                    "This WAV file is cut short: it ends before its audio data does. "
                    "Try uploading it again.",
                    detail=f"data chunk declares {size} bytes, {available} present",
                )
            return
        pos += 8 + size + (size & 1)


def _check_aiff_not_truncated(data: bytes) -> None:
    """The same check for AIFF/AIFC, whose sound lives in a big-endian `SSND` chunk."""
    if len(data) < 12 or data[:4] != b"FORM" or data[8:12] not in (b"AIFF", b"AIFC"):
        return
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        if chunk_id == b"SSND":
            available = len(data) - (pos + 8)
            if size > available:
                raise MalformedFile(
                    "This AIFF file is cut short: it ends before its audio data does. "
                    "Try uploading it again.",
                    detail=f"SSND chunk declares {size} bytes, {available} present",
                )
            return
        pos += 8 + size + (size & 1)


def _ogg_has_end_of_stream(data: bytes) -> bool:
    """True when the last whole Ogg page carries the end-of-stream flag.

    A cut-off Ogg file still opens, and libsndfile reports the shortened length
    as if it were the real one, so nothing downstream can tell. The container
    can: an encoder that finished sets bit 0x04 in the header of its last page.
    """
    pos = data.rfind(b"OggS")
    while pos >= 0:
        if pos + 27 <= len(data) and data[pos + 4] == 0:   # a whole page header, version 0
            return bool(data[pos + 5] & 0x04)
        pos = data.rfind(b"OggS", 0, pos)
    return False


def probe_audio(
    data: bytes, *, filename: str | None, limits: ImportLimits = DEFAULT_LIMITS
) -> AudioInfo:
    """Validate an upload by its content and read its header. Decodes no samples.

    The filename is only ever used in messages. Order matters: byte size, then
    container, then declared duration - each before the more expensive step.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MalformedFile("The upload is not a file.", detail=f"got {type(data).__name__}")
    size = len(data)
    if size > limits.max_audio_bytes:
        raise LimitExceeded(
            f"This audio file is {size / 2**20:.1f} MB; the limit is "
            f"{limits.max_audio_bytes / 2**20:.0f} MB.",
            detail=f"{size} > max_audio_bytes {limits.max_audio_bytes}",
        )
    label = _safe_label(filename)
    if size == 0:
        raise MalformedFile(f"{label} is empty.")
    data = bytes(data)

    foreign = _foreign_kind(data)
    if foreign is not None:
        raise UnsupportedFormat(
            f"{label} is {foreign}, not an audio file this importer reads "
            "(WAV, FLAC, OGG, MP3, AIFF).",
            detail=f"leading bytes {data[:12]!r}",
        )

    sf = _require("soundfile")
    try:
        handle = sf.SoundFile(io.BytesIO(data))
    except Exception as exc:
        raise MalformedFile(
            f"{label} could not be read as audio. Supported: WAV, FLAC, OGG, MP3, AIFF.",
            detail=f"libsndfile: {exc}",
        ) from exc
    try:
        fmt, subtype = str(handle.format), str(handle.subtype)
        channels, sample_rate, frames = int(handle.channels), int(handle.samplerate), int(handle.frames)
    finally:
        handle.close()

    if fmt not in _ACCEPTED_FORMATS:
        raise UnsupportedFormat(
            f"{label} is a {fmt} file; this importer reads WAV, FLAC, OGG, MP3 and AIFF.",
            detail=f"libsndfile format {fmt}/{subtype}",
        )
    if not 1 <= channels <= _MAX_CHANNELS:
        raise UnsupportedFormat(
            f"{label} has {channels} channels; at most {_MAX_CHANNELS} are supported."
        )
    if not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE:
        raise UnsupportedFormat(
            f"{label} has a sample rate of {sample_rate} Hz, which is outside "
            f"{_MIN_SAMPLE_RATE}-{_MAX_SAMPLE_RATE} Hz."
        )
    if frames < 0:
        raise MalformedFile(f"{label} declares a negative length.", detail=f"frames={frames}")
    duration = frames / sample_rate
    if duration > limits.max_audio_seconds:
        raise LimitExceeded(
            f"This recording is longer than the {_clock(limits.max_audio_seconds)} limit "
            f"(it runs {_clock(duration)}).",
            detail=f"declared {frames} frames at {sample_rate} Hz",
        )
    found: list[str] = []
    if fmt in ("WAV", "WAVEX"):
        _check_riff_not_truncated(data)
    elif fmt == "AIFF":
        _check_aiff_not_truncated(data)
    elif fmt == "OGG" and not _ogg_has_end_of_stream(data):
        # Not an error: some stream captures never write the marker. But an
        # interrupted upload looks exactly like this, and nothing else will say so.
        found.append(
            "This OGG file has no end-of-stream marker, so it was probably cut short: "
            "the end of the recording may be missing."
        )
    return AudioInfo(fmt, subtype, channels, sample_rate, frames, duration, size, tuple(found))


# --------------------------------------------------------------------------
# Decode: blocks -> mono -> 22050 Hz
# --------------------------------------------------------------------------


@dataclass
class _Decoded:
    samples: "np.ndarray"      # mono float32 at 22050 Hz
    source_frames: int
    clipped_fraction: float
    rms: float
    truncated: bool


def _decode(
    data: bytes,
    info: AudioInfo,
    limits: ImportLimits,
    tick: Callable[[float], None],
) -> _Decoded:
    np = _require("numpy")
    sf = _require("soundfile")
    soxr = _require("soxr")

    max_frames = int(limits.max_audio_seconds * info.sample_rate)
    block_frames = max(1024, _DECODE_BLOCK_SAMPLES // info.channels)
    resampler = (
        soxr.ResampleStream(info.sample_rate, AUDIO_SAMPLE_RATE, 1, dtype="float32", quality="HQ")
        if info.sample_rate != AUDIO_SAMPLE_RATE
        else None
    )

    # One output buffer, sized from the header and grown only if the header
    # understated the length. Collecting chunks and concatenating them would
    # hold the whole recording twice.
    ratio = AUDIO_SAMPLE_RATE / info.sample_rate
    out = np.empty(int(min(max(info.frames, 0), max_frames) * ratio) + 8192, dtype=np.float32)
    written = 0

    def append(chunk: Any) -> None:
        nonlocal out, written
        need = written + int(chunk.shape[0])
        if need > out.shape[0]:
            grown = np.empty(max(need, int(out.shape[0] * 1.5)), dtype=np.float32)
            grown[:written] = out[:written]
            out = grown
        out[written:need] = chunk
        written = need

    total = 0
    clipped = 0
    n_values = 0
    sum_squares = 0.0
    try:
        with sf.SoundFile(io.BytesIO(data)) as handle:
            while True:
                block = handle.read(block_frames, dtype="float32", always_2d=True)
                if block.shape[0] == 0:
                    break
                total += block.shape[0]
                if total > max_frames:
                    # The header said this would fit. It lied, or had no length.
                    raise LimitExceeded(
                        f"This recording is longer than the {_clock(limits.max_audio_seconds)} limit.",
                        detail=f"decoded {total} frames, header declared {info.frames}",
                    )
                block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0)
                np.clip(block, -_SAMPLE_CEILING, _SAMPLE_CEILING, out=block)
                clipped += int(np.count_nonzero(np.abs(block) >= _CLIP_LEVEL))
                n_values += block.size
                mono = block.mean(axis=1, dtype=np.float32) if info.channels > 1 else block[:, 0]
                sum_squares += float(np.sum(mono.astype(np.float64) ** 2))
                mono = np.ascontiguousarray(mono, dtype=np.float32)
                append(resampler.resample_chunk(mono, last=False) if resampler else mono)
                tick(min(1.0, total / max(1, info.frames)))
    except (ScoreImportError, TranscriptionCancelled):
        raise
    except Exception as exc:
        raise MalformedFile(
            "The audio data in this file is damaged and could not be decoded.",
            detail=f"libsndfile after {total} frames: {exc}",
        ) from exc
    if resampler is not None:
        append(resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True))

    # A clean end of file well before the declared length: the tail is missing.
    short_by = info.frames - total
    truncated = short_by > max(0.01 * info.frames, 0.1 * info.sample_rate)
    return _Decoded(
        samples=out[:written],
        source_frames=total,
        clipped_fraction=clipped / n_values if n_values else 0.0,
        rms=math.sqrt(sum_squares / total) if total else 0.0,
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class _Model:
    session: Any
    input_name: str
    note_output: str
    onset_output: str
    path: str
    sha256: str
    signature: str


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, _Model] = {}


def _map_outputs(outputs: list[Any]) -> tuple[str, str, str]:
    """(note, onset, contour) output names, decided from the model itself.

    The contour head is the only one with 264 bins, so shape settles it. The
    note and onset heads are both (batch, 172, 88) and nothing in the graph
    says which is which, so names settle those: ``StatefulPartitionedCall:1`` is
    note and ``:2`` is onset, the mapping hard-coded in upstream's
    ``Model.predict`` (it is what alphabetical order of the head names -
    contour, note, onset - would give). Anything else is refused rather than
    guessed. `tests/test_audio_transcription.py` checks the mapping
    behaviourally too, so a swap cannot pass unnoticed.
    """
    by_bins: dict[int, list[str]] = {}
    for out in outputs:
        bins = out.shape[-1] if out.shape else None
        if isinstance(bins, int):
            by_bins.setdefault(bins, []).append(out.name)
    contour = by_bins.get(N_CONTOUR_BINS, [])
    semitone = sorted(by_bins.get(N_PITCHES, []))
    note = [n for n in semitone if n.endswith(":1") or "note" in n.lower()]
    onset = [n for n in semitone if n.endswith(":2") or "onset" in n.lower()]
    if len(contour) != 1 or len(semitone) != 2 or len(note) != 1 or len(onset) != 1 or note == onset:
        described = ", ".join(f"{o.name}{o.shape}" for o in outputs)
        raise AudioUnavailable(
            "Audio transcription is not available: the transcription model file is not "
            "the expected Basic Pitch model.",
            detail=f"cannot map outputs to note/onset/contour: {described}",
        )
    return note[0], onset[0], contour[0]


def _load_model() -> _Model:
    path, why = _find_model_path()
    if path is None:
        raise AudioUnavailable(
            "Audio transcription is not available on this server: the Basic Pitch model "
            f"file (nmp.onnx) was not found - {why}.",
            detail=why,
        )
    key = str(path.resolve())
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        ort = _require("onnxruntime")
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()

        options = ort.SessionOptions()
        # One thread, sequential: the same input gives the same floats on
        # every run, and a server can run one transcription per core.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        try:
            options.use_deterministic_compute = True
        except AttributeError:  # older onnxruntime
            pass
        try:
            session = ort.InferenceSession(blob, options, providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise AudioUnavailable(
                "Audio transcription is not available: the transcription model could not be loaded.",
                detail=f"onnxruntime failed to load {path}: {exc!r}",
            ) from exc

        inputs = session.get_inputs()
        outputs = session.get_outputs()
        shape = list(inputs[0].shape) if inputs else []
        if len(inputs) != 1 or len(shape) != 3 or shape[1] != AUDIO_N_SAMPLES or shape[2] != 1:
            raise AudioUnavailable(
                "Audio transcription is not available: the transcription model file is not "
                "the expected Basic Pitch model.",
                detail=f"unexpected inputs: {[(i.name, i.shape) for i in inputs]}",
            )
        note, onset, _contour = _map_outputs(outputs)
        signature = (
            f"in {inputs[0].name}{shape}; "
            + "; ".join(f"out {o.name}{list(o.shape)}" for o in outputs)
        )
        model = _Model(session, inputs[0].name, note, onset, str(path), digest, signature)
        _MODEL_CACHE[key] = model
        return model


def _expected_frames(n_samples: int) -> int:
    """Frames that cover `n_samples` of audio once the windows are unwrapped.

    Each window keeps 142 of its 172 frames and advances the audio by
    HOP_SIZE = 36164 samples, so a kept frame stands for 36164/142 = 254.68
    samples, not 256. Upstream trims to ``floor(seconds * 86)`` frames, which
    is 0.67% too few and silently discards the end of the recording (4.8 s of
    a 12 minute file). This keeps every frame that starts inside the audio.
    """
    return int(math.ceil(n_samples * KEPT_FRAMES / HOP_SIZE))


def _run_model(
    audio: "np.ndarray",
    model: _Model,
    tick: Callable[[float], None],
    check_cancel: Callable[[], None],
) -> tuple["np.ndarray", "np.ndarray"]:
    """The upstream `run_inference`: pad, window, predict, unwrap. One window per call.

    Upstream prepends OVERLAP_LEN/2 zeros, cuts windows of AUDIO_N_SAMPLES
    every HOP_SIZE samples (zero-padding the last), and after prediction drops
    N_OVERLAPPING_FRAMES/2 frames from each side of every window before
    concatenating. This does the same without materialising the padded copy
    of the audio or a list of per-window outputs.
    """
    np = _require("numpy")
    original_length = int(audio.shape[0])
    front_pad = OVERLAP_LEN // 2
    starts = range(0, original_length + front_pad, HOP_SIZE)  # positions in the padded signal
    n_olap = N_OVERLAPPING_FRAMES // 2

    note_post = np.empty((len(starts) * KEPT_FRAMES, N_PITCHES), dtype=np.float32)
    onset_post = np.empty_like(note_post)
    batch = np.zeros((1, AUDIO_N_SAMPLES, 1), dtype=np.float32)
    for index, start in enumerate(starts):
        check_cancel()
        lo = start - front_pad                      # same window, in unpadded coordinates
        src = audio[max(lo, 0):max(0, min(original_length, lo + AUDIO_N_SAMPLES))]
        batch[:] = 0.0
        offset = max(0, -lo)
        batch[0, offset:offset + src.shape[0], 0] = src
        note, onset = model.session.run(
            [model.note_output, model.onset_output], {model.input_name: batch}
        )
        if note.shape != (1, WINDOW_FRAMES, N_PITCHES) or onset.shape != note.shape:
            raise AudioUnavailable(
                "Audio transcription is not available: the transcription model gave output "
                "of an unexpected shape.",
                detail=f"note {note.shape}, onset {onset.shape}",
            )
        row = index * KEPT_FRAMES
        note_post[row:row + KEPT_FRAMES] = note[0, n_olap:-n_olap, :]
        onset_post[row:row + KEPT_FRAMES] = onset[0, n_olap:-n_olap, :]
        tick((index + 1) / len(starts))

    n_frames = min(_expected_frames(original_length), note_post.shape[0])
    return note_post[:n_frames], onset_post[:n_frames]


# --------------------------------------------------------------------------
# Confidence, polyphony, tempo
# --------------------------------------------------------------------------


def _note_confidence(note_mean: float, onset_peak: float) -> float:
    """confidence = clip(0.6 * mean note posterior + 0.4 * onset posterior, 0, 1).

    `note_mean` is the model's note posterior at the note's pitch, averaged
    over the note's frames. `onset_peak` is the model's *raw* onset posterior
    (not the frame-difference onsets used for peak picking) at that pitch,
    maximum over the start frame and one frame either side. A note found by
    the melodia pass has no onset peak by construction, so it cannot score
    above 0.6 plus whatever onset evidence happens to be there.
    """
    value = CONFIDENCE_NOTE_WEIGHT * note_mean + CONFIDENCE_ONSET_WEIGHT * onset_peak
    return float(min(1.0, max(0.0, value)))


def _max_polyphony(spans: list[tuple[float, float]]) -> int:
    events: list[tuple[float, int]] = []
    for onset, offset in spans:
        events.append((onset, 1))
        events.append((offset, -1))
    events.sort(key=lambda e: (e[0], e[1]))  # offs before ons at the same instant
    best = current = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


def _estimate_tempo(onset_post: "np.ndarray", frame_seconds: float) -> tuple[float | None, float]:
    """(bpm, confidence) from the autocorrelation of the onset-strength envelope.

    The envelope is the onset posterior summed over pitch, per frame. Its
    autocorrelation (mean removed, normalised so lag 0 is 1, corrected for the
    shrinking overlap) is searched over lags of 60/200 s to 60/50 s. A mild
    log-normal preference for ~110 bpm breaks the tie between a tempo and its
    half or double; it cannot rescue a wrong choice, only pick among peaks.

    confidence = (peak - median) / (1 - median), clipped to 0..1, where
    `median` is the median autocorrelation over the searched lags. A strictly
    periodic onset train scores near 1; onsets with no common pulse score near
    0. It says how periodic the onsets are, not that the octave is right.
    """
    np = _require("numpy")
    n = int(onset_post.shape[0])
    if n * frame_seconds < TEMPO_MIN_SECONDS:
        return None, 0.0
    envelope = onset_post.sum(axis=1).astype(np.float64)
    envelope -= envelope.mean()
    energy = float(np.dot(envelope, envelope))
    if energy <= 1e-9:
        return None, 0.0

    lag_min = max(1, int(math.floor(60.0 / TEMPO_MAX_BPM / frame_seconds)))
    lag_max = min(n // 2, int(math.ceil(60.0 / TEMPO_MIN_BPM / frame_seconds)))
    if lag_max - lag_min < 3:
        return None, 0.0

    size = 1 << int(math.ceil(math.log2(2 * n)))
    spectrum = np.fft.rfft(envelope, size)
    acf = np.fft.irfft(spectrum * np.conj(spectrum), size)[: lag_max + 2]
    acf = acf / energy * (n / (n - np.arange(acf.shape[0])))

    lags = np.arange(lag_min, lag_max + 1)
    bpms = 60.0 / (lags * frame_seconds)
    prior = np.exp(-0.5 * (np.log2(bpms / 110.0) / 1.0) ** 2)
    window = acf[lag_min:lag_max + 1]
    best = int(np.argmax(window * prior))
    lag = lag_min + best
    peak = float(acf[lag])
    median = float(np.median(window))
    confidence = 0.0 if peak <= 0 else (peak - median) / max(1e-9, 1.0 - median)
    confidence = float(min(1.0, max(0.0, confidence)))

    # Parabolic interpolation: one frame at 120 bpm is 2.8 bpm, too coarse.
    refined = float(lag)
    if 1 <= lag < acf.shape[0] - 1:
        left, mid, right = float(acf[lag - 1]), float(acf[lag]), float(acf[lag + 1])
        denom = left - 2.0 * mid + right
        if denom < 0:
            refined = lag + 0.5 * (left - right) / denom
    return 60.0 / (refined * frame_seconds), confidence


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def transcribe_audio(
    data: bytes,
    *,
    filename: str | None = None,
    options: TranscriptionOptions = TranscriptionOptions(),
    limits: ImportLimits = DEFAULT_LIMITS,
    progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> TranscriptionResult:
    """Transcribe an audio file to a `Score`. See the module docstring for the caveats.

    `progress(fraction, stage)` is called with a non-decreasing fraction in
    [0, 1]: stages "decode", "inference" (once per 2-second window), "notes",
    "tempo", "done". `should_cancel()` is polled between windows and inside the
    note-creation loops; when it returns true, `TranscriptionCancelled` is raised.
    """
    _validate_options(options)
    reported = 0.0

    def report(fraction: float, stage: str) -> None:
        nonlocal reported
        reported = min(1.0, max(reported, fraction))
        if progress is not None:
            progress(reported, stage)

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise TranscriptionCancelled("transcription cancelled")

    info = probe_audio(data, filename=filename, limits=limits)
    support = audio_support()
    if not support.available:
        raise AudioUnavailable(
            "Audio transcription is not available on this server. Missing: "
            + "; ".join(support.missing) + ".",
            detail=support.detail,
        )
    from . import _basic_pitch_notes as bp

    model = _load_model()
    check_cancel()
    report(0.0, "decode")
    decoded = _decode(bytes(data), info, limits, lambda f: (check_cancel(), report(0.10 * f, "decode")))
    audio = decoded.samples
    duration = decoded.source_frames / info.sample_rate
    if audio.shape[0] == 0 or decoded.source_frames == 0:
        raise EmptyScore("This audio file contains no sound.", detail="zero frames decoded")

    note_post, onset_post = _run_model(
        audio, model, lambda f: report(0.10 + 0.78 * f, "inference"), check_cancel
    )

    report(0.88, "notes")
    frame_notes = bp.output_to_notes_polyphonic(
        note_post,
        onset_post,
        onset_thresh=options.onset_threshold,
        frame_thresh=options.frame_threshold,
        min_note_len=int(round(options.min_note_ms / 1000.0 * (AUDIO_SAMPLE_RATE / FFT_HOP))),
        min_pitch=options.min_pitch,
        max_pitch=options.max_pitch,
        melodia_trick=options.melodia_trick,
        checkpoint=check_cancel,
    )
    if not frame_notes:
        raise EmptyScore(
            "No notes could be found in this recording. It may be silent, very quiet, "
            "or contain no pitched instrument.",
            detail=f"max note posterior {float(note_post.max()) if note_post.size else 0.0:.3f}",
        )
    if len(frame_notes) > limits.max_notes:
        raise LimitExceeded(
            f"This recording produced {len(frame_notes)} notes; the limit is {limits.max_notes}.",
        )

    warnings: list[str] = list(info.warnings)
    if model.sha256 != KNOWN_MODEL_SHA256:
        warnings.append(
            "The transcription model file differs from the one this importer was tested "
            "with; accuracy may differ from what is documented."
        )

    report(0.95, "tempo")
    times = bp.model_frames_to_time(note_post.shape[0] + 1)
    frame_seconds = float(times[-1] - times[0]) / max(1, times.shape[0] - 1)
    tempo_bpm: float | None = None
    tempo_confidence: float | None = None
    if options.estimate_tempo:
        n_onsets = sum(1 for fn in frame_notes if fn.from_onset)
        if n_onsets >= TEMPO_MIN_ONSETS:
            candidate, tempo_confidence = _estimate_tempo(onset_post, frame_seconds)
            if candidate is not None and tempo_confidence >= TEMPO_MIN_CONFIDENCE:
                tempo_bpm = round(candidate, 1)
        else:
            tempo_confidence = 0.0
    if tempo_bpm is None:
        warnings.append(
            "No steady tempo could be found"
            + ("" if options.estimate_tempo else " (tempo estimation was turned off)")
            + f", so the score uses a placeholder of {FALLBACK_BPM:.0f} bpm. Bar lines and "
            "beat positions are not meaningful until you set the tempo."
        )
    else:
        warnings.append(
            f"Tempo estimated at {tempo_bpm:g} bpm from how regular the note onsets are. "
            "It may be half or double the felt tempo; check it before trusting bar lines."
        )
    score_bpm = tempo_bpm if tempo_bpm is not None else FALLBACK_BPM
    timeline = Timeline.constant(score_bpm)

    # Frame notes -> Notes, in (onset, pitch) order so ids are stable.
    rows = []
    for fn in frame_notes:
        onset_s = max(0.0, float(times[fn.start]))
        offset_s = float(times[fn.end])
        bin_index = fn.pitch - bp.MIDI_OFFSET
        lo, hi = max(0, fn.start - 1), min(onset_post.shape[0], fn.start + 2)
        onset_peak = float(onset_post[lo:hi, bin_index].max())
        note_mean = float(note_post[fn.start:fn.end, bin_index].mean())
        rows.append((onset_s, fn.pitch, max(offset_s - onset_s, 1e-3), note_mean, onset_peak))
    rows.sort(key=lambda r: (r[0], r[1]))

    notes: list[Note] = []
    for index, (onset_s, pitch, dur_s, note_mean, onset_peak) in enumerate(rows):
        beat = timeline.beat_at(onset_s)
        notes.append(
            Note(
                pitch=int(pitch),
                onset=round(onset_s, 6),
                duration=round(dur_s, 6),
                bar=timeline.bar_at(beat),
                id=f"a{index}",
                velocity=int(min(127, max(1, round(127 * note_mean)))),
                beat=round(beat, 6),
                beats=round(timeline.beat_at(onset_s + dur_s) - beat, 6),
                track=0,
                confidence=round(_note_confidence(note_mean, onset_peak), 4),
            )
        )

    total_duration = sum(n.duration for n in notes)
    overall = sum(n.confidence * n.duration for n in notes) / total_duration
    low_ids = [n.id for n in notes if n.confidence < LOW_CONFIDENCE]
    polyphony = _max_polyphony([(n.onset, n.offset) for n in notes])

    if overall < LOW_CONFIDENCE:
        warnings.append(
            f"Overall confidence is low ({overall:.2f}). Expect many wrong or missing notes; "
            "this recording may be noisy, reverberant, or not a pitched instrument."
        )
    if polyphony > DENSE_POLYPHONY:
        warnings.append(
            f"Up to {polyphony} notes sound at once. Dense chords are where this model is "
            "weakest: expect missing inner notes and extra notes an octave or a fifth away."
        )
    if decoded.clipped_fraction > _CLIP_FRACTION:
        warnings.append(
            f"The recording is clipped ({decoded.clipped_fraction:.1%} of samples at full scale). "
            "Distortion adds harmonics that can be transcribed as notes."
        )
    if decoded.rms < _LOW_RMS:
        warnings.append(
            f"The recording is very quiet (RMS {20 * math.log10(max(decoded.rms, 1e-9)):.0f} dBFS). "
            "Quiet notes may have been missed."
        )
    if decoded.truncated:
        warnings.append(
            f"The file ended early: its header promises {_clock(info.duration_seconds)} but only "
            f"{_clock(duration)} could be decoded. Only that part was transcribed."
        )

    pitches = [n.pitch for n in notes]
    score = Score(
        notes=notes,
        tempo_bpm=score_bpm,
        title=_title_from(filename, limits),
        timeline=timeline,
        tracks=[
            TrackInfo(
                index=0,
                name="Transcribed audio",
                note_count=len(notes),
                lowest_pitch=min(pitches),
                highest_pitch=max(pitches),
            )
        ],
        source_format="audio",
        warnings=list(warnings),
    )
    report(1.0, "done")
    return TranscriptionResult(
        score=score,
        duration_seconds=duration,
        sample_rate=info.sample_rate,
        overall_confidence=round(overall, 4),
        low_confidence_note_ids=low_ids,
        tempo_bpm=tempo_bpm,
        tempo_confidence=None if tempo_confidence is None else round(tempo_confidence, 4),
        warnings=warnings,
        model=MODEL_NAME,
        model_sha256=model.sha256,
        max_polyphony=polyphony,
        info=info,
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _validate_options(options: TranscriptionOptions) -> None:
    if not 0.0 < options.onset_threshold <= 1.0 or not 0.0 < options.frame_threshold <= 1.0:
        raise ValueError("onset_threshold and frame_threshold must be in (0, 1]")
    if not 0.0 <= options.min_note_ms <= 5000.0:
        raise ValueError("min_note_ms must be between 0 and 5000")
    if not 0 <= options.min_pitch <= options.max_pitch <= 127:
        raise ValueError("need 0 <= min_pitch <= max_pitch <= 127")


def _clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _safe_label(filename: str | None) -> str:
    if not filename:
        return "This file"
    name = re.sub(r"[^\w .()\-]", "_", Path(str(filename)).name)[:80]
    return f"'{name}'" if name else "This file"


def _title_from(filename: str | None, limits: ImportLimits) -> str:
    if not filename:
        return "untitled"
    stem = Path(str(filename)).stem
    stem = re.sub(r"[\x00-\x1f\x7f]", "", stem).strip()
    return stem[: limits.max_title_chars] or "untitled"
