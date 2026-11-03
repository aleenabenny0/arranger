"""Read MIDI files into a Score.

Written against the Standard MIDI File spec rather than using a library, for
two reasons. One, it keeps the whole project installable with zero `pip`
commands, which removes the most common way a beginner's project stops
working. Two, MIDI is a small enough format that the parser is a few hundred
lines and you can actually read it - which matters, because when a file
imports wrong, you need to be able to look.

What this handles: format 0, 1, and 2; running status; tempo changes; time
signature changes; key signatures; variable-length quantities; note-on with
velocity 0 as note-off; note velocity; sustain pedal (CC64); track names and
programs; percussion filtering.

What it ignores on purpose: pitch bend, aftertouch, sysex, and every
controller except sustain. None of them change which keys are pressed.

Every byte here comes from a stranger. The parser is bounded by
`ImportLimits` and raises typed errors; it never trusts a declared length.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .ir import Note, PedalSpan, Score, TrackInfo
from .limits import (
    DEFAULT_LIMITS,
    EmptyScore,
    ImportLimits,
    LimitExceeded,
    MalformedFile,
    UnsupportedFormat,
)
from .timeline import KeyChange, MeterChange, TempoChange, Timeline

DRUM_CHANNEL = 9  # 0-indexed; channel 10 in one-indexed MIDI docs
DEFAULT_TEMPO = 500_000  # microseconds per quarter note = 120bpm
SUSTAIN_CONTROLLER = 64


class MidiError(MalformedFile):
    """The file is not valid MIDI, or uses something we do not support."""

    code = "malformed_midi"


class MidiUnsupported(UnsupportedFormat, MidiError):
    """Valid MIDI, but a variant this reader does not handle."""

    code = "unsupported_midi"


# --- low-level reading ---------------------------------------------------


class _Reader:
    """A cursor over bytes. MIDI is full of variable-length fields, so a
    position-tracking reader is much less error-prone than manual slicing."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise MidiError("The MIDI file ends unexpectedly.", detail="eof in byte()")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def bytes(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise MidiError("The MIDI file ends unexpectedly.", detail="eof in bytes()")
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
        raise MidiError(
            "The MIDI file contains an invalid timing value.",
            detail="variable-length quantity longer than 4 bytes",
        )

    def at_end(self) -> bool:
        return self.pos >= len(self.data)


# --- track parsing -------------------------------------------------------


@dataclass
class _RawNote:
    start: int
    end: int
    pitch: int
    channel: int
    velocity: int


@dataclass
class _RawTrack:
    notes: list[_RawNote] = field(default_factory=list)
    tempos: list[tuple[int, int]] = field(default_factory=list)       # tick, usec/quarter
    sigs: list[tuple[int, int, int]] = field(default_factory=list)    # tick, num, den
    keys: list[tuple[int, int, str]] = field(default_factory=list)    # tick, fifths, mode
    pedal: list[tuple[int, int, bool]] = field(default_factory=list)  # tick, channel, down
    programs: dict[int, int] = field(default_factory=dict)            # channel -> program
    name: str = ""
    instrument: str = ""
    end_tick: int = 0
    unclosed: int = 0
    events: int = 0


def _text(payload: bytes, limit: int) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("latin-1", errors="replace")
    cleaned = "".join(ch for ch in text if ch.isprintable()).strip()
    return cleaned[:limit]


