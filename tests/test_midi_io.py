"""MIDI reading, writing, and round trips.

Edge-case files are built byte by byte here rather than checked in, so each
test shows exactly which bytes it is about.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.adapters.score_json import score_from_dict, score_to_dict  # noqa: E402
from arranger.io import MidiError, MidiUnsupported, read_midi_bytes  # noqa: E402
from arranger.ir import Note, PedalSpan, Score  # noqa: E402
from arranger.limits import (  # noqa: E402
    EmptyScore,
    ImportLimits,
    LimitExceeded,
    MalformedFile,
    ScoreImportError,
)
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402

PPQ = 480


# --- byte-level builders --------------------------------------------------


def varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def chunk(events: bytes) -> bytes:
    body = events + b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + body


def header(fmt: int, tracks: int, division: int = PPQ) -> bytes:
    return b"MThd" + struct.pack(">IHHH", 6, fmt, tracks, division)


def on(delta: int, pitch: int, velocity: int = 80, channel: int = 0) -> bytes:
    return varlen(delta) + bytes([0x90 | channel, pitch, velocity])


def off(delta: int, pitch: int, channel: int = 0) -> bytes:
    return varlen(delta) + bytes([0x80 | channel, pitch, 0])


def tempo(delta: int, bpm: float) -> bytes:
    return varlen(delta) + b"\xff\x51\x03" + round(60_000_000 / bpm).to_bytes(3, "big")


def time_sig(delta: int, num: int, den_power: int) -> bytes:
    return varlen(delta) + b"\xff\x58\x04" + bytes([num, den_power, 24, 8])


def simple_file(events: bytes, fmt: int = 0) -> bytes:
    return header(fmt, 1) + chunk(events)


# --- parsing semantics ----------------------------------------------------


def test_reads_pitch_timing_and_velocity():
    data = simple_file(tempo(0, 120) + on(0, 60, 90) + off(PPQ, 60) + on(0, 64, 50) + off(PPQ * 2, 64))
    score = read_midi_bytes(data, filename="two_notes.mid")

    assert score.title == "two notes"
    assert [(n.pitch, n.velocity) for n in score.notes] == [(60, 90), (64, 50)]
    assert score.notes[0].onset == 0.0 and score.notes[0].duration == pytest.approx(0.5)
    assert score.notes[1].onset == pytest.approx(0.5) and score.notes[1].duration == pytest.approx(1.0)
    assert score.notes[1].beat == 1.0 and score.notes[1].beats == 2.0
    assert score.source_format == "midi"
    assert all(n.id for n in score.notes) and len({n.id for n in score.notes}) == 2


def test_tempo_change_shifts_later_onsets_only():
    # One beat at 120 (0.5 s), then the tempo halves: the next beat takes 1.0 s.
    data = simple_file(
        tempo(0, 120) + on(0, 60) + off(PPQ, 60) + tempo(0, 60) + on(0, 62) + off(PPQ, 62)
        + on(0, 64) + off(PPQ, 64)
    )
    score = read_midi_bytes(data)

    assert [round(n.onset, 6) for n in score.notes] == [0.0, 0.5, 1.5]
    assert [round(n.duration, 6) for n in score.notes] == [0.5, 1.0, 1.0]
    assert score.timeline.has_tempo_changes
    assert score.tempo_bpm == pytest.approx(120)


def test_meter_change_moves_barlines():
    # Two bars of 4/4, then 3/4. A note on every beat.
    events = time_sig(0, 4, 2)
    for _ in range(8):
        events += on(0, 60) + off(PPQ, 60)
    events += time_sig(0, 3, 2)
    for _ in range(6):
        events += on(0, 62) + off(PPQ, 62)
    score = read_midi_bytes(simple_file(events))

    bars = [n.bar for n in score.notes]
    assert bars == [1] * 4 + [2] * 4 + [3] * 3 + [4] * 3
    assert score.timeline.meter_at_bar(3) == (3, 4)
    assert all(n.bar == score.timeline.bar_at(n.beat) for n in score.notes)


def test_mid_bar_meter_change_is_moved_and_reported():
    events = time_sig(0, 4, 2) + on(0, 60) + off(PPQ * 2, 60) + time_sig(0, 3, 2) + on(0, 62) + off(PPQ, 62)
    score = read_midi_bytes(simple_file(events))
    assert any("moved to the next barline" in w for w in score.warnings)


def test_running_status_and_note_on_velocity_zero():
    # Second and third events omit the status byte; velocity 0 ends the note.
    events = varlen(0) + bytes([0x90, 60, 80]) + varlen(PPQ) + bytes([60, 0]) + varlen(0) + bytes([64, 70])
    events += varlen(PPQ) + bytes([64, 0])
    score = read_midi_bytes(simple_file(events))
    assert [(n.pitch, round(n.beat, 3), round(n.beats, 3)) for n in score.notes] == [
        (60, 0.0, 1.0), (64, 1.0, 1.0)
    ]


def test_running_status_survives_a_meta_event():
    events = varlen(0) + bytes([0x90, 60, 80]) + tempo(PPQ, 100) + varlen(0) + bytes([60, 0])
    score = read_midi_bytes(simple_file(events))
    assert len(score.notes) == 1 and score.notes[0].beats == 1.0


def test_simultaneous_notes_and_repeated_pitches():
    chord = on(0, 60) + on(0, 64) + on(0, 67) + off(PPQ, 60) + off(0, 64) + off(0, 67)
    repeat = on(0, 60) + off(PPQ // 2, 60) + on(0, 60) + off(PPQ // 2, 60)
    score = read_midi_bytes(simple_file(chord + repeat))
    assert [n.pitch for n in score.notes[:3]] == [60, 64, 67]
    assert [round(n.beat, 3) for n in score.notes[3:]] == [1.0, 1.5]


def test_overlapping_same_pitch_is_paired_first_in_first_out():
    events = on(0, 60) + on(PPQ, 60) + off(PPQ, 60) + off(PPQ, 60)
    score = read_midi_bytes(simple_file(events))
    assert [(n.beat, n.beats) for n in score.notes] == [(0.0, 2.0), (1.0, 2.0)]


def test_percussion_is_filtered_and_reported():
    events = on(0, 60) + on(0, 36, channel=9) + off(PPQ, 60) + off(0, 36, channel=9)
    score = read_midi_bytes(simple_file(events))
    assert [n.pitch for n in score.notes] == [60]
    assert any("percussion" in w for w in score.warnings)

    with_drums = read_midi_bytes(simple_file(events), include_drums=True)
    assert sorted(n.pitch for n in with_drums.notes) == [36, 60]


def test_percussion_only_file_is_empty():
    events = on(0, 36, channel=9) + off(PPQ, 36, channel=9)
    with pytest.raises(EmptyScore) as info:
        read_midi_bytes(simple_file(events))
    assert "pitched" in info.value.public


def test_format_zero_channels_become_separate_tracks():
    events = on(0, 72, channel=0) + on(0, 48, channel=1) + off(PPQ, 72, channel=0) + off(0, 48, channel=1)
    score = read_midi_bytes(simple_file(events))
    assert len(score.tracks) == 2
    # Exactly two parts: the higher one is the right hand.
    by_pitch = {n.pitch: n.staff for n in score.notes}
    assert by_pitch == {72: 1, 48: 2}


def test_doubled_layers_collapse_to_one_note():
    track_a = chunk(on(0, 60, 60) + off(PPQ, 60))
    track_b = chunk(on(0, 60, 100) + off(PPQ * 2, 60))
    score = read_midi_bytes(header(1, 2) + track_a + track_b)
    assert len(score.notes) == 1
    assert score.notes[0].velocity == 100 and score.notes[0].beats == 2.0
    assert any("doubled" in w for w in score.warnings)


def test_sustain_pedal_becomes_spans():
    events = (
        tempo(0, 120)
        + varlen(0) + bytes([0xB0, 64, 127])
        + on(0, 60) + off(PPQ, 60)
        + varlen(PPQ) + bytes([0xB0, 64, 0])
        + on(0, 62) + off(PPQ, 62)
    )
    score = read_midi_bytes(simple_file(events))
    assert score.pedals == [PedalSpan(0.0, 1.0)]
    assert score.pedal_down_at(0.75) and not score.pedal_down_at(1.25)


def test_key_signature_and_track_metadata():
    name = b"Melody"
    events = (
        varlen(0) + b"\xff\x03" + varlen(len(name)) + name
        + varlen(0) + b"\xff\x59\x02" + struct.pack("b", -3) + b"\x01"
        + varlen(0) + bytes([0xC0, 40])
        + on(0, 67) + off(PPQ, 67)
    )
    score = read_midi_bytes(simple_file(events))
    key = score.timeline.key_at(0)
    assert (key.fifths, key.mode) == (-3, "minor")
    assert score.tracks[0].name == "Melody" and score.tracks[0].program == 40


def test_missing_metadata_is_stated_not_hidden():
    score = read_midi_bytes(simple_file(on(0, 60) + off(PPQ, 60)))
    assert score.tempo_bpm == pytest.approx(120)
    assert any("No tempo" in w for w in score.warnings)
    assert any("No time signature" in w for w in score.warnings)


def test_unterminated_note_is_kept_and_reported():
    score = read_midi_bytes(simple_file(on(0, 60) + on(PPQ, 64) + off(PPQ, 64)))
    assert sorted(n.pitch for n in score.notes) == [60, 64]
    assert any("never ended" in w for w in score.warnings)


# --- malformed and hostile input -----------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"", b"MThd", b"not a midi file at all", b"RIFF" + b"\x00" * 40],
    ids=["empty", "short", "text", "wrong-magic"],
)
def test_non_midi_is_rejected(data):
    with pytest.raises(MidiError):
        read_midi_bytes(data)


def test_smpte_division_is_unsupported_not_malformed():
    data = header(0, 1, division=0xE728) + chunk(on(0, 60) + off(10, 60))
    with pytest.raises(MidiUnsupported) as info:
        read_midi_bytes(data)
    assert info.value.code == "unsupported_midi"
    assert isinstance(info.value, ScoreImportError)


def test_zero_division_and_no_tracks():
    with pytest.raises(MidiError):
        read_midi_bytes(header(0, 1, division=0) + chunk(on(0, 60) + off(10, 60)))
    with pytest.raises(MidiError):
        read_midi_bytes(header(1, 0) + b"")


def test_truncated_event_stream_raises_cleanly():
    body = on(0, 60)[:-1]  # note-on missing its velocity byte
    data = header(0, 1) + b"MTrk" + struct.pack(">I", len(body)) + body
    with pytest.raises(MidiError):
        read_midi_bytes(data)


def test_chunk_length_larger_than_file_is_bounded():
    body = on(0, 60) + off(PPQ, 60) + b"\x00\xff\x2f\x00"
    data = header(0, 1) + b"MTrk" + struct.pack(">I", 0x7FFFFFFF) + body
    score = read_midi_bytes(data)
    assert len(score.notes) == 1
    assert any("truncated" in w for w in score.warnings)


def test_overlong_variable_length_quantity():
    data = simple_file(b"\xff\xff\xff\xff\x7f" + bytes([0x90, 60, 80]))
    with pytest.raises(MidiError):
        read_midi_bytes(data)


def test_limits_are_enforced_before_the_work_is_done():
    many = b"".join(on(0, 60 + (i % 12)) + off(10, 60 + (i % 12)) for i in range(500))
    data = simple_file(many)

    with pytest.raises(LimitExceeded):
        read_midi_bytes(data, limits=ImportLimits(max_notes=100))
    with pytest.raises(LimitExceeded):
        read_midi_bytes(data, limits=ImportLimits(max_events=200))
    with pytest.raises(LimitExceeded):
        read_midi_bytes(data, limits=ImportLimits(max_bytes=64))
    with pytest.raises(LimitExceeded):
        read_midi_bytes(header(1, 500) + chunk(on(0, 60) + off(10, 60)), limits=ImportLimits(max_tracks=64))
    # Near miss: the same file is fine under the default limits.
    assert len(read_midi_bytes(data).notes) == 500


def test_duration_and_bar_limits():
    long_note = tempo(0, 20) + on(0, 60) + off(0x0FFFFFFF, 60)
    with pytest.raises(LimitExceeded):
        read_midi_bytes(simple_file(long_note))

    far = tempo(0, 960) + time_sig(0, 1, 5) + on(PPQ * 600, 60) + off(PPQ, 60)
    with pytest.raises(LimitExceeded):
        read_midi_bytes(simple_file(far), limits=ImportLimits(max_bars=1000))


def test_public_messages_never_contain_parser_internals():
    with pytest.raises(MidiError) as info:
        read_midi_bytes(simple_file(b"\xff\xff\xff\xff\x7f"))
    assert "variable-length" not in info.value.public
    assert "variable-length" in info.value.detail


# --- writing and round trips ---------------------------------------------


def make_score() -> Score:
    timeline = Timeline(
        [TempoChange(0, 96), TempoChange(8, 72)],
        [MeterChange(1, 4, 4), MeterChange(3, 3, 4)],
        [KeyChange(0, 2, "major")],
    )
    rows = [(60, 0.0, 1.0, 1), (64, 1.0, 0.5, 1), (67, 1.5, 0.5, 1), (48, 0.0, 4.0, 2),
            (72, 8.0, 3.0, 1), (43, 8.0, 3.0, 2)]
    notes = []
    for i, (pitch, beat, beats, staff) in enumerate(rows):
        onset = timeline.seconds_at(beat)
        notes.append(
            Note(pitch=pitch, onset=onset, duration=timeline.seconds_at(beat + beats) - onset,
                 staff=staff, bar=timeline.bar_at(beat), id=f"n{i}", velocity=60 + i * 5,
                 beat=beat, beats=beats)
        )
    return Score(notes=notes, tempo_bpm=96, title="Round Trip", timeline=timeline,
                 pedals=[PedalSpan(0.0, timeline.seconds_at(4.0))])


def test_write_then_read_preserves_the_music():
    original = make_score()
    back = read_midi_bytes(write_midi(original), filename="Round Trip.mid")

    def key(n):
        return (n.pitch, round(n.beat, 6), round(n.beats, 6), n.velocity, n.staff, n.bar)

    assert sorted(map(key, back.notes)) == sorted(map(key, original.notes))
    # Beats are exact. Seconds are not quite: MIDI stores tempo as whole
    # microseconds per beat, and 72 bpm is 833333.33, so a few parts per
    # million of drift is the format, not the writer.
    for a, b in zip(sorted(original.notes, key=key), sorted(back.notes, key=key), strict=True):
        assert b.onset == pytest.approx(a.onset, rel=1e-5, abs=1e-6)
        assert b.duration == pytest.approx(a.duration, rel=1e-5, abs=1e-6)
    assert [(t.beat, round(t.bpm, 3)) for t in back.timeline.tempos] == [(0.0, 96.0), (8.0, 72.0)]
    assert back.timeline.meter_at_bar(3) == (3, 4)
    assert back.timeline.key_at(0).fifths == 2
    assert back.pedals[0].start == pytest.approx(0.0) and back.pedals[0].end == pytest.approx(2.5)
    assert back.title == "Round Trip"


def test_pickup_bar_survives_a_midi_round_trip():
    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 3, 4)], pickup_beats=1.0)
    notes = [
        Note(pitch=67, onset=0.0, duration=0.5, beat=0.0, beats=1.0, bar=1, staff=1),
        Note(pitch=72, onset=0.5, duration=1.5, beat=1.0, beats=3.0, bar=2, staff=1),
        Note(pitch=74, onset=2.0, duration=1.5, beat=4.0, beats=3.0, bar=3, staff=1),
    ]
    back = read_midi_bytes(write_midi(Score(notes=notes, timeline=timeline, tempo_bpm=120)))
    assert back.timeline.pickup_beats == 1.0
    assert [n.bar for n in back.notes] == [1, 2, 3]
    assert back.timeline.meter_at_bar(2) == (3, 4)


def test_writer_places_notes_without_beats_using_the_tempo_map():
    score = Score.from_tuples([(60, 0.0, 0.5, 1), (62, 0.5, 0.5, 1)], tempo_bpm=120)
    back = read_midi_bytes(write_midi(score))
    assert [(n.pitch, n.beat, n.beats) for n in back.notes] == [(60, 0.0, 1.0), (62, 1.0, 1.0)]


def test_writer_does_not_let_a_restruck_pitch_cut_itself_off():
    notes = [
        Note(pitch=60, onset=0.0, duration=2.0, beat=0.0, beats=4.0, staff=1),
        Note(pitch=60, onset=0.5, duration=0.5, beat=1.0, beats=1.0, staff=1),
    ]
    back = read_midi_bytes(write_midi(Score(notes=notes, tempo_bpm=120)))
    assert [(n.beat, n.beats) for n in back.notes] == [(0.0, 1.0), (1.0, 1.0)]


def test_corpus_style_long_piece_round_trips_quickly():
    import time

    notes = []
    for i in range(20_000):
        beat = i * 0.25
        notes.append(Note(pitch=48 + (i * 7) % 36, onset=beat * 0.5, duration=0.12,
                          beat=beat, beats=0.25, staff=1 if i % 2 else 2))
    started = time.perf_counter()
    back = read_midi_bytes(write_midi(Score(notes=notes, tempo_bpm=120)))
    assert len(back.notes) == 20_000
    assert time.perf_counter() - started < 5.0


# --- JSON codec ------------------------------------------------------------


def test_score_json_round_trips_every_field():
    original = make_score()
    original.notes[0] = Note(
        pitch=61, onset=0.0, duration=0.625, staff=1, bar=1, id="n0", velocity=70, beat=0.0,
        beats=1.0, track=0, source_id="t0n4", role="melody", spelling=("D", -1),
        articulations=("staccato",), dynamic="mf", rolled=True, confidence=0.75,
    )
    original.warnings.append("example warning")
    back = score_from_dict(score_to_dict(original))
    assert back.notes == sorted(original.notes, key=lambda n: (n.onset, n.pitch))
    assert back.timeline == original.timeline
    assert back.pedals == original.pedals and back.warnings == original.warnings


def test_legacy_minimal_json_still_loads():
    score = score_from_dict({"title": "old", "tempo_bpm": 76, "notes": [
        {"pitch": 60, "onset": 0, "duration": 1, "staff": 1, "bar": 1}]})
    assert score.title == "old" and score.notes[0].pitch == 60 and score.timeline is None


@pytest.mark.parametrize(
    "note",
    [
        {"pitch": "60", "onset": 0, "duration": 1},
        {"pitch": 60, "onset": -1, "duration": 1},
        {"pitch": 60, "onset": 0, "duration": 0},
        {"pitch": 200, "onset": 0, "duration": 1},
        {"pitch": 60, "onset": 0, "duration": 1, "bar": 10**9},
        {"pitch": 60, "onset": float("nan"), "duration": 1},
        {"pitch": True, "onset": 0, "duration": 1},
        {"pitch": 60, "onset": 0},
    ],
    ids=["string-pitch", "negative-onset", "zero-duration", "pitch-range", "huge-bar", "nan",
         "bool-pitch", "missing-duration"],
)
def test_score_json_rejects_bad_notes(note):
    with pytest.raises(MalformedFile):
        score_from_dict({"notes": [note]})


def test_score_json_note_limit():
    notes = [{"pitch": 60, "onset": i, "duration": 1} for i in range(11)]
    with pytest.raises(LimitExceeded):
        score_from_dict({"notes": notes}, max_notes=10)
    assert len(score_from_dict({"notes": notes[:10]}, max_notes=10).notes) == 10
