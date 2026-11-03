"""Write a Score as a Standard MIDI File.

Format 1: a conductor track carrying tempo, meter and key, then one track per
hand. Positions come from each note's beat when it has one, because beats are
exact and seconds are not; a note that only knows its seconds is placed by
inverting the tempo map.

Stdlib only.
"""

from __future__ import annotations

import struct

from ..ir import Note, Score
from ..timeline import Timeline

PPQ = 480
_ACOUSTIC_GRAND = 0


def _varlen(value: int) -> bytes:
    value = max(0, int(value))
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + _varlen(len(payload)) + payload


def _track(events: list[tuple[int, int, bytes]]) -> bytes:
    """Encode (tick, order, message) events as one MTrk chunk.

    `order` breaks ties at the same tick: note-offs must precede note-ons, or
    a repeated pitch is switched off the instant it starts.
    """
    events.sort(key=lambda e: (e[0], e[1]))
    body = bytearray()
    last = 0
    for tick, _, message in events:
        body += _varlen(tick - last) + message
        last = tick
    body += _varlen(0) + _meta(0x2F, b"")
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def _pickup_signature(pickup_beats: float) -> tuple[int, int] | None:
    """A pickup length as a time signature, e.g. 1.0 -> 1/4, 1.5 -> 3/8."""
    if pickup_beats <= 0:
        return None
    for denominator in (4, 8, 16, 32):
        numerator = pickup_beats * denominator / 4.0
        if abs(numerator - round(numerator)) < 1e-6 and 1 <= round(numerator) <= 64:
            return round(numerator), denominator
    return None


def _timeline_for(score: Score) -> Timeline:
    return score.timeline or Timeline.constant(score.tempo_bpm or 120.0)


def _ticks(note: Note, timeline: Timeline, ppq: int) -> tuple[int, int]:
    if note.beat is not None and note.beats is not None and note.beats > 0:
        start, end = note.beat, note.beat + note.beats
    else:
        start = timeline.beat_at(note.onset)
        end = timeline.beat_at(note.onset + note.duration)
    start_tick = max(0, round(start * ppq))
    return start_tick, max(start_tick + 1, round(end * ppq))


def write_midi(score: Score, *, ppq: int = PPQ) -> bytes:
    """Serialise `score`. Staff 2 goes to the left-hand track, everything else right."""
    timeline = _timeline_for(score)

    conductor: list[tuple[int, int, bytes]] = [
        (0, 0, _meta(0x03, score.title.encode("utf-8", errors="replace")[:120]))
    ]
    for tempo in timeline.tempos:
        usec = max(1, min(0xFFFFFF, round(60_000_000 / tempo.bpm)))
        conductor.append((round(tempo.beat * ppq), 1, _meta(0x51, usec.to_bytes(3, "big"))))
    def signature(tick: int, numerator: int, denominator: int) -> None:
        power = max(0, denominator.bit_length() - 1)
        conductor.append((tick, 2, _meta(0x58, bytes([numerator & 0xFF, power, 24, 8]))))

    # MIDI has no notion of a pickup bar. The convention every sequencer
    # understands is a short first bar with its own time signature, followed
    # by the real meter. That keeps bar numbers identical after re-import.
    pickup = _pickup_signature(timeline.pickup_beats)
    for meter in timeline.meters:
        if timeline.pickup_beats > 0 and meter.bar == 1:
            if pickup is not None:
                signature(0, *pickup)
                signature(round(timeline.pickup_beats * ppq), meter.numerator, meter.denominator)
            else:
                signature(0, meter.numerator, meter.denominator)
        else:
            signature(round(timeline.bar_start(meter.bar) * ppq), meter.numerator, meter.denominator)
    for key in timeline.keys:
        conductor.append(
            (
                round(key.beat * ppq),
                3,
                _meta(0x59, struct.pack("b", key.fifths) + bytes([1 if key.mode == "minor" else 0])),
            )
        )

    hands: dict[int, list[Note]] = {1: [], 2: []}
    for note in score.notes:
        if 0 <= note.pitch <= 127:
            hands[2 if note.staff == 2 else 1].append(note)

    tracks = [_track(conductor)]
    names = {1: "Right hand", 2: "Left hand"}
    for staff in (1, 2):
        events: list[tuple[int, int, bytes]] = [
            (0, 0, _meta(0x03, names[staff].encode())),
            (0, 1, bytes([0xC0, _ACOUSTIC_GRAND])),
        ]
        if staff == 1:
            for pedal in score.pedals:
                down = round(timeline.beat_at(pedal.start) * ppq)
                up = max(down + 1, round(timeline.beat_at(pedal.end) * ppq))
                events.append((down, 4, bytes([0xB0, 64, 127])))
                events.append((up, 2, bytes([0xB0, 64, 0])))

        # One key cannot sound twice. If a pitch restarts before its previous
        # note ends, end the earlier one at the restart, otherwise its note-off
        # would silence the new note early.
        spans: dict[int, list[list[int]]] = {}
        for note in sorted(hands[staff], key=lambda n: (n.onset, n.pitch)):
            start, end = _ticks(note, timeline, ppq)
            velocity = min(127, max(1, note.velocity if note.velocity else 72))
            previous = spans.setdefault(note.pitch, [])
            if previous and previous[-1][1] > start:
                previous[-1][1] = max(previous[-1][0] + 1, start)
            previous.append([start, end, velocity])
        for pitch, pitch_spans in spans.items():
            for start, end, velocity in pitch_spans:
                events.append((start, 5, bytes([0x90, pitch, velocity])))
                events.append((end, 3, bytes([0x80, pitch, 0])))
        tracks.append(_track(events))

    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), ppq)
    return header + b"".join(tracks)