def _parse_track(data: bytes, limits: ImportLimits, budget: list[int]) -> _RawTrack:
    """One MTrk chunk -> its events, in absolute ticks.

    `budget` is a one-element list holding the remaining event allowance for
    the whole file, shared across tracks so that many small tracks cannot add
    up to more work than one large one.
    """
    r = _Reader(data)
    out = _RawTrack()
    tick = 0
    # Running status: a channel event may omit its status byte and reuse the
    # previous channel status. Forgetting this misparses most real files. The
    # spec says meta and sysex events cancel it, but enough sequencers write
    # files that assume otherwise that being strict rejects real music, so
    # only channel messages update `running`.
    running = 0

    notes_on: dict[tuple[int, int], list[tuple[int, int]]] = {}

    while not r.at_end():
        budget[0] -= 1
        if budget[0] < 0:
            raise LimitExceeded(
                "This MIDI file has more events than this service will process.",
                detail=f"max_events={limits.max_events}",
            )
        out.events += 1
        tick += r.varlen()
        b = r.byte()

        if b & 0x80:
            status = b
            if b < 0xF0:
                running = b
        else:
            r.pos -= 1  # running status: that byte was data, not status
            if not running:
                raise MidiError(
                    "The MIDI file is corrupt.", detail="data byte before any status byte"
                )
            status = running

        event, channel = status & 0xF0, status & 0x0F

        if status == 0xFF:  # meta event
            meta_type = r.byte()
            payload = r.bytes(r.varlen())
            if meta_type == 0x51 and len(payload) == 3:
                usec = int.from_bytes(payload, "big")
                if usec > 0:
                    out.tempos.append((tick, usec))
            elif meta_type == 0x58 and len(payload) >= 2:
                if payload[0] >= 1 and payload[1] <= 6:  # 2**6 = 64th notes
                    out.sigs.append((tick, payload[0], 2 ** payload[1]))
            elif meta_type == 0x59 and len(payload) >= 2:
                fifths = struct.unpack("b", payload[:1])[0]
                if -7 <= fifths <= 7:
                    out.keys.append((tick, fifths, "minor" if payload[1] == 1 else "major"))
            elif meta_type == 0x03 and not out.name:
                out.name = _text(payload, limits.max_title_chars)
            elif meta_type == 0x04 and not out.instrument:
                out.instrument = _text(payload, limits.max_title_chars)
            elif meta_type == 0x2F:
                break

        elif status in (0xF0, 0xF7):  # sysex, skip
            r.bytes(r.varlen())

        elif event == 0x90:  # note on
            pitch, velocity = r.byte() & 0x7F, r.byte() & 0x7F
            if velocity > 0:
                notes_on.setdefault((channel, pitch), []).append((tick, velocity))
            else:
                _close_note(notes_on, out.notes, channel, pitch, tick)

        elif event == 0x80:  # note off
            pitch = r.byte() & 0x7F
            r.byte()  # release velocity, unused
            _close_note(notes_on, out.notes, channel, pitch, tick)

        elif event == 0xB0:  # controller
            controller, value = r.byte() & 0x7F, r.byte() & 0x7F
            if controller == SUSTAIN_CONTROLLER:
                out.pedal.append((tick, channel, value >= 64))

        elif event == 0xC0:  # program change
            out.programs.setdefault(channel, r.byte() & 0x7F)

        elif event in (0xA0, 0xE0):  # two data bytes, ignored
            r.bytes(2)
        elif event == 0xD0:  # one data byte, ignored
            r.bytes(1)
        else:
            raise MidiError(
                "The MIDI file is corrupt.", detail=f"unrecognised status byte {status:#04x}"
            )

        if len(out.notes) > limits.max_notes:
            raise LimitExceeded(
                "This MIDI file has more notes than this service will process.",
                detail=f"max_notes={limits.max_notes}",
            )

    # Notes still held at end of track: close them at the final tick rather
    # than dropping them. A truncated file should not silently lose music.
    for (channel, pitch), starts in notes_on.items():
        for start, velocity in starts:
            if tick > start:
                out.notes.append(_RawNote(start, tick, pitch, channel, velocity))
                out.unclosed += 1

    out.end_tick = tick
    return out


def _close_note(notes_on, notes: list[_RawNote], channel: int, pitch: int, tick: int) -> None:
    starts = notes_on.get((channel, pitch))
    if starts:
        start, velocity = starts.pop(0)
        if tick > start:  # zero-length notes are artefacts, drop them
            notes.append(_RawNote(start, tick, pitch, channel, velocity))


# --- timing --------------------------------------------------------------


