"""Read MIDI files into a Score.

Written against the Standard MIDI File spec rather than using a library, for
two reasons. One, it keeps the whole project installable with zero `pip`
commands, which removes the most common way a beginner's project stops
working. Two, MIDI is a small enough format that the parser is ~200 lines and
you can actually read it — which matters, because when a file imports wrong,
you need to be able to look.

What this handles: format 0, 1, and 2; running status; tempo changes; time
signatures; variable-length quantities; note-on-with-velocity-0 as note-off.

What it ignores on purpose: controllers, pitch bend, program changes,
aftertouch, sysex. None of them affect which notes are played, which is all
the verifier cares about.
"""

from __future__ import annotations

import struct
from bisect import bisect_right
from pathlib import Path

from .ir import Note, Score

DRUM_CHANNEL = 9  # 0-indexed; channel 10 in one-indexed MIDI docs
DEFAULT_TEMPO = 500_000  # microseconds per quarter note = 120bpm


class MidiError(ValueError):
    """The file isn't valid MIDI, or uses something we don't support."""


# --- low-level reading ---------------------------------------------------


class _Reader:
    """A cursor over bytes. MIDI is full of variable-length fields, so a
    position-tracking reader is much less error-prone than manual slicing."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise MidiError("unexpected end of file")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise MidiError("unexpected end of file")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def varlen(self) -> int:
        """Variable-length quantity: 7 bits per byte, high bit means continue.

        This is how MIDI stores delta times. A value under 128 is one byte;
        larger values chain. Get this wrong and every subsequent byte in the
        track is misread, which is why bad parsers produce garbage rather
        than errors.
        """
        value = 0
        for _ in range(4):
            b = self.byte()
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                return value
        raise MidiError("variable-length quantity longer than 4 bytes")

    def at_end(self) -> bool:
        return self.pos >= len(self.data)


# --- timing --------------------------------------------------------------


class TimeMap:
    """Converts MIDI ticks to seconds, respecting tempo changes.

    Tempo is not a constant. A piece that speeds up halfway through has two
    tempo events, and every tick after the second one converts differently.
    Ignoring this is the classic MIDI-parsing bug: everything is right until
    the first tempo change, then progressively wrong.
    """

    def __init__(self, tempo_events: list[tuple[int, int]], division: int):
        events = sorted(tempo_events)
        if not events or events[0][0] != 0:
            events.insert(0, (0, DEFAULT_TEMPO))

        self.division = division
        self.ticks: list[int] = []
        self.seconds: list[float] = []
        self.tempos: list[int] = []

        elapsed = 0.0
        prev_tick, prev_tempo = 0, events[0][1]
        for tick, tempo in events:
            elapsed += (tick - prev_tick) * prev_tempo / 1e6 / division
            self.ticks.append(tick)
            self.seconds.append(elapsed)
            self.tempos.append(tempo)
            prev_tick, prev_tempo = tick, tempo

    def seconds_at(self, tick: int) -> float:
        i = max(0, bisect_right(self.ticks, tick) - 1)
        delta = tick - self.ticks[i]
        return self.seconds[i] + delta * self.tempos[i] / 1e6 / self.division

    def bpm_at_start(self) -> float:
        return 60_000_000 / self.tempos[0]


class BarMap:
    """Turns ticks into bar numbers, respecting time-signature changes.

    Only used to make violation messages readable ("bar 14" rather than
    "t=27.3s"). Nothing depends on it being exactly right, but being off by
    one makes the reports useless for finding the spot in your sheet music.
    """

    def __init__(self, sig_events: list[tuple[int, int, int]], division: int):
        events = sorted(sig_events)
        if not events or events[0][0] != 0:
            events.insert(0, (0, 4, 4))

        self.starts: list[int] = []      # tick where each section begins
        self.bar_at_start: list[int] = []
        self.ticks_per_bar: list[float] = []

        bar = 1
        prev_tick = 0
        prev_tpb = division * 4 * events[0][1] / events[0][2]
        for tick, num, den in events:
            if tick > prev_tick and prev_tpb > 0:
                bar += int((tick - prev_tick) / prev_tpb)
            tpb = division * 4 * num / den
            self.starts.append(tick)
            self.bar_at_start.append(bar)
            self.ticks_per_bar.append(tpb)
            prev_tick, prev_tpb = tick, tpb

    def bar_at(self, tick: int) -> int:
        i = max(0, bisect_right(self.starts, tick) - 1)
        tpb = self.ticks_per_bar[i]
        if tpb <= 0:
            return self.bar_at_start[i]
        return self.bar_at_start[i] + int((tick - self.starts[i]) / tpb)


# --- track parsing -------------------------------------------------------


def _parse_track(data: bytes) -> dict:
    """One MTrk chunk -> its note events and timing metadata.

    Returns absolute ticks, not deltas. Delta times are how MIDI stores it;
    absolute ticks are what everything downstream wants.
    """
    r = _Reader(data)
    tick = 0
    status = 0  # running status: an event may omit its status byte and
    # reuse the previous one. Forgetting this misparses most real files.

    notes_on: dict[tuple[int, int], list[int]] = {}
    notes: list[tuple[int, int, int, int]] = []  # start, end, pitch, channel
    tempos: list[tuple[int, int]] = []
    sigs: list[tuple[int, int, int]] = []
    name = ""

    while not r.at_end():
        tick += r.varlen()
        b = r.byte()

        if b & 0x80:
            status = b
        else:
            r.pos -= 1  # running status: that byte was data, not status
            if not status:
                raise MidiError("data byte before any status byte")

        event, channel = status & 0xF0, status & 0x0F

        if status == 0xFF:  # meta event
            meta_type = r.byte()
            length = r.varlen()
            payload = r.bytes(length)
            if meta_type == 0x51 and length == 3:
                tempos.append((tick, int.from_bytes(payload, "big")))
            elif meta_type == 0x58 and length >= 2:
                sigs.append((tick, payload[0], 2 ** payload[1]))
            elif meta_type == 0x03:
                name = payload.decode("latin-1", errors="replace").strip()
            elif meta_type == 0x2F:
                break

        elif status in (0xF0, 0xF7):  # sysex, skip
            r.bytes(r.varlen())

        elif event == 0x90:  # note on
            pitch, velocity = r.byte(), r.byte()
            if velocity > 0:
                notes_on.setdefault((channel, pitch), []).append(tick)
            else:
                _close_note(notes_on, notes, channel, pitch, tick)

        elif event == 0x80:  # note off
            pitch = r.byte()
            r.byte()  # release velocity, unused
            _close_note(notes_on, notes, channel, pitch, tick)

        elif event in (0xA0, 0xB0, 0xE0):  # two data bytes, ignored
            r.bytes(2)
        elif event in (0xC0, 0xD0):  # one data byte, ignored
            r.bytes(1)
        else:
            raise MidiError(f"unrecognised status byte {status:#04x}")

    # Notes still held at end of track: close them at the final tick rather
    # than dropping them. A truncated file shouldn't silently lose music.
    for (channel, pitch), starts in notes_on.items():
        for start in starts:
            notes.append((start, tick, pitch, channel))

    return {"notes": notes, "tempos": tempos, "sigs": sigs, "name": name}


def _close_note(notes_on, notes, channel, pitch, tick) -> None:
    starts = notes_on.get((channel, pitch))
    if starts:
        start = starts.pop(0)
        if tick > start:  # zero-length notes are artefacts, drop them
            notes.append((start, tick, pitch, channel))


# --- the public entry point ---------------------------------------------


def read_midi(
    path: str | Path,
    *,
    include_drums: bool = False,
    max_tracks: int | None = None,
) -> Score:
    """Load a MIDI file as a Score.

    Staff assignment: if exactly two tracks carry notes, they're treated as
    the two hands (higher average pitch = staff 1 = right). That's the usual
    convention in piano MIDI. With any other number of tracks, staff is left
    unset and the verifier infers hands itself — which is the safer default,
    since guessing wrong produces confident nonsense.
    """
    path = Path(path)
    raw = path.read_bytes()

    if len(raw) < 14 or raw[:4] != b"MThd":
        raise MidiError(f"{path.name} is not a MIDI file (no MThd header)")

    _, header_len, fmt, n_tracks, division = struct.unpack(">4sIHHH", raw[:14])
    if division & 0x8000:
        raise MidiError("SMPTE timecode division is not supported")
    if division == 0:
        raise MidiError("division is zero")

    pos = 8 + header_len
    tracks = []
    while pos + 8 <= len(raw) and len(tracks) < n_tracks:
        chunk_id, chunk_len = struct.unpack(">4sI", raw[pos : pos + 8])
        body = raw[pos + 8 : pos + 8 + chunk_len]
        pos += 8 + chunk_len
        if chunk_id == b"MTrk":  # non-MTrk chunks are legal and ignorable
            tracks.append(_parse_track(body))

    if not tracks:
        raise MidiError("no track data found")

    time_map = TimeMap(
        [ev for t in tracks for ev in t["tempos"]], division
    )
    bar_map = BarMap([ev for t in tracks for ev in t["sigs"]], division)

    playing = [t for t in tracks if t["notes"]]
    if not include_drums:
        playing = [
            t for t in playing
            if not all(ch == DRUM_CHANNEL for _, _, _, ch in t["notes"])
        ]
    if not playing:
        raise MidiError("file contains no playable notes")

    if max_tracks:  # keep the busiest tracks; usually the melody and bass
        playing = sorted(playing, key=lambda t: -len(t["notes"]))[:max_tracks]

    staff_of = _assign_staves(playing)

    notes = []
    seen: set[tuple[int, int]] = set()
    for i, track in enumerate(playing):
        for start, end, pitch, channel in track["notes"]:
            if channel == DRUM_CHANNEL and not include_drums:
                continue
            # Downloaded MIDI files routinely stack duplicate layers — the same
            # melody on two tracks for a fuller synth sound. A piano has one
            # key per pitch, so a doubled note is not two notes; it is one.
            # Left in, they inflate every polyphony count and make the whole
            # file look unplayable. Rounded to the millisecond so that layers
            # nudged fractionally apart still collapse.
            key = (pitch, round(start))
            if key in seen:
                continue
            seen.add(key)

            onset = time_map.seconds_at(start)
            notes.append(
                Note(
                    pitch=pitch,
                    onset=onset,
                    duration=max(time_map.seconds_at(end) - onset, 1e-3),
                    staff=staff_of.get(i),
                    bar=bar_map.bar_at(start),
                )
            )

    return Score(
        notes=notes,
        tempo_bpm=time_map.bpm_at_start(),
        title=path.stem.replace("_", " "),
    )


def _assign_staves(playing: list[dict]) -> dict[int, int]:
    """Two tracks means two hands. Anything else, don't guess."""
    if len(playing) != 2:
        return {}
    means = [
        sum(p for _, _, p, _ in t["notes"]) / len(t["notes"]) for t in playing
    ]
    high = 0 if means[0] >= means[1] else 1
    return {high: 1, 1 - high: 2}
