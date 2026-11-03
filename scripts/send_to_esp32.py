"""Play a piece on the ESP32 player (firmware/esp32-player) over a serial port.

    python scripts/send_to_esp32.py PIECE.mid --port COM3          # Windows
    python scripts/send_to_esp32.py PIECE.mid --port /dev/ttyUSB0  # Linux
    python scripts/send_to_esp32.py PIECE.mid --dry-run            # print the frames; no device needed
    python scripts/send_to_esp32.py --port COM3 --tone 69          # a wiring check: A4 for a second

The host keeps time. The piece is read with the project's own readers (MIDI,
MusicXML or the score JSON), turned into timed NOTE_ON and NOTE_OFF frames,
and streamed in real time; the device sounds the highest held note on its
piezo and acknowledges every frame. `--melody-only` sends the upper staff (or
the highest note of each chord when the file has no staves), which is what a
one-voice sounder can carry.

A real port needs pyserial: pip install -e ".[esp32]". The frame format is
documented in firmware/esp32-player/components/frameproto/frameproto.h; the
encoder and decoder here are its Python twins, checked against the same golden
bytes in tests/test_esp32_sender.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arranger.ir import Score  # noqa: E402

SYNC1, SYNC2 = 0xA5, 0x5A
MAX_PAYLOAD = 32
NOTE_ON, NOTE_OFF, ALL_OFF, PING, TONE_TEST = 0x01, 0x02, 0x03, 0x04, 0x05
ACK, PONG, ERROR = 0x81, 0x84, 0x7F
TYPE_NAMES = {NOTE_ON: "NOTE_ON", NOTE_OFF: "NOTE_OFF", ALL_OFF: "ALL_OFF", PING: "PING", TONE_TEST: "TONE_TEST",
              ACK: "ACK", PONG: "PONG", ERROR: "ERROR"}


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, no reflection, no final XOR: "123456789" gives 0xF4."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode(frame_type: int, payload: bytes = b"") -> bytes:
    if not 0 <= frame_type <= 255:
        raise ValueError("frame type must fit in one byte")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload longer than {MAX_PAYLOAD} bytes")
    body = bytes([frame_type, len(payload)]) + payload
    return bytes([SYNC1, SYNC2]) + body + bytes([crc8(body)])


@dataclass
class Frame:
    type: int
    payload: bytes

    def __str__(self) -> str:
        return f"{TYPE_NAMES.get(self.type, f'0x{self.type:02X}')} {self.payload.hex(' ')}".rstrip()


class FrameParser:
    """The receiver's state machine, the same one the firmware runs."""

    def __init__(self) -> None:
        self.state = "sync1"
        self.type = 0
        self.length = 0
        self.payload = bytearray()
        self.frames = 0
        self.crc_errors = 0
        self.length_errors = 0
        self.resyncs = 0

    def feed(self, byte: int) -> Frame | None:
        if self.state == "sync1":
            if byte == SYNC1:
                self.state = "sync2"
            else:
                self.resyncs += 1
        elif self.state == "sync2":
            if byte == SYNC2:
                self.state = "type"
            elif byte == SYNC1:
                self.resyncs += 1
            else:
                self.resyncs += 2
                self.state = "sync1"
        elif self.state == "type":
            self.type = byte
            self.state = "length"
        elif self.state == "length":
            if byte > MAX_PAYLOAD:
                self.length_errors += 1
                self.state = "sync1"
                return None
            self.length = byte
            self.payload = bytearray()
            self.state = "payload" if byte else "crc"
        elif self.state == "payload":
            self.payload.append(byte)
            if len(self.payload) >= self.length:
                self.state = "crc"
        elif self.state == "crc":
            self.state = "sync1"
            if byte != crc8(bytes([self.type, self.length]) + bytes(self.payload)):
                self.crc_errors += 1
                return None
            self.frames += 1
            return Frame(self.type, bytes(self.payload))
        return None

    def feed_all(self, data: bytes) -> list[Frame]:
        out = []
        for byte in data:
            frame = self.feed(byte)
            if frame is not None:
                out.append(frame)
        return out


# --- from a piece to timed frames --------------------------------------------


@dataclass(order=True)
class Event:
    time: float
    order: int      # note-offs before note-ons at the same instant
    frame: bytes


def melody_notes(score: Score) -> list:
    """The upper staff when the file has staves, else the top note of each onset."""
    notes = [n for n in score.notes if n.staff == 1]
    if notes:
        return notes
    by_onset: dict[float, list] = {}
    for note in score.notes:
        by_onset.setdefault(round(note.onset, 4), []).append(note)
    return [max(group, key=lambda n: n.pitch) for group in by_onset.values()]


def events_for(score: Score, *, melody_only: bool = False, speed: float = 1.0) -> list[Event]:
    if speed <= 0:
        raise ValueError("speed must be positive")
    notes = melody_notes(score) if melody_only else list(score.notes)
    events: list[Event] = []
    for note in notes:
        if not 0 <= note.pitch <= 127:
            continue
        velocity = max(1, min(127, int(note.velocity or 80)))
        start = note.onset / speed
        end = (note.onset + max(note.duration, 0.02)) / speed
        events.append(Event(start, 1, encode(NOTE_ON, bytes([note.pitch, velocity]))))
        events.append(Event(end, 0, encode(NOTE_OFF, bytes([note.pitch]))))
    events.sort()
    return events


