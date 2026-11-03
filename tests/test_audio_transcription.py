"""Proof that the audio adapter really transcribes audio.

Every clip is synthesised in-process by `tests/audio_synth.py`, so the ground
truth is exact and there are no assets to license. The model tests run the
real Basic Pitch ONNX model through `transcribe_audio`; nothing is mocked on
that path. They skip, loudly, only when `audio_support()` says the optional
`audio` extra is not installed.

Scoring is note-level: a detected note is correct when its pitch is exact and
its onset is within 50 ms of a not-yet-matched true note. Offsets are not
scored. Measured precision/recall/F1 are printed (run with `-s` to see them).

Run:  python -m pytest tests/test_audio_transcription.py -q
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from arranger.adapters import audio as A  # noqa: E402
from arranger.ir import Score  # noqa: E402
from arranger.limits import (  # noqa: E402
    EmptyScore,
    ImportLimits,
    LimitExceeded,
    MalformedFile,
    ScoreImportError,
    UnsupportedFormat,
)

SUPPORT = A.audio_support()
needs_model = pytest.mark.skipif(
    not SUPPORT.available,
    reason=f"audio extra not installed, missing: {SUPPORT.missing}. {SUPPORT.detail}",
)
try:  # the synthesiser needs numpy + soundfile, i.e. the same extra
    import numpy as np

    import audio_synth as synth
except ImportError:  # pragma: no cover - exercised only without the extra
    np = None
    synth = None
needs_synth = pytest.mark.skipif(synth is None, reason="numpy/soundfile not installed")

ONSET_TOLERANCE = 0.050

C_MAJOR = [60, 62, 64, 65, 67, 69, 71, 72]
SCALE = [(pitch, 0.25 + 0.5 * i, 0.45, 96) for i, pitch in enumerate(C_MAJOR)]  # 120 bpm


def two_hand_texture():
    """Four bars at 100 bpm: a melody in quarters over a sustained triad per bar."""
    beat = 0.6
    chords = [(48, 52, 55), (53, 57, 60), (55, 59, 62), (48, 52, 55)]
    melody = [72, 74, 76, 72, 77, 76, 74, 72, 79, 77, 76, 74, 72, 76, 79, 84]
    notes = []
    for bar, chord in enumerate(chords):
        for pitch in chord:
            notes.append((pitch, 0.3 + bar * 4 * beat, 4 * beat * 0.95, 70))
    for i, pitch in enumerate(melody):
        notes.append((pitch, 0.3 + i * beat, beat * 0.9, 100))
    return notes


def evaluate(truth, notes, tolerance=ONSET_TOLERANCE):
    """One-to-one matching on exact pitch and onset within `tolerance`."""
    used: set[int] = set()
    onset_errors = []
    for pitch, onset, _dur, _vel in truth:
        best = None
        for i, note in enumerate(notes):
            if i in used or note.pitch != pitch or abs(note.onset - onset) > tolerance:
                continue
            if best is None or abs(note.onset - onset) < abs(notes[best].onset - onset):
                best = i
        if best is not None:
            used.add(best)
            onset_errors.append(notes[best].onset - onset)
    tp = len(used)
    precision = tp / len(notes) if notes else 0.0
    recall = tp / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "onset_errors": onset_errors,
        "false_positives": [n for i, n in enumerate(notes) if i not in used],
        "matched": [notes[i] for i in sorted(used)],
    }


def report(label, m):
    worst = max((abs(e) for e in m["onset_errors"]), default=0.0)
    print(
        f"\n[measured] {label}: precision={m['precision']:.3f} recall={m['recall']:.3f} "
        f"f1={m['f1']:.3f} worst_onset_error={1000 * worst:.1f}ms"
    )


def as_rows(result):
    return [(n.id, n.pitch, n.onset, n.duration, n.velocity, n.confidence) for n in result.score.notes]


@pytest.fixture(scope="module")
def scale_result():
    return A.transcribe_audio(synth.wav_bytes(SCALE), filename="c major scale.wav")


# --------------------------------------------------------------------------
# 1-2. Accuracy
# --------------------------------------------------------------------------


def test_audio_support_is_present_when_required():
    """Skips are honest but easy to miss. With ARRANGER_REQUIRE_AUDIO=1 a
    missing extra is a failure, so a green run cannot mean "nothing ran"."""
    if os.environ.get("ARRANGER_REQUIRE_AUDIO") == "1":
        assert SUPPORT.available, f"audio extra required but missing {SUPPORT.missing}: {SUPPORT.detail}"
        assert synth is not None


@needs_model
def test_the_model_tests_are_not_skipped_here():
    """A guard against a green run that proved nothing."""
    assert SUPPORT.available and SUPPORT.model_path and os.path.isfile(SUPPORT.model_path)
    for container in ("WAV", "FLAC", "OGG"):
        assert container in SUPPORT.detail


@needs_model
def test_monophonic_scale_is_recovered_exactly(scale_result):
    m = evaluate(SCALE, scale_result.score.notes)
    report("C major scale (monophonic)", m)
    assert sorted(n.pitch for n in m["matched"]) == C_MAJOR, "every pitch recovered"
    assert all(abs(e) <= ONSET_TOLERANCE for e in m["onset_errors"])
    assert m["recall"] == 1.0
    assert m["f1"] >= 0.95


@needs_model
def test_result_is_a_well_formed_audio_score(scale_result):
    r = scale_result
    assert isinstance(r.score, Score)
    assert r.score.source_format == "audio"
    assert r.score.title == "c major scale"
    assert r.score.timeline is not None
    assert [n.id for n in r.score.notes] == [f"a{i}" for i in range(len(r.score.notes))]
    assert r.score.tracks[0].note_count == len(r.score.notes)
    assert r.model == "basic-pitch-icassp-2022-onnx"
    assert len(r.model_sha256) == 64
    assert r.sample_rate == 22050
    assert r.duration_seconds == pytest.approx(len(synth.synth(SCALE)) / 22050, abs=1e-6)
    for n in r.score.notes:
        assert 21 <= n.pitch <= 108 and n.duration > 0 and n.onset >= 0
        assert 1 <= n.velocity <= 127
        assert n.confidence is not None and 0.0 <= n.confidence <= 1.0
        assert n.beat == pytest.approx(r.score.timeline.beat_at(n.onset), abs=1e-4)
        assert n.bar == r.score.timeline.bar_at(n.beat) and n.beats > 0
    total = sum(n.duration for n in r.score.notes)
    weighted = sum(n.confidence * n.duration for n in r.score.notes) / total
    assert r.overall_confidence == pytest.approx(weighted, abs=1e-3)
    assert r.low_confidence_note_ids == [n.id for n in r.score.notes if n.confidence < 0.5]
    assert r.score.warnings == r.warnings


@needs_model
def test_two_hand_texture():
    truth = two_hand_texture()
    r = A.transcribe_audio(synth.wav_bytes(truth))
    m = evaluate(truth, r.score.notes)
    report("melody over sustained triads (two hands)", m)
    print(
        "[measured] two-hand false positives (pitch, onset, confidence):",
        [(n.pitch, round(n.onset, 2), n.confidence) for n in m["false_positives"]],
    )
    assert m["f1"] >= 0.80
    melody = [t for t in truth if t[0] >= 72]
    assert evaluate(melody, [n for n in r.score.notes if n.pitch >= 72])["recall"] == 1.0


@needs_model
def test_onsets_do_not_drift_and_the_last_note_survives():
    """45 s of notes: frame times stay aligned, and the end of the file is kept.

    Upstream Basic Pitch trims its unwrapped output to floor(seconds * 86)
    frames. Unwrapped frames are 11.55 ms apart, not 1/86 s, so that cut lands
    0.67% before the end of the audio. The final note here starts after that
    cut: with upstream trimming it is lost, with `_expected_frames` it is kept.
    """
    from arranger.adapters import _basic_pitch_notes as bp

    truth = [(60 + (i % 12), 0.2 + 0.5 * i, 0.4, 96) for i in range(89)]
    final = (67, truth[-1][1] + 0.55, 0.25, 96)
    truth.append(final)
    samples = synth.synth(truth, tail=0.0)

    r = A.transcribe_audio(synth.to_bytes(samples))
    m = evaluate(truth, r.score.notes)
    report("45 s chromatic run", m)
    assert m["f1"] >= 0.95
    errors = m["onset_errors"]
    assert abs(sum(errors[-10:]) / 10 - sum(errors[:10]) / 10) < 0.015, "no drift"
    assert any(n.pitch == 67 and abs(n.onset - final[1]) <= ONSET_TOLERANCE for n in r.score.notes)

    # The same posteriors, trimmed the upstream way, do not contain that note.
    note, onset = A._run_model(samples, A._load_model(), lambda f: None, lambda: None)
    n_upstream = int(np.floor(len(samples) * (86 / 22050)))
    assert n_upstream < note.shape[0]
    assert bp.model_frames_to_time(n_upstream)[-1] < final[1] < len(samples) / 22050
    trimmed = bp.output_to_notes_polyphonic(
        note[:n_upstream], onset[:n_upstream], onset_thresh=0.5, frame_thresh=0.3, min_note_len=5
    )
    assert len(trimmed) == len(r.score.notes) - 1
    assert bp.model_frames_to_time(note.shape[0])[-1] == pytest.approx(len(samples) / 22050, abs=0.03)


# --------------------------------------------------------------------------
# 3. Silence and noise
# --------------------------------------------------------------------------


@needs_model
def test_silence_is_an_empty_score():
    silence = np.zeros(22050 * 3, dtype=np.float32)
    with pytest.raises(EmptyScore) as err:
        A.transcribe_audio(synth.to_bytes(silence))
    assert err.value.code == "empty_score"


@needs_model
def test_white_noise_gives_no_confident_notes():
    data = synth.to_bytes(synth.white_noise(4.0, rms=0.1, seed=1))
    try:
        r = A.transcribe_audio(data)
    except EmptyScore:
        return
    print(f"\n[measured] white noise: {len(r.score.notes)} notes, overall={r.overall_confidence}")
    assert r.overall_confidence < 0.5
    assert all(n.confidence < 0.5 for n in r.score.notes), "no confident notes"
    assert set(r.low_confidence_note_ids) == {n.id for n in r.score.notes}
    assert any("confidence is low" in w for w in r.warnings)
    assert r.tempo_bpm is None


# --------------------------------------------------------------------------
# 4. Limits and hostile input
# --------------------------------------------------------------------------


def test_oversized_upload_is_refused_before_anything_is_opened(monkeypatch):
    """Needs no audio library at all: the size check comes first."""
    def boom(name):
        raise AssertionError(f"tried to import {name} before checking the size")

    monkeypatch.setattr(A, "_require", boom)
    limits = ImportLimits(max_audio_bytes=1000)
    with pytest.raises(LimitExceeded) as err:
        A.probe_audio(b"RIFF" + b"\x00" * 2000, filename="big.wav", limits=limits)
    assert err.value.code == "limit_exceeded"
    with pytest.raises(LimitExceeded):
        A.transcribe_audio(b"\x00" * 1001, limits=limits)


@needs_synth
def test_overlong_audio_is_refused_before_decoding(monkeypatch):
    data = synth.to_bytes(np.zeros(22050 * 5, dtype=np.float32))
    monkeypatch.setattr(A, "_decode", lambda *a, **k: pytest.fail("decoded an over-long file"))
    with pytest.raises(LimitExceeded) as err:
        A.transcribe_audio(data, limits=ImportLimits(max_audio_seconds=4.0))
    assert "limit" in err.value.public
    with pytest.raises(LimitExceeded):
        A.probe_audio(data, filename=None, limits=ImportLimits(max_audio_seconds=4.0))
    assert A.probe_audio(data, filename=None, limits=ImportLimits(max_audio_seconds=5.0)).frames == 22050 * 5


@needs_synth
def test_decoder_stops_when_the_header_understates_the_length():
    """The header is not trusted: decoding re-checks the limit as it goes."""
    data = synth.to_bytes(np.zeros(22050 * 3, dtype=np.float32))
    honest = A.probe_audio(data, filename=None)
    lying = A.AudioInfo(honest.format, honest.subtype, 1, 22050, 22050 // 2, 0.5, honest.byte_size)
    with pytest.raises(LimitExceeded):
        A._decode(data, lying, ImportLimits(max_audio_seconds=1.0), lambda f: None)


@needs_synth
@pytest.mark.parametrize(
    "blob",
    [bytes(range(256)) * 40, b"", b"RIFF\x24\x00\x00\x00WAVEfmt "],
    ids=["garbage", "empty", "wav-header-only"],
)
def test_garbage_is_malformed(blob):
    with pytest.raises(MalformedFile) as err:
        A.probe_audio(blob, filename="x.wav")
    assert err.value.code == "malformed_file"
    with pytest.raises(MalformedFile):
        A.transcribe_audio(blob, filename="x.wav")


@needs_synth
def test_truncated_wav_is_malformed():
    data = synth.wav_bytes(SCALE)
    with pytest.raises(MalformedFile) as err:
        A.transcribe_audio(data[: len(data) // 2], filename="scale.wav")
    assert "cut short" in err.value.public
    with pytest.raises(MalformedFile):
        A.transcribe_audio(data[:30], filename="scale.wav")


@needs_synth
def test_audio_damaged_in_the_middle_is_malformed():
    """The header is fine, so the probe passes; the decoder is what finds out."""
    flac = bytearray(synth.to_bytes(synth.synth(SCALE), fmt="FLAC", subtype=None))
    middle = len(flac) // 2
    flac[middle:middle + 600] = bytes(600)
    assert A.probe_audio(bytes(flac), filename=None).format == "FLAC"
    with pytest.raises(MalformedFile) as err:
        A.transcribe_audio(bytes(flac), filename="scale.flac")
    assert "damaged" in err.value.public and "libsndfile" in err.value.detail


@needs_synth
def test_png_renamed_wav_is_rejected_by_content():
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 4000
    with pytest.raises((UnsupportedFormat, MalformedFile)) as err:
        A.transcribe_audio(png, filename="totally-audio.wav")
    assert isinstance(err.value, UnsupportedFormat) and "PNG" in err.value.public


@needs_model
def test_wav_named_as_something_else_is_accepted_by_content():
    r = A.transcribe_audio(synth.wav_bytes(SCALE), filename="holiday-photo.png")
    assert [n.pitch for n in r.score.notes] == C_MAJOR


# --------------------------------------------------------------------------
# 5. Cancellation and progress
# --------------------------------------------------------------------------


@needs_model
def test_cancel_after_first_window_and_progress_is_monotonic():
    data = synth.to_bytes(synth.synth([(60 + i, 0.2 + 0.5 * i, 0.4, 96) for i in range(16)]))  # ~8.7 s
    seen: list[tuple[float, str]] = []

    def windows_done():
        return sum(1 for _, stage in seen if stage == "inference")

    with pytest.raises(A.TranscriptionCancelled):
        A.transcribe_audio(
            data,
            progress=lambda fraction, stage: seen.append((fraction, stage)),
            should_cancel=lambda: windows_done() >= 1,
        )
    fractions = [f for f, _ in seen]
    assert windows_done() == 1, "stopped before the second window ran"
    assert all(0.0 <= f <= 1.0 for f in fractions)
    assert fractions == sorted(fractions)
    assert "done" not in [stage for _, stage in seen] and fractions[-1] < 1.0
    assert not isinstance(A.TranscriptionCancelled("x"), ScoreImportError)


@needs_model
def test_cancel_during_decoding_is_a_cancel_not_a_bad_file():
    """Regression: the decoder wraps library errors as MalformedFile, and must
    not do that to a cancellation raised from its progress tick."""
    calls = []

    def cancel_on_second_poll():
        calls.append(1)
        return len(calls) >= 2   # first poll is before decoding, second is inside it

    with pytest.raises(A.TranscriptionCancelled):
        A.transcribe_audio(synth.wav_bytes(SCALE), should_cancel=cancel_on_second_poll)
    assert len(calls) == 2


@needs_model
def test_progress_completes_and_reports_every_window():
    data = synth.to_bytes(synth.synth([(60 + i, 0.2 + 0.5 * i, 0.4, 96) for i in range(16)]))
    seen: list[tuple[float, str]] = []
    A.transcribe_audio(data, progress=lambda f, s: seen.append((f, s)))
    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions) and fractions[0] >= 0.0 and fractions[-1] == 1.0
    n_samples = A.probe_audio(data, filename=None).frames + A.OVERLAP_LEN // 2
    assert sum(1 for _, s in seen if s == "inference") == -(-n_samples // A.HOP_SIZE)
    assert seen[-1][1] == "done"


# --------------------------------------------------------------------------
# 6. Confidence means something
# --------------------------------------------------------------------------


@needs_model
def test_confidence_falls_as_noise_rises():
    clean = synth.synth([(64, 0.3, 1.0, 100)])
    confidences = {}
    for label, samples in (
        ("clean", clean),
        ("snr 0 dB", synth.add_noise(clean, 0.0, seed=5)),
        ("snr -10 dB", synth.add_noise(clean, -10.0, seed=5)),
    ):
        r = A.transcribe_audio(synth.to_bytes(samples))
        hits = [n for n in r.score.notes if n.pitch == 64 and abs(n.onset - 0.3) <= ONSET_TOLERANCE]
        assert hits, f"the note itself is still found at {label}"
        confidences[label] = hits[0].confidence
    print(f"\n[measured] confidence of the same E4: {confidences}")
    assert confidences["clean"] > confidences["snr 0 dB"] + 0.05
    assert confidences["snr 0 dB"] > confidences["snr -10 dB"]
    assert confidences["clean"] >= 0.8


@needs_model
def test_ghost_notes_score_below_real_ones():
    """A harmonic series cut off at 6 partials makes the model hear ghosts.

    That is a real failure of the model on an unnatural timbre, provoked on
    purpose. The point: every ghost is less confident than every real note,
    so the confidence is usable for triage.
    """
    r = A.transcribe_audio(synth.to_bytes(synth.synth(SCALE, n_harmonics=6, rolloff=1.0)))
    m = evaluate(SCALE, r.score.notes)
    report("scale with a truncated harmonic series (provoked ghosts)", m)
    assert m["recall"] == 1.0 and m["false_positives"], "this timbre is known to produce ghosts"
    assert max(n.confidence for n in m["false_positives"]) < min(n.confidence for n in m["matched"])
    assert max(n.confidence for n in m["false_positives"]) < 0.55


# --------------------------------------------------------------------------
# 7-8. Determinism and input-format invariance
# --------------------------------------------------------------------------


@needs_model
def test_two_runs_are_identical():
    data = synth.wav_bytes(two_hand_texture())
    first, second = A.transcribe_audio(data), A.transcribe_audio(data)
    assert as_rows(first) == as_rows(second)
    assert (first.overall_confidence, first.tempo_bpm, first.tempo_confidence) == (
        second.overall_confidence, second.tempo_bpm, second.tempo_confidence)
    assert first.model_sha256 == second.model_sha256 == A.KNOWN_MODEL_SHA256


def assert_same_notes(reference, other, onset_tol=0.012, conf_tol=0.05):
    ref = sorted(reference.score.notes, key=lambda n: (n.pitch, n.onset))
    got = sorted(other.score.notes, key=lambda n: (n.pitch, n.onset))
    assert [n.pitch for n in got] == [n.pitch for n in ref]
    for a, b in zip(ref, got, strict=True):
        assert abs(a.onset - b.onset) <= onset_tol
        assert abs(a.confidence - b.confidence) <= conf_tol


@needs_model
def test_stereo_and_48k_match_mono_22k(scale_result):
    hi = synth.synth(SCALE, sr=48000)
    mono48 = A.transcribe_audio(synth.to_bytes(hi, sr=48000))
    stereo48 = A.transcribe_audio(synth.to_bytes(np.stack([hi, 0.7 * hi], axis=1), sr=48000))
    stereo22 = A.transcribe_audio(synth.to_bytes(np.stack([synth.synth(SCALE)] * 2, axis=1)))
    float22 = A.transcribe_audio(synth.to_bytes(synth.synth(SCALE), subtype="FLOAT"))
    assert mono48.sample_rate == stereo48.sample_rate == 48000
    assert stereo48.info.channels == 2 and float22.info.subtype == "FLOAT"
    for other in (mono48, stereo48, stereo22, float22):
        assert_same_notes(scale_result, other)
        assert other.duration_seconds == pytest.approx(scale_result.duration_seconds, abs=0.01)


@needs_model
@pytest.mark.parametrize("container", ["FLAC", "OGG", "MP3", "AIFF", "WAVEX"])
def test_other_containers_decode_to_the_same_notes(container, scale_result):
    import soundfile as sf

    if container not in sf.available_formats():
        pytest.skip(f"this libsndfile build has no {container} support")
    data = synth.to_bytes(synth.synth(SCALE), fmt=container, subtype=None)
    assert A.probe_audio(data, filename=None).format == container
    result = A.transcribe_audio(data)
    assert_same_notes(scale_result, result)
    assert not any("cut short" in w or "ended early" in w for w in result.warnings)


@needs_model
def test_half_a_file_is_never_transcribed_silently():
    """libsndfile shortens a cut-off WAV, AIFF or OGG and says nothing. We must."""
    import soundfile as sf

    samples = synth.synth(SCALE)
    for container in ("WAV", "AIFF", "WAVEX"):
        data = synth.to_bytes(samples, fmt=container, subtype="PCM_16")
        with pytest.raises(MalformedFile) as err:
            A.transcribe_audio(data[: len(data) // 2])
        assert "cut short" in err.value.public

    data = synth.to_bytes(samples, fmt="OGG", subtype=None)
    assert A.probe_audio(data, filename=None).warnings == ()
    half = data[: len(data) // 2]
    try:
        result = A.transcribe_audio(half)
    except (MalformedFile, EmptyScore):
        pass  # a build that refuses the fragment outright is also fine
    else:
        assert any("cut short" in w for w in result.warnings)
        assert len(result.score.notes) < len(SCALE)

    # A complete file written in several goes (many Ogg pages) is not flagged.
    long_ogg = io.BytesIO()
    with sf.SoundFile(long_ogg, "w", samplerate=22050, channels=1, format="OGG") as handle:
        for _ in range(8):
            handle.write(samples)
    assert A.probe_audio(long_ogg.getvalue(), filename=None).warnings == ()


# --------------------------------------------------------------------------
# 9. Importable without the extra; support reported truthfully
# --------------------------------------------------------------------------


def test_import_succeeds_with_every_audio_dependency_missing():
    code = (
        "import sys\n"
        "for name in ('numpy', 'soundfile', 'soxr', 'onnxruntime', 'basic_pitch'):\n"
        "    sys.modules[name] = None\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import arranger.adapters.audio as A\n"
        "s = A.audio_support()\n"
        "assert s.available is False, s\n"
        "assert s.missing[:4] == ['numpy', 'soundfile', 'soxr', 'onnxruntime'], s.missing\n"
        "assert 'pip install' in s.detail and '--no-deps' in s.detail\n"
        "try:\n"
        "    A.probe_audio(b'RIFF1234WAVE', filename='a.wav')\n"
        "except A.AudioUnavailable as exc:\n"
        "    assert exc.code == 'audio_unavailable' and 'soundfile' in exc.public\n"
        "else:\n"
        "    raise SystemExit('expected AudioUnavailable')\n"
        "print('ok')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != A.MODEL_PATH_ENV}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


@needs_synth
def test_a_single_missing_dependency_is_named(monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    support = A.audio_support()
    assert support.available is False
    assert support.missing == ["onnxruntime"]
    assert "onnxruntime: not importable" in support.detail
    with pytest.raises(A.AudioUnavailable) as err:
        A.transcribe_audio(synth.wav_bytes(SCALE))
    assert err.value.code == "audio_unavailable" and "onnxruntime" in err.value.public
    assert isinstance(err.value, ScoreImportError)


@needs_synth
def test_a_missing_model_file_is_named_not_faked(monkeypatch, tmp_path):
    monkeypatch.setenv(A.MODEL_PATH_ENV, str(tmp_path / "nope.onnx"))
    support = A.audio_support()
    assert support.available is False and support.model_path is None
    assert len(support.missing) == 1 and "nmp.onnx" in support.missing[0]
    with pytest.raises(A.AudioUnavailable) as err:
        A.transcribe_audio(synth.wav_bytes(SCALE))
    assert "nmp.onnx" in err.value.public


@needs_model
def test_model_file_alone_is_enough(monkeypatch, tmp_path):
    """ARRANGER_BASIC_PITCH_ONNX points at a bare model file; basic_pitch is not consulted."""
    import shutil

    copy = tmp_path / "model.onnx"
    shutil.copyfile(SUPPORT.model_path, copy)
    monkeypatch.setenv(A.MODEL_PATH_ENV, str(copy))
    monkeypatch.setitem(sys.modules, "basic_pitch", None)  # find_spec now reports "not installed"
    support = A.audio_support()
    assert support.available and support.model_path == str(copy)
    r = A.transcribe_audio(synth.wav_bytes(SCALE))
    assert [n.pitch for n in r.score.notes] == C_MAJOR
    assert r.model_sha256 == A.KNOWN_MODEL_SHA256
    assert not any("model file differs" in w for w in r.warnings)


def test_support_report_matches_reality():
    import importlib.util

    expected_missing = [
        name for name in ("numpy", "soundfile", "soxr", "onnxruntime")
        if importlib.util.find_spec(name) is None
    ]
    assert [m for m in SUPPORT.missing if "nmp.onnx" not in m] == expected_missing
    assert SUPPORT.available == (not SUPPORT.missing)
    if SUPPORT.available and not os.environ.get(A.MODEL_PATH_ENV):
        assert Path(SUPPORT.model_path).name == "nmp.onnx"


# --------------------------------------------------------------------------
# The port itself
# --------------------------------------------------------------------------


@needs_model
def test_note_and_onset_heads_are_not_swapped():
    """Both heads are (172, 88); only behaviour tells them apart.

    For one sustained note the note posterior stays high for the whole note,
    while the onset posterior fires briefly at the start.
    """
    audio = synth.synth([(69, 0.5, 1.5, 100)])
    note, onset = A._run_model(audio, A._load_model(), lambda f: None, lambda: None)
    assert note.shape == onset.shape and note.shape[1] == 88
    bin_a4 = 69 - 21
    note_frames = int((note[:, bin_a4] > 0.3).sum())
    onset_frames = int((onset[:, bin_a4] > 0.3).sum())
    assert note_frames > 100 and 1 <= onset_frames < 15, (note_frames, onset_frames)
    times = __import__("arranger.adapters._basic_pitch_notes", fromlist=["x"]).model_frames_to_time(len(onset))
    assert abs(times[int(onset[:, bin_a4].argmax())] - 0.5) < 0.05
    assert int(note[60:150].mean(axis=0).argmax()) == bin_a4


def _reference_notes(frames, onsets, onset_thresh, frame_thresh, min_note_len, energy_tol=11):
    """Basic Pitch's `output_to_notes_polyphonic`, transcribed line for line.

    Differences from the upstream text: `scipy.signal.argrelmax` is written
    out as its definition, and the frequency limits (unused here) are gone.
    """
    n_frames = frames.shape[0]
    diffs = []
    for n in (1, 2):
        appended = np.concatenate([np.zeros((n, frames.shape[1])), frames])
        diffs.append(appended[n:, :] - appended[:-n, :])
    frame_diff = np.min(diffs, axis=0)
    frame_diff[frame_diff < 0] = 0
    frame_diff[:2, :] = 0
    frame_diff = np.max(onsets) * frame_diff / np.max(frame_diff)
    onsets = np.max([onsets, frame_diff], axis=0)

    peak_thresh_mat = np.zeros(onsets.shape)
    for t in range(1, n_frames - 1):
        for f in range(onsets.shape[1]):
            if onsets[t, f] > onsets[t - 1, f] and onsets[t, f] > onsets[t + 1, f]:
                peak_thresh_mat[t, f] = onsets[t, f]
    onset_idx = np.where(peak_thresh_mat >= onset_thresh)
    remaining_energy = np.array(frames, dtype=np.float64)
    events = []
    for note_start_idx, freq_idx in zip(onset_idx[0][::-1], onset_idx[1][::-1], strict=True):
        if note_start_idx >= n_frames - 1:
            continue
        i, k = note_start_idx + 1, 0
        while i < n_frames - 1 and k < energy_tol:
            k = k + 1 if remaining_energy[i, freq_idx] < frame_thresh else 0
            i += 1
        i -= k
        if i - note_start_idx <= min_note_len:
            continue
        remaining_energy[note_start_idx:i, freq_idx] = 0
        if freq_idx < 87:
            remaining_energy[note_start_idx:i, freq_idx + 1] = 0
        if freq_idx > 0:
            remaining_energy[note_start_idx:i, freq_idx - 1] = 0
        events.append((int(note_start_idx), int(i), int(freq_idx) + 21))

    while np.max(remaining_energy) > frame_thresh:
        i_mid, freq_idx = np.unravel_index(np.argmax(remaining_energy), remaining_energy.shape)
        remaining_energy[i_mid, freq_idx] = 0
        i, k = i_mid + 1, 0
        while i < n_frames - 1 and k < energy_tol:
            k = k + 1 if remaining_energy[i, freq_idx] < frame_thresh else 0
            remaining_energy[i, max(0, freq_idx - 1):freq_idx + 2] = 0
            i += 1
        i_end = i - 1 - k
        i, k = i_mid - 1, 0
        while i > 0 and k < energy_tol:
            k = k + 1 if remaining_energy[i, freq_idx] < frame_thresh else 0
            remaining_energy[i, max(0, freq_idx - 1):freq_idx + 2] = 0
            i -= 1
        i_start = i + 1 + k
        if i_end - i_start <= min_note_len:
            continue
        events.append((int(i_start), int(i_end), int(freq_idx) + 21))
    return events


@needs_model
@pytest.mark.parametrize("source", ["two-hand", "noisy", "ghosts"])
def test_note_creation_matches_a_literal_port_of_upstream(source):
    from arranger.adapters import _basic_pitch_notes as bp

    samples = {
        "two-hand": lambda: synth.synth(two_hand_texture()),
        "noisy": lambda: synth.add_noise(synth.synth(two_hand_texture()), -3.0, seed=2),
        "ghosts": lambda: synth.synth(SCALE, n_harmonics=6, rolloff=1.0),
    }[source]()
    note, onset = A._run_model(samples, A._load_model(), lambda f: None, lambda: None)
    before = (note.copy(), onset.copy())
    ours = bp.output_to_notes_polyphonic(note, onset, onset_thresh=0.5, frame_thresh=0.3, min_note_len=5)
    assert (note == before[0]).all() and (onset == before[1]).all(), "inputs must not be mutated"
    reference = _reference_notes(note.astype(np.float64), onset.astype(np.float64), 0.5, 0.3, 5)
    assert [(n.start, n.end, n.pitch) for n in ours] == reference
    assert any(not n.from_onset for n in ours) or source == "two-hand"


@needs_synth
def test_peak_picking_and_block_argmax_match_their_definitions():
    from arranger.adapters import _basic_pitch_notes as bp

    rng = np.random.default_rng(0)
    x = rng.integers(0, 4, size=(700, 88)).astype(np.float32)  # many ties and plateaus
    mask = bp.relative_maxima(x)
    assert not mask[0].any() and not mask[-1].any()
    for t, f in [(1, 0), (5, 3), (350, 40), (698, 87)]:
        assert bool(mask[t, f]) == bool(x[t, f] > x[t - 1, f] and x[t, f] > x[t + 1, f])
    assert not bp.relative_maxima(np.ones((10, 88), dtype=np.float32)).any(), "plateaus are not peaks"

    cache = bp._BlockMax(x, block=64)
    for _ in range(200):
        expected = np.unravel_index(int(np.argmax(x)), x.shape)
        assert cache.argmax() == (int(expected[0]), int(expected[1]))
        assert cache.max() == float(x.max())
        t, f = cache.argmax()
        lo, hi = max(0, t - 9), min(x.shape[0] - 1, t + 9)
        x[lo:hi + 1, max(0, f - 1):f + 2] = 0
        cache.refresh(lo, hi)


@needs_synth
def test_frame_times_match_the_upstream_formula():
    from arranger.adapters import _basic_pitch_notes as bp

    times = bp.model_frames_to_time(400)
    offset = (256 / 22050) * (172 - 43844 / 256) + 0.0018
    assert times[0] == 0.0
    assert times[171] == pytest.approx(171 * 256 / 22050)
    assert times[172] == pytest.approx(172 * 256 / 22050 - offset)
    assert times[399] == pytest.approx(399 * 256 / 22050 - 2 * offset)


# --------------------------------------------------------------------------
# Tempo and warnings
# --------------------------------------------------------------------------


@needs_model
def test_steady_tempo_is_found_and_sets_the_bars(scale_result):
    r = scale_result
    assert r.tempo_bpm == pytest.approx(120.0, abs=3.0)
    assert r.tempo_confidence >= 0.5
    assert r.score.tempo_bpm == r.tempo_bpm == r.score.timeline.initial_bpm
    assert any("half or double" in w for w in r.warnings)
    # 0.25 s lead-in then one note per beat in 4/4: notes 0-3 in bar 1, 4-7 in bar 2.
    assert [n.bar for n in r.score.notes] == [1, 1, 1, 1, 2, 2, 2, 2]


@needs_model
def test_irregular_onsets_get_no_tempo_and_say_so():
    rng = np.random.default_rng(3)
    onsets = np.cumsum(rng.uniform(0.18, 0.9, size=14))
    notes = [(int(rng.integers(55, 80)), float(t), 0.3, 96) for t in onsets]
    r = A.transcribe_audio(synth.to_bytes(synth.synth(notes)))
    assert r.tempo_bpm is None and r.tempo_confidence < 0.5
    assert r.score.tempo_bpm == 120.0 and r.score.timeline.initial_bpm == 120.0
    assert any("not meaningful until you set the tempo" in w for w in r.warnings)
    assert all(n.beat == pytest.approx(n.onset * 2.0, abs=1e-4) for n in r.score.notes)

    off = A.transcribe_audio(synth.wav_bytes(SCALE), options=A.TranscriptionOptions(estimate_tempo=False))
    assert off.tempo_bpm is None and off.tempo_confidence is None and off.score.tempo_bpm == 120.0


@needs_model
def test_warnings_for_dense_chords_clipping_and_quiet_audio():
    chord = [(p, 0.3, 1.5, 90) for p in (36, 43, 48, 52, 55, 60, 64, 67, 72)]
    dense = A.transcribe_audio(synth.wav_bytes(chord))
    assert dense.max_polyphony > 6 and any("notes sound at once" in w for w in dense.warnings)

    clean = synth.synth(SCALE)
    clipped = A.transcribe_audio(synth.to_bytes(np.clip(clean * 8, -1, 1)))
    assert any("clipped" in w for w in clipped.warnings)
    quiet = A.transcribe_audio(synth.to_bytes(clean * 0.004, subtype="FLOAT"))
    assert any("very quiet" in w for w in quiet.warnings)

    plain = A.transcribe_audio(synth.to_bytes(clean))
    assert not any(k in w for w in plain.warnings for k in ("clipped", "quiet", "at once", "ended early"))


@needs_model
def test_a_file_that_ends_early_is_transcribed_with_a_warning_or_refused():
    import soundfile as sf

    if "MP3" not in sf.available_formats():
        pytest.skip("this libsndfile build has no MP3 support")
    data = synth.to_bytes(synth.synth(SCALE), fmt="MP3", subtype=None)
    try:
        r = A.transcribe_audio(data[: int(len(data) * 0.6)], filename="cut.mp3")
    except (MalformedFile, EmptyScore):
        return
    assert any("ended early" in w for w in r.warnings)
    assert r.duration_seconds < 0.9 * (len(synth.synth(SCALE)) / 22050)
    assert 1 <= len(r.score.notes) < len(SCALE)


@needs_model
def test_pitch_range_and_option_validation():
    r = A.transcribe_audio(synth.wav_bytes(SCALE), options=A.TranscriptionOptions(min_pitch=64, max_pitch=69))
    assert sorted({n.pitch for n in r.score.notes}) == [64, 65, 67, 69]
    with pytest.raises(ValueError):
        A.transcribe_audio(synth.wav_bytes(SCALE), options=A.TranscriptionOptions(onset_threshold=0.0))
    with pytest.raises(ValueError):
        A.transcribe_audio(synth.wav_bytes(SCALE), options=A.TranscriptionOptions(min_pitch=90, max_pitch=40))


@needs_synth
def test_probe_reports_the_header_without_decoding():
    stereo = np.zeros((48000, 2), dtype=np.float32)
    info = A.probe_audio(synth.to_bytes(stereo, sr=48000), filename="x.bin")
    assert (info.format, info.channels, info.sample_rate, info.frames) == ("WAV", 2, 48000, 48000)
    assert info.duration_seconds == pytest.approx(1.0) and info.subtype == "PCM_16"
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, np.zeros(8000, dtype=np.float32), 8000, format="AU")
    with pytest.raises(UnsupportedFormat):
        A.probe_audio(buf.getvalue(), filename="x.au")
    with pytest.raises(UnsupportedFormat):
        A.probe_audio(synth.to_bytes(np.zeros(8000, dtype=np.float32), fmt="W64", subtype="PCM_16"), filename="x.wav")
    with pytest.raises(UnsupportedFormat):
        A.probe_audio(b"MThd\x00\x00\x00\x06" + b"\x00" * 64, filename="song.wav")
    with pytest.raises(UnsupportedFormat) as err:
        A.probe_audio(b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 64, filename="song.m4a")
    assert "M4A" in err.value.public and "convert" in err.value.public
