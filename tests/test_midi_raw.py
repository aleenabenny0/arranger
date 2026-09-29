"""The raw view of a MIDI file that the cross-language diff compares.

`read_midi_raw` shares its chunk walk with `read_midi_bytes`, so the two
cannot disagree about which events a file holds; these tests pin what the
raw view shows and that it refuses what the reader refuses.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.io import MidiError, MidiUnsupported, read_midi_bytes, read_midi_raw  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.limits import ImportLimits, LimitExceeded  # noqa: E402


def varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def track(*events: bytes) -> bytes:
    body = b"".join(events) + b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + body


def header(fmt: int, tracks: int, division: int = 480) -> bytes:
    return b"MThd" + struct.pack(">IHHH", 6, fmt, tracks, division)


def test_raw_view_lists_notes_in_ticks_with_the_metadata():
    data = header(0, 1) + track(
        b"\x00\xff\x03\x04Tune",
        b"\x00\xff\x51\x03\x07\xa1\x20",          # 120 bpm
        b"\x00\xff\x58\x04\x03\x02\x18\x08",      # 3/4
        b"\x00\xff\x59\x02\xfd\x01",              # three flats, minor
        b"\x00\xc0\x05",                          # program 5 on channel 0
        b"\x00\xb0\x40\x7f",                      # pedal down
        b"\x00\x90\x3c\x64",                      # C4 on
        varlen(480) + b"\x80\x3c\x40",            # C4 off after a quarter
        b"\x00\x90\x3e\x50",                      # D4 on, never closed
    )
    raw = read_midi_raw(data)
    assert raw["format"] == 0 and raw["division"] == 480 and raw["declared_tracks"] == 1
    assert raw["truncated"] is False
    (t,) = raw["tracks"]
    assert t["name"] == "Tune"
    assert t["notes"] == [[0, 480, 60, 0, 100], [480, 480 + 0, 62, 0, 80]] or t["notes"] == [[0, 480, 60, 0, 100]]
    # The D4 is held until the end of the track, which is the same tick it started on, so it is dropped.
    assert t["notes"] == [[0, 480, 60, 0, 100]]
    assert t["unclosed"] == 0
    assert t["end_tick"] == 480
    assert t["events"] == 10
    assert t["tempos"] == [[0, 500000]]
    assert t["time_signatures"] == [[0, 3, 4]]
    assert t["key_signatures"] == [[0, -3, "minor"]]
    assert t["programs"] == [[0, 5]]
    assert t["pedal"] == [[0, 0, True]]


def test_raw_view_agrees_with_the_score_reader_on_a_written_file():
    score = Score(
        notes=[Note(pitch=60 + i, onset=i * 0.5, duration=0.4, staff=1) for i in range(8)]
        + [Note(pitch=48, onset=0.0, duration=2.0, staff=2)],
        tempo_bpm=120.0,
        title="Round trip",
    )
    data = write_midi(score)
    raw = read_midi_raw(data)
    parsed = read_midi_bytes(data)
    assert sum(len(t["notes"]) for t in raw["tracks"]) == len(parsed.notes)
    assert {n[2] for t in raw["tracks"] for n in t["notes"]} == {n.pitch for n in parsed.notes}
    assert [t["tempos"] for t in raw["tracks"]][0] == [[0, 500000]]


def test_raw_view_marks_a_truncated_chunk_and_raises_what_the_reader_raises():
    data = header(0, 1) + track(b"\x00\x90\x3c\x64", varlen(480) + b"\x80\x3c\x40")
    assert read_midi_raw(data[:-4])["truncated"] is True         # the end-of-track event is missing
    with pytest.raises(MidiError):
        read_midi_raw(data[:-6])                                   # cut inside the note-off
    with pytest.raises(MidiError):
        read_midi_raw(b"RIFF" + data[4:])
    with pytest.raises(MidiUnsupported):
        read_midi_raw(header(3, 1) + track(b"\x00\x90\x3c\x64", b"\x00\x80\x3c\x40"))
    with pytest.raises(LimitExceeded):
        read_midi_raw(data, limits=ImportLimits(max_events=2))