def load_score(path: Path) -> Score:
    suffix = path.suffix.lower()
    if suffix in (".mid", ".midi"):
        from arranger.io import read_midi
        return read_midi(path)
    if suffix in (".musicxml", ".xml", ".mxl"):
        from arranger.adapters.musicxml_reader import read_musicxml
        return read_musicxml(path)
    if suffix == ".json":
        import json

        from arranger.adapters.score_json import score_from_dict
        return score_from_dict(json.loads(path.read_text(encoding="utf-8")))
    raise SystemExit(f"{path}: not a MIDI, MusicXML or score JSON file")


# --- the link ------------------------------------------------------------------


class Link:
    """A serial port with the reply parser attached."""

    def __init__(self, port: str, baud: int) -> None:
        try:
            import serial  # pyserial
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise SystemExit('pyserial is not installed: pip install -e ".[esp32]"') from error
        self.serial = serial.Serial(port, baud, timeout=0, write_timeout=2)
        self.parser = FrameParser()
        self.replies: list[Frame] = []

    def send(self, frame: bytes) -> None:
        self.serial.write(frame)

    def poll(self) -> list[Frame]:
        waiting = self.serial.in_waiting
        if not waiting:
            return []
        frames = self.parser.feed_all(self.serial.read(waiting))
        self.replies.extend(frames)
        return frames

    def wait_for(self, frame_type: int, timeout: float) -> Frame | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.poll():
                if frame.type == frame_type:
                    return frame
            time.sleep(0.01)
        return None

    def close(self) -> None:
        self.serial.close()


def stream(link: Link, events: list[Event], *, sleep=time.sleep, clock=time.perf_counter) -> dict:
    """Send the events on time. Returns counts of what was sent and answered."""
    started = clock()
    sent = 0
    for event in events:
        while True:
            lag = event.time - (clock() - started)
            if lag <= 0:
                break
            sleep(min(lag, 0.01))
            link.poll()
        link.send(event.frame)
        sent += 1
    link.send(encode(ALL_OFF))
    sent += 1
    link.wait_for(ACK, 0.5)
    acks = sum(1 for f in link.replies if f.type == ACK and f.payload[1:2] == b"\x00")
    refused = sum(1 for f in link.replies if f.type == ACK and f.payload[1:2] != b"\x00")
    errors = sum(1 for f in link.replies if f.type == ERROR)
    return {"sent": sent, "acknowledged": acks, "refused": refused, "errors": errors,
            "crc_errors_here": link.parser.crc_errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("piece", nargs="?", type=Path, help="a MIDI, MusicXML or score JSON file")
    parser.add_argument("--port", help="serial port, for example COM3 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--melody-only", action="store_true", help="send the upper staff or the top note of each chord")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed; 0.5 is half tempo")
    parser.add_argument("--dry-run", action="store_true", help="print the timed frames instead of sending them")
    parser.add_argument("--tone", type=int, help="play this MIDI pitch for a second and exit (wiring check)")
    args = parser.parse_args(argv)

    if args.tone is not None:
        if not 0 <= args.tone <= 127:
            print("--tone takes a MIDI pitch from 0 to 127", file=sys.stderr)
            return 2
        frame = encode(TONE_TEST, bytes([args.tone, 1000 & 0xFF, 1000 >> 8]))
        if args.dry_run or not args.port:
            print(f"t=0.000 {frame.hex(' ')}  TONE_TEST {args.tone} 1000ms")
            return 0
        link = Link(args.port, args.baud)
        try:
            link.send(encode(PING))
            if link.wait_for(PONG, 2.0) is None:
                print("no PONG from the device; check the port and the baud rate", file=sys.stderr)
                return 1
            link.send(frame)
            print("acknowledged" if link.wait_for(ACK, 2.0) else "no acknowledgement")
        finally:
            link.close()
        return 0

    if args.piece is None:
        parser.error("give a piece to play, or --tone PITCH")
    score = load_score(args.piece)
    events = events_for(score, melody_only=args.melody_only, speed=args.speed)
    if not events:
        print("the piece has no notes to send", file=sys.stderr)
        return 1

    if args.dry_run or not args.port:
        if not args.dry_run:
            print("no --port given: printing the frames instead", file=sys.stderr)
        for event in events:
            frame = FrameParser().feed_all(event.frame)[0]
            print(f"t={event.time:8.3f} {event.frame.hex(' ')}  {frame}")
        print(f"{len(events)} frames over {events[-1].time:.1f} s", file=sys.stderr)
        return 0

    link = Link(args.port, args.baud)
    try:
        link.send(encode(PING))
        pong = link.wait_for(PONG, 2.0)
        if pong is None:
            print("no PONG from the device; check the port and the baud rate", file=sys.stderr)
            return 1
        print(f"device answered: protocol {pong.payload[0]}, firmware {pong.payload[1]}.{pong.payload[2]}")
        print(f"playing {args.piece.name}: {len(events) // 2} notes over {events[-1].time:.1f} s")
        summary = stream(link, events)
    finally:
        try:
            link.send(encode(ALL_OFF))
        finally:
            link.close()
    print(", ".join(f"{key} {value}" for key, value in summary.items()))
    return 0 if summary["errors"] == 0 and summary["refused"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