def _build_timeline(
    tempos: list[tuple[int, int]],
    sigs: list[tuple[int, int, int]],
    keys: list[tuple[int, int, str]],
    division: int,
    warnings: list[str],
) -> Timeline:
    """Tick-based MIDI meta events -> a beat-based Timeline.

    MIDI places a time-signature change at a tick, but a meter can only
    change at a barline. Walk the changes in order, converting each tick to
    the bar it falls in under the meter in force; a change that lands
    mid-bar is moved to the next barline and reported.
    """
    tempo_changes = [
        TempoChange(tick / division, 60_000_000 / usec) for tick, usec in sorted(tempos)
    ]

    meter_changes: list[MeterChange] = []
    events = sorted(sigs)
    if not events or events[0][0] != 0:
        events.insert(0, (0, 4, 4))
    bar, bar_start_beat = 1, 0.0
    cur_beats = events[0][1] * 4.0 / events[0][2]
    moved = 0
    for tick, num, den in events:
        beat = tick / division
        if beat > bar_start_beat and cur_beats > 0:
            whole = (beat - bar_start_beat) / cur_beats
            bars_passed = int(whole + 1e-6)
            if whole - bars_passed > 1e-3:  # mid-bar: push to the next barline
                bars_passed += 1
                moved += 1
            bar += bars_passed
            bar_start_beat += bars_passed * cur_beats
        meter_changes.append(MeterChange(bar, num, den))
        cur_beats = num * 4.0 / den
    if moved:
        warnings.append(
            f"{moved} time-signature change(s) fell inside a bar and were moved "
            "to the next barline."
        )

    key_changes = [KeyChange(tick / division, fifths, mode) for tick, fifths, mode in sorted(keys)]

    # A one-bar-long first meter that is shorter than the meter following it
    # is how MIDI spells a pickup bar. Read it as one.
    pickup_beats = 0.0
    if (
        len(meter_changes) >= 2
        and meter_changes[0].bar == 1
        and meter_changes[1].bar == 2
        and meter_changes[0].bar_beats < meter_changes[1].bar_beats
    ):
        pickup_beats = meter_changes[0].bar_beats
        real = meter_changes[1]
        meter_changes = [MeterChange(1, real.numerator, real.denominator), *meter_changes[2:]]
    return Timeline(tempo_changes, meter_changes, key_changes, pickup_beats=pickup_beats)


