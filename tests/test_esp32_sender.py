"""The host side of the ESP32 player's protocol.

The frame bytes checked here are the same golden bytes the firmware's host
tests check (firmware/esp32-player/host/test_frameproto.c), so the Python
encoder and the C parser are pinned to one another.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "send_to_esp32.py"
spec = importlib.util.spec_from_file_location("send_to_esp32", SCRIPT)
sender = importlib.util.module_from_spec(spec)
sys.modules["send_to_esp32"] = sender   # dataclasses resolve string annotations through sys.modules
assert spec.loader is not None
spec.loader.exec_module(sender)


def test_crc8_matches_the_standard_check_value():
    assert sender.crc8(b"123456789") == 0xF4
    assert sender.crc8(b"") == 0


def test_frames_match_the_golden_bytes_the_firmware_tests_use():
    assert sender.encode(sender.NOTE_ON, bytes([60, 100])) == bytes([0xA5, 0x5A, 0x01, 0x02, 60, 100, 0xFE])
    assert sender.encode(sender.NOTE_OFF, bytes([60])) == bytes([0xA5, 0x5A, 0x02, 0x01, 60, 0x77])
    assert sender.encode(sender.PING) == bytes([0xA5, 0x5A, 0x04, 0x00, 0x54])
    assert sender.encode(sender.ALL_OFF) == bytes([0xA5, 0x5A, 0x03, 0x00, 0x3F])
    with pytest.raises(ValueError):
        sender.encode(sender.NOTE_ON, bytes(33))


def test_the_parser_round_trips_and_survives_noise():
    parser = sender.FrameParser()
    stream = b"boot text\r\n\xa5x" + sender.encode(sender.PONG, bytes([1, 0, 1])) + b"\x00\xa5\xa5" \
        + sender.encode(sender.ACK, bytes([sender.NOTE_ON, 0]))
    frames = parser.feed_all(stream)
    assert [(f.type, f.payload) for f in frames] == [(sender.PONG, bytes([1, 0, 1])), (sender.ACK, bytes([1, 0]))]
    assert parser.resyncs > 0 and parser.frames == 2
    # A wrong CRC drops that frame only.
    bad = bytearray(sender.encode(sender.NOTE_ON, bytes([60, 100])))
    bad[-1] ^= 0xFF
    assert parser.feed_all(bytes(bad)) == []
    assert parser.crc_errors == 1
    assert [f.type for f in parser.feed_all(sender.encode(sender.PING))] == [sender.PING]
    # A length over the maximum is refused before any payload is read.
    assert parser.feed_all(bytes([0xA5, 0x5A, 0x01, 33])) == []
    assert parser.length_errors == 1
    assert str(frames[0]) == "PONG 01 00 01"


def test_events_are_timed_and_ordered_with_offs_before_ons():
    score = Score(notes=[
        Note(pitch=60, onset=0.0, duration=0.5, staff=1, velocity=90),
        Note(pitch=48, onset=0.0, duration=1.0, staff=2, velocity=70),
        Note(pitch=62, onset=0.5, duration=0.5, staff=1, velocity=None),
    ], tempo_bpm=120.0)
    events = sender.events_for(score)
    assert [(round(e.time, 3), e.frame[2], e.frame[4]) for e in events] == [
        (0.0, sender.NOTE_ON, 48), (0.0, sender.NOTE_ON, 60),
        (0.5, sender.NOTE_OFF, 60), (0.5, sender.NOTE_ON, 62),
        (1.0, sender.NOTE_OFF, 48), (1.0, sender.NOTE_OFF, 62),
    ]
    assert events[1].frame[5] == 90            # velocity carried
    assert events[3].frame[5] == 80            # a note without velocity gets the default
    melody = sender.events_for(score, melody_only=True)
    assert {e.frame[4] for e in melody} == {60, 62}
    half = sender.events_for(score, speed=0.5)
    assert round(half[-1].time, 3) == 2.0
    with pytest.raises(ValueError):
        sender.events_for(score, speed=0)


def test_melody_only_takes_the_top_note_when_there_are_no_staves():
    score = Score(notes=[Note(pitch=60, onset=0.0, duration=0.5), Note(pitch=67, onset=0.0, duration=0.5),
                         Note(pitch=64, onset=0.5, duration=0.5)])
    assert [n.pitch for n in sender.melody_notes(score)] == [67, 64]


def test_dry_run_prints_the_frames_for_a_midi_file(tmp_path, capsys):
    score = Score(notes=[Note(pitch=60, onset=0.0, duration=0.5, staff=1), Note(pitch=64, onset=0.5, duration=0.5, staff=1)], tempo_bpm=120.0)
    path = tmp_path / "tune.mid"
    path.write_bytes(write_midi(score))
    assert sender.main([str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 4
    assert out[0].startswith("t=   0.000 a5 5a 01 02 3c") and out[0].endswith("NOTE_ON 3c 48")   # the writer stores velocity 72
    assert "NOTE_OFF 3c" in out[1] or "NOTE_OFF 3c" in out[2]
    # Without a port the frames are printed too, with a note on stderr.
    assert sender.main([str(path)]) == 0
    assert "no --port given" in capsys.readouterr().err


def test_tone_check_and_bad_inputs(tmp_path, capsys):
    assert sender.main(["--tone", "69", "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("t=0.000 a5 5a 05 03 45 e8 03")
    assert sender.main(["--tone", "200", "--dry-run"]) == 2
    with pytest.raises(SystemExit):
        sender.main([])
    junk = tmp_path / "notes.txt"
    junk.write_text("not music")
    with pytest.raises(SystemExit):
        sender.main([str(junk), "--dry-run"])


class FakeLink:
    """A device that acknowledges everything, for the streaming loop."""

    def __init__(self):
        self.sent = []
        self.replies = []
        self.parser = sender.FrameParser()

    def send(self, frame):
        self.sent.append(frame)
        decoded = sender.FrameParser().feed_all(frame)[0]
        self.replies.append(sender.Frame(sender.ACK, bytes([decoded.type, 0])))

    def poll(self):
        return []

    def wait_for(self, frame_type, timeout):
        return next((f for f in self.replies if f.type == frame_type), None)


def test_streaming_sends_every_event_in_order_and_ends_with_all_off():
    score = Score(notes=[Note(pitch=60 + i, onset=i * 0.01, duration=0.01, staff=1) for i in range(5)])
    events = sender.events_for(score)
    link = FakeLink()
    clock = iter(x * 0.001 for x in range(100000))
    summary = sender.stream(link, events, sleep=lambda s: None, clock=lambda: next(clock))
    assert summary == {"sent": 11, "acknowledged": 11, "refused": 0, "errors": 0, "crc_errors_here": 0}
    assert link.sent[:-1] == [e.frame for e in events]
    assert link.sent[-1] == sender.encode(sender.ALL_OFF)