def _pedal_spans(
    events: list[tuple[int, int, bool]], division: int, timeline: Timeline, end_tick: int
) -> list[PedalSpan]:
    """CC64 events -> spans in seconds. Any channel's pedal counts: a piano has one."""
    spans: list[PedalSpan] = []
    down_since: dict[int, int] = {}
    for tick, channel, down in sorted(events):
        if down and channel not in down_since:
            down_since[channel] = tick
        elif not down and channel in down_since:
            start = down_since.pop(channel)
            if tick > start:
                spans.append(
                    PedalSpan(
                        timeline.seconds_at(start / division),
                        timeline.seconds_at(tick / division),
                    )
                )
    for start in down_since.values():
        if end_tick > start:
            spans.append(
                PedalSpan(
                    timeline.seconds_at(start / division),
                    timeline.seconds_at(end_tick / division),
                )
            )
    # Merge overlaps so "is the pedal down at t" is a simple interval test.
    spans.sort(key=lambda s: s.start)
    merged: list[PedalSpan] = []
    for span in spans:
        if merged and span.start <= merged[-1].end + 1e-6:
            merged[-1] = PedalSpan(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    return merged


# --- the public entry points --------------------------------------------


def sniff_midi(data: bytes) -> bool:
    return len(data) >= 14 and data[:4] == b"MThd"


def read_midi_bytes(
    data: bytes,
    *,
    filename: str | None = None,
    limits: ImportLimits = DEFAULT_LIMITS,
    include_drums: bool = False,
    max_tracks: int | None = None,
) -> Score:
    """Parse a Standard MIDI File held in memory.

    A "part" is one (track, channel) pair that carries notes. Format 0 files
    put every instrument on one track and separate them by channel; format 1
    files usually use one track per instrument. Splitting on both makes track
    selection in the UI meaningful for either.

    Staff assignment: if exactly two parts carry notes, they are treated as
    the two hands (higher average pitch = staff 1 = right). That is the usual
    convention in piano MIDI. With any other number of parts, staff is left
    unset and the verifier infers hands itself - which is the safer default,
    since guessing wrong produces confident nonsense.
    """
    if len(data) > limits.max_bytes:
        raise LimitExceeded(
            "This file is larger than the upload limit.",
            detail=f"{len(data)} > max_bytes={limits.max_bytes}",
        )
    if not sniff_midi(data):
        raise MidiError("This is not a MIDI file.", detail="no MThd header")

    _, header_len, fmt, n_tracks, division = struct.unpack(">4sIHHH", data[:14])
    if header_len < 6:
        raise MidiError("The MIDI file header is corrupt.", detail=f"header_len={header_len}")
    if fmt > 2:
        raise MidiUnsupported("This MIDI format is not supported.", detail=f"format={fmt}")
    if division & 0x8000:
        raise MidiUnsupported(
            "This MIDI file uses SMPTE timecode, which is not supported. "
            "Re-export it with a ticks-per-quarter-note time base.",
            detail="SMPTE division",
        )
    if division == 0:
        raise MidiError("The MIDI file header is corrupt.", detail="division is zero")
    if n_tracks > limits.max_tracks:
        raise LimitExceeded(
            "This MIDI file has more tracks than this service will process.",
            detail=f"{n_tracks} > max_tracks={limits.max_tracks}",
        )

    warnings: list[str] = []
    pos = 8 + header_len
    raw_tracks: list[_RawTrack] = []
    budget = [limits.max_events]
    chunks = 0
    while pos + 8 <= len(data) and len(raw_tracks) < n_tracks:
        chunks += 1
        if chunks > limits.max_tracks * 4:
            raise LimitExceeded("This MIDI file has too many chunks.", detail="chunk count")
        chunk_id, chunk_len = struct.unpack(">4sI", data[pos : pos + 8])
        available = len(data) - (pos + 8)
        if chunk_len > available:
            warnings.append("The file is truncated; music after the break is missing.")
            chunk_len = available
        body = data[pos + 8 : pos + 8 + chunk_len]
        pos += 8 + chunk_len
        if chunk_id == b"MTrk":  # non-MTrk chunks are legal and ignorable
            raw_tracks.append(_parse_track(body, limits, budget))

    if not raw_tracks:
        raise MidiError("The MIDI file contains no tracks.", detail="no MTrk chunks")
    if len(raw_tracks) < n_tracks:
        warnings.append(
            f"The header promises {n_tracks} tracks but only {len(raw_tracks)} were found."
        )

    timeline = _build_timeline(
        [ev for t in raw_tracks for ev in t.tempos],
        [ev for t in raw_tracks for ev in t.sigs],
        [ev for t in raw_tracks for ev in t.keys],
        division,
        warnings,
    )
    end_tick = max((t.end_tick for t in raw_tracks), default=0)
    if timeline.seconds_at(end_tick / division) > limits.max_duration_seconds:
        raise LimitExceeded(
            "This piece is longer than this service will process.",
            detail=f"max_duration_seconds={limits.max_duration_seconds}",
        )

    # Parts: (track, channel) pairs that carry notes.
    parts: dict[tuple[int, int], list[_RawNote]] = {}
    for ti, track in enumerate(raw_tracks):
        for note in track.notes:
            parts.setdefault((ti, note.channel), []).append(note)

    drum_notes = sum(len(v) for (_, ch), v in parts.items() if ch == DRUM_CHANNEL)
    if drum_notes and not include_drums:
        warnings.append(f"{drum_notes} percussion notes were left out.")

    pitched = {k: v for k, v in parts.items() if include_drums or k[1] != DRUM_CHANNEL}
    if not pitched:
        raise EmptyScore(
            "This MIDI file contains no pitched notes."
            if drum_notes
            else "This MIDI file contains no notes.",
            detail="no playable notes",
        )

    ordered = sorted(pitched)  # stable part numbering: by track, then channel
    if max_tracks:  # keep the busiest parts; usually the melody and bass
        keep = set(sorted(ordered, key=lambda k: -len(pitched[k]))[:max_tracks])
        dropped = [k for k in ordered if k not in keep]
        if dropped:
            warnings.append(f"{len(dropped)} quieter part(s) were left out.")
        ordered = [k for k in ordered if k in keep]

    unclosed = sum(t.unclosed for t in raw_tracks)
    if unclosed:
        warnings.append(
            f"{unclosed} note(s) never ended and were closed at the end of their track."
        )

    staff_of = _assign_staves([pitched[k] for k in ordered])

    tracks: list[TrackInfo] = []
    notes: list[Note] = []
    # Downloaded MIDI files routinely stack duplicate layers - the same melody
    # on two tracks for a fuller synth sound. A piano has one key per pitch, so
    # a doubled note is not two notes; it is one. Left in, they inflate every
    # polyphony count and make the whole file look unplayable. The longest and
    # loudest copy wins.
    seen: dict[tuple[int, int], int] = {}
    doubled = 0

    for part_index, key in enumerate(ordered):
        ti, channel = key
        raw = raw_tracks[ti]
        part_notes = sorted(pitched[key], key=lambda n: (n.start, n.pitch))
        pitches = [n.pitch for n in part_notes]
        tracks.append(
            TrackInfo(
                index=part_index,
                name=raw.name or raw.instrument or f"Track {ti + 1}",
                program=raw.programs.get(channel),
                channel=channel,
                is_percussion=channel == DRUM_CHANNEL,
                note_count=len(part_notes),
                lowest_pitch=min(pitches),
                highest_pitch=max(pitches),
            )
        )
        for k, rn in enumerate(part_notes):
            start_beat = rn.start / division
            end_beat = rn.end / division
            onset = timeline.seconds_at(start_beat)
            duration = max(timeline.seconds_at(end_beat) - onset, 1e-3)
            dedupe_key = (rn.pitch, rn.start)
            if dedupe_key in seen:
                doubled += 1
                existing = notes[seen[dedupe_key]]
                if duration > existing.duration or rn.velocity > (existing.velocity or 0):
                    notes[seen[dedupe_key]] = Note(
                        pitch=existing.pitch,
                        onset=existing.onset,
                        duration=max(duration, existing.duration),
                        staff=existing.staff,
                        bar=existing.bar,
                        voice=existing.voice,
                        id=existing.id,
                        velocity=max(rn.velocity, existing.velocity or 0),
                        beat=existing.beat,
                        beats=max(end_beat - start_beat, existing.beats or 0.0),
                        track=existing.track,
                    )
                continue
            seen[dedupe_key] = len(notes)
            notes.append(
                Note(
                    pitch=rn.pitch,
                    onset=onset,
                    duration=duration,
                    staff=staff_of.get(part_index),
                    bar=timeline.bar_at(start_beat),
                    id=f"t{part_index}n{k}",
                    velocity=rn.velocity,
                    beat=start_beat,
                    beats=end_beat - start_beat,
                    track=part_index,
                )
            )
    if doubled:
        warnings.append(f"{doubled} doubled note(s) on the same pitch and beat were merged.")

    last = max((n.bar or 1 for n in notes), default=1)
    if last > limits.max_bars:
        raise LimitExceeded(
            "This piece has more bars than this service will process.",
            detail=f"{last} > max_bars={limits.max_bars}",
        )

    if not any(t.tempos for t in raw_tracks):
        warnings.append("No tempo was stated; 120 bpm is assumed.")
    if not any(t.sigs for t in raw_tracks):
        warnings.append(
            "No time signature was stated; 4/4 is assumed, so bar lines may be wrong."
        )

    title = ""
    if filename:
        title = Path(filename).stem.replace("_", " ").strip()
    if not title:
        title = next((t.name for t in raw_tracks if t.name), "") or "untitled"
    title = "".join(ch for ch in title if ch.isprintable())[: limits.max_title_chars]

    return Score(
        notes=notes,
        tempo_bpm=timeline.initial_bpm,
        title=title,
        timeline=timeline,
        pedals=_pedal_spans(
            [ev for t in raw_tracks for ev in t.pedal], division, timeline, end_tick
        ),
        tracks=tracks,
        source_format="midi",
        warnings=warnings,
    )


def read_midi(
    path: str | Path,
    *,
    include_drums: bool = False,
    max_tracks: int | None = None,
    limits: ImportLimits = DEFAULT_LIMITS,
) -> Score:
    """Load a MIDI file from disk. See `read_midi_bytes`."""
    path = Path(path)
    size = path.stat().st_size
    if size > limits.max_bytes:
        raise LimitExceeded(
            "This file is larger than the upload limit.",
            detail=f"{size} > max_bytes={limits.max_bytes}",
        )
    return read_midi_bytes(
        path.read_bytes(),
        filename=path.name,
        limits=limits,
        include_drums=include_drums,
        max_tracks=max_tracks,
    )


def _assign_staves(parts: list[list[_RawNote]]) -> dict[int, int]:
    """Two parts means two hands. Anything else, do not guess."""
    if len(parts) != 2:
        return {}
    means = [sum(n.pitch for n in p) / len(p) for p in parts]
    high = 0 if means[0] >= means[1] else 1
    return {high: 1, 1 - high: 2}
