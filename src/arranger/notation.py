"""From note events to something that can be printed.

A `Score` says *when keys go down*. A page of music says something stricter:
every bar adds up to its meter, every voice is a gapless sequence of notes and
rests, long notes are tied across barlines, and every pitch has a letter name
that makes sense in the key. This module does that conversion once, into a
`NotatedScore`, so the MusicXML writer and the LilyPond writer are both thin
and cannot disagree.

All positions are exact `Fraction`s of a quarter note. Nothing here guesses
silently: every lossy step adds a line to `NotatedScore.warnings`.

Dependency-free.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from math import floor

from .ir import Note, Score
from .timeline import Timeline

F = Fraction
ZERO = F(0)

# --- spelling -------------------------------------------------------------

# Position of each natural on the line of fifths, F=-1 C=0 G=1 ...
_LOF_STEP = {"F": -1, "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5}
_STEP_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_LOF_NAMES = ["F", "C", "G", "D", "A", "E", "B"]


def spell_pitch_class(pc: int, fifths: int, mode: str = "major") -> tuple[str, int]:
    """Letter and accidental for a pitch class in a key.

    Notes are chosen from a twelve-wide window on the line of fifths, placed
    around the key. In a major key the window runs from four fifths flatward
    of the tonic to seven sharpward: C major gets Ab Eb Bb F C G D A E B F# C#.

    A minor key shares its signature with a major key but leans sharpward: its
    leading tone and raised sixth are the accidentals that actually occur. So
    the window shifts two fifths sharp. A minor gets Bb F C G D A E B F# C# G#
    D#, which spells the leading tone G#. Spelling it Ab, as the major window
    would, is the classic tell of software that ignores the mode.
    """
    low = fifths - (2 if mode == "minor" else 4)
    # The pitch class of line-of-fifths position p is (p * 7) mod 12.
    for position in range(low, low + 12):
        if (position * 7) % 12 == pc % 12:
            step = _LOF_NAMES[(position + 1) % 7]
            alter = (position + 1) // 7
            return step, alter
    raise AssertionError("unreachable: twelve fifths cover twelve pitch classes")


def spell(
    pitch: int, fifths: int, preferred: tuple[str, int] | None = None, mode: str = "major"
) -> tuple[str, int, int]:
    """(step, alter, octave) for a MIDI pitch. `preferred` wins when it fits."""
    if preferred is not None:
        step, alter = preferred
        if step in _STEP_PC and (_STEP_PC[step] + alter) % 12 == pitch % 12:
            return step, alter, (pitch - alter) // 12 - 1
    step, alter = spell_pitch_class(pitch % 12, fifths, mode)
    return step, alter, (pitch - alter) // 12 - 1


_MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
_TONIC_TO_FIFTHS_MAJOR = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}


def estimate_key(notes: list[Note]) -> tuple[int, str]:
    """Krumhansl-Schmuckler key estimate: (fifths, mode).

    Used only when the source states no key. It decides spelling and the key
    signature, so a wrong guess costs accidentals, not notes.
    """
    weights = [0.0] * 12
    for n in notes:
        weights[n.pitch % 12] += max(n.duration, 0.05)
    if not any(weights):
        return 0, "major"
    mean_w = sum(weights) / 12

    def correlation(profile: tuple[float, ...], tonic: int) -> float:
        mean_p = sum(profile) / 12
        num = sum((weights[(tonic + i) % 12] - mean_w) * (profile[i] - mean_p) for i in range(12))
        den_w = sum((w - mean_w) ** 2 for w in weights) ** 0.5
        den_p = sum((p - mean_p) ** 2 for p in profile) ** 0.5
        return num / (den_w * den_p) if den_w and den_p else 0.0

    best = (-2.0, 0, "major")
    for tonic in range(12):
        for mode, profile in (("major", _MAJOR_PROFILE), ("minor", _MINOR_PROFILE)):
            score = correlation(profile, tonic)
            if score > best[0]:
                best = (score, tonic, mode)
    _, tonic, mode = best
    major_tonic = tonic if mode == "major" else (tonic + 3) % 12
    return _TONIC_TO_FIFTHS_MAJOR[major_tonic], mode


# --- the notated model ----------------------------------------------------


@dataclass
class NPitch:
    midi: int
    step: str
    alter: int
    octave: int
    note_id: str | None = None
    tie_start: bool = False
    tie_stop: bool = False


@dataclass
class NEvent:
    """One chord or rest in one voice. `start` is relative to the bar."""

    start: Fraction
    duration: Fraction              # sounding length in quarter notes
    pitches: list[NPitch] = field(default_factory=list)   # empty means rest
    note_type: str = "quarter"      # the printed value, before any tuplet ratio
    dots: int = 0
    tuplet: tuple[int, int] | None = None     # (actual, normal), e.g. (3, 2)
    tuplet_start: bool = False
    tuplet_stop: bool = False
    articulations: tuple[str, ...] = ()
    dynamic: str | None = None
    rolled: bool = False
    hidden: bool = False            # spacer rest in a secondary voice
    whole_bar_rest: bool = False

    @property
    def is_rest(self) -> bool:
        return not self.pitches


@dataclass
class NVoice:
    number: int
    events: list[NEvent] = field(default_factory=list)


@dataclass
class NMeasure:
    number: int
    length: Fraction
    numerator: int
    denominator: int
    fifths: int
    mode: str
    show_time: bool = False
    show_key: bool = False
    is_pickup: bool = False
    tempo_bpm: float | None = None           # metronome mark at the bar start
    # Every tempo change that falls in this bar, as (offset, bpm, marked).
    # `marked` ones are printed as a metronome mark; the rest are playback-only,
    # because a rubato performance has hundreds and a page of them is unreadable.
    # None of them may be dropped: they are what makes seconds round-trip.
    tempo_changes: list[tuple[Fraction, float, bool]] = field(default_factory=list)
    staves: dict[int, list[NVoice]] = field(default_factory=dict)
    pedal_starts: list[Fraction] = field(default_factory=list)
    pedal_stops: list[Fraction] = field(default_factory=list)


@dataclass
class NotatedScore:
    title: str
    composer: str
    measures: list[NMeasure]
    warnings: list[str] = field(default_factory=list)
    key_was_estimated: bool = False


# --- duration vocabulary ---------------------------------------------------

_TYPES: list[tuple[Fraction, str]] = [
    (F(8), "breve"), (F(4), "whole"), (F(2), "half"), (F(1), "quarter"),
    (F(1, 2), "eighth"), (F(1, 4), "16th"), (F(1, 8), "32nd"), (F(1, 16), "64th"),
]
_NOTATABLE: dict[Fraction, tuple[str, int]] = {}
for _value, _name in _TYPES:
    _NOTATABLE[_value] = (_name, 0)
    _NOTATABLE[_value * F(3, 2)] = (_name, 1)


def _base_unit(duration: Fraction) -> Fraction:
    """The undotted value a printed duration is built on."""
    _, dots = _NOTATABLE[duration]
    return duration / F(3, 2) if dots else duration


def _is_compound(numerator: int, denominator: int) -> bool:
    return denominator >= 8 and numerator % 3 == 0 and numerator > 3


def _primary_boundaries(numerator: int, denominator: int, length: Fraction) -> list[Fraction]:
    """The divisions a reader's eye must be able to find in the bar."""
    unit = F(4, denominator)
    if _is_compound(numerator, denominator):
        step = unit * 3
    elif numerator % 2 == 0 and numerator >= 4:
        step = unit * (numerator // 2)
    else:
        return []
    out, x = [], step
    while x < length:
        out.append(x)
        x += step
    return out


def _boundary_levels(numerator: int, denominator: int, length: Fraction) -> list[Fraction]:
    """Grid steps from strongest to weakest, for choosing where to split."""
    unit = F(4, denominator)
    steps: list[Fraction] = []
    if _is_compound(numerator, denominator):
        steps.append(unit * 3)
    elif numerator % 2 == 0 and numerator >= 4:
        steps.append(unit * (numerator // 2))
    step = unit
    while step >= F(1, 16):
        if step not in steps:
            steps.append(step)
        step /= 2
    return steps


def _allowed(start: Fraction, duration: Fraction, m: NMeasure) -> bool:
    """May a single printed note of `duration` begin at `start`?"""
    if duration not in _NOTATABLE:
        return False
    base = _base_unit(duration)
    if start % (base / 2) != 0:
        return False
    end = start + duration
    compound = _is_compound(m.numerator, m.denominator)
    for boundary in _primary_boundaries(m.numerator, m.denominator, m.length):
        if start < boundary < end:
            # Crossing the middle of the bar hides the beat unless the note
            # starts squarely on its own grid.
            if start % base != 0:
                return False
            if compound and duration % (F(4, m.denominator) * 3) != 0:
                return False
    return True


def _split_metric(start: Fraction, end: Fraction, m: NMeasure) -> list[tuple[Fraction, Fraction]]:
    """Break [start, end) into printable pieces that respect the beat."""
    if end <= start:
        return []
    if _allowed(start, end - start, m):
        return [(start, end)]
    for step in _boundary_levels(m.numerator, m.denominator, m.length):
        first = (floor(start / step) + 1) * step
        if first < end:
            # Several boundaries at this level: cut at the one that leaves the
            # longest printable head, which keeps tie chains short.
            cut = first
            candidate = first
            while candidate < end:
                if _allowed(start, candidate - start, m):
                    cut = candidate
                candidate += step
            return _split_metric(start, cut, m) + _split_metric(cut, end, m)
    # Finer than a 64th: print the nearest value and accept the rounding.
    return [(start, end)]


# --- quantisation ----------------------------------------------------------

_GRIDS: list[tuple[int, float]] = [(4, 0.0), (3, 0.0004), (8, 0.0020), (6, 0.0030)]


def _choose_grid(fractions_in_window: list[float]) -> int:
    best_div, best_cost = 4, float("inf")
    for div, penalty in _GRIDS:
        cost = penalty
        for f in fractions_in_window:
            nearest = round(f * div) / div
            cost += (f - nearest) ** 2
        if cost < best_cost - 1e-12:
            best_div, best_cost = div, cost
    return best_div


@dataclass
class _QNote:
    note: Note
    start: Fraction
    end: Fraction
    staff: int
    staccato: bool = False


def _beat_of(note: Note, timeline: Timeline) -> tuple[float, float]:
    if note.beat is not None and note.beats is not None and note.beats > 0:
        return note.beat, note.beat + note.beats
    start = timeline.beat_at(note.onset)
    return start, timeline.beat_at(note.onset + note.duration)


Grids = dict[tuple[int, int], int]  # (bar, beat window within the bar) -> subdivisions


def _quantise(
    score: Score, timeline: Timeline, split_pitch: int
) -> tuple[list[_QNote], dict[int, Grids], int]:
    """Snap every note to a per-beat grid. Returns notes, grids[staff], moved count.

    A window is one quarter note, counted from the start of its bar, so a
    barline always falls on a window edge. The last window of a bar whose
    length is not a whole number of quarters (9/8, 3/8) is short, and short
    windows only ever use the binary grid: a triplet that the barline cuts in
    half cannot be written down.
    """
    raw: list[tuple[Note, float, float, int]] = []
    for n in score.notes:
        start, end = _beat_of(n, timeline)
        staff = n.staff if n.staff in (1, 2) else (1 if n.pitch >= split_pitch else 2)
        raw.append((n, max(0.0, start), max(0.0, end), staff))

    def locate(value: float) -> tuple[int, int, float, float]:
        """(bar, window, fraction into the window, bar start) for an absolute beat."""
        bar = timeline.bar_at(value)
        bar_start = timeline.bar_start(bar)
        rel = max(0.0, value - bar_start)
        k = floor(rel + 1e-9)
        return bar, k, rel - k, bar_start

    windows: dict[int, dict[tuple[int, int], list[float]]] = {1: {}, 2: {}}
    for _, start, _end, staff in raw:
        bar, k, f, _ = locate(start)
        windows[staff].setdefault((bar, k), []).append(f)

    grids: dict[int, Grids] = {1: {}, 2: {}}
    for staff, by_window in windows.items():
        for (bar, k), fractions_in in by_window.items():
            full = timeline.bar_beats(bar) - k >= 1 - 1e-9
            grids[staff][(bar, k)] = _choose_grid(fractions_in) if full else 4

    def snap(value: float, staff: int) -> tuple[Fraction, Fraction]:
        bar, k, f, bar_start = locate(value)
        div = grids[staff].get((bar, k), 4)
        start_f = F(bar_start).limit_denominator(960)
        return start_f + k + F(round(f * div), div), F(1, div)

    out: list[_QNote] = []
    moved = 0
    for n, start, end, staff in raw:
        qs, unit = snap(start, staff)
        qe, _ = snap(end, staff)
        if qe <= qs:
            qe = qs + unit
        if abs(float(qs) - start) > 0.02 or abs(float(qe) - end) > 0.02:
            moved += 1
        out.append(_QNote(n, qs, qe, staff))
    return out, grids, moved


def _close_small_gaps(notes: list[_QNote]) -> None:
    """Make durations readable.

    Performed notes are released early; printing that literally gives a page
    of sixteenth rests. If a note sounds for most of the time until the next
    onset on its staff, print it as filling that time. If it is much shorter
    but the beat is short too, fill it and mark it staccato, which is what the
    player did. A short note followed by a long silence keeps its rest.
    """
    for staff in (1, 2):
        onsets = sorted({q.start for q in notes if q.staff == staff})
        for q in notes:
            if q.staff != staff:
                continue
            index = bisect_right(onsets, q.start)
            if index >= len(onsets):
                continue
            nxt = onsets[index]
            if q.end >= nxt:
                continue
            ioi = nxt - q.start
            sounded = q.end - q.start
            if sounded * 2 >= ioi and ioi <= 4:
                q.end = nxt                  # held for most of it: print it full
            elif ioi <= 1:
                q.end = nxt                  # a short note on a short beat is staccato
                if "staccato" not in q.note.articulations:
                    q.staccato = True
            # Otherwise the silence is long enough to be a real rest.


# --- voices ----------------------------------------------------------------


@dataclass
class _Chord:
    start: Fraction
    end: Fraction
    members: list[_QNote]


def _assign_voices(notes: list[_QNote], staff: int) -> tuple[list[list[_Chord]], int]:
    """At most two voices per staff. Returns voices and the count of forced edits."""
    groups: dict[tuple[Fraction, Fraction], list[_QNote]] = {}
    for q in notes:
        groups.setdefault((q.start, q.end), []).append(q)

    def top(members: list[_QNote]) -> int:
        return max(m.note.pitch for m in members)

    ordered = sorted(
        groups.items(),
        key=lambda kv: (kv[0][0], -top(kv[1]) if staff == 1 else top(kv[1]), -(kv[0][1] - kv[0][0])),
    )
    voices: list[list[_Chord]] = [[], []]
    forced = 0
    for (start, end), members in ordered:
        placed = False
        for voice in voices:
            if not voice or voice[-1].end <= start:
                voice.append(_Chord(start, end, list(members)))
                placed = True
                break
        if placed:
            continue
        forced += 1
        primary = voices[0][-1]
        if primary.start == start:
            primary.members.extend(members)       # same attack: fold into the chord
        elif start - primary.start >= F(1, 8):
            primary.end = start                    # cut the earlier note short
            voices[0].append(_Chord(start, end, list(members)))
        else:
            primary.members.extend(members)
    return voices, forced


# --- assembling measures ----------------------------------------------------


def _tuplet_pieces(start: Fraction, end: Fraction, div: int) -> list[tuple[Fraction, Fraction, str, int]]:
    """Pieces of one tuplet window: (start, end, printed type, dots)."""
    unit = F(1, div)
    # Printed value = sounding value scaled back up by the tuplet ratio.
    ratio = F(3, 2)
    out = []
    cursor = start
    while cursor < end:
        remaining = end - cursor
        for units in (4, 3, 2, 1) if div == 6 else (2, 1):
            length = unit * units
            printed = length * ratio
            if length <= remaining and printed in _NOTATABLE:
                name, dots = _NOTATABLE[printed]
                out.append((cursor, cursor + length, name, dots))
                cursor += length
                break
        else:
            name, dots = _NOTATABLE.get(unit * ratio, ("16th", 0))
            out.append((cursor, cursor + unit, name, dots))
            cursor += unit
    return out


def _articulations(members: list[_QNote]) -> tuple[str, ...]:
    seen: list[str] = []
    for q in members:
        for a in q.note.articulations:
            if a not in seen:
                seen.append(a)
        if q.staccato and "staccato" not in seen:
            seen.append("staccato")
    return tuple(seen)


_MAJOR_TONICS = {-7: "C-flat", -6: "G-flat", -5: "D-flat", -4: "A-flat", -3: "E-flat", -2: "B-flat", -1: "F",
                 0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F-sharp", 7: "C-sharp"}
_MINOR_TONICS = {-7: "A-flat", -6: "E-flat", -5: "B-flat", -4: "F", -3: "C", -2: "G", -1: "D",
                 0: "A", 1: "E", 2: "B", 3: "F-sharp", 4: "C-sharp", 5: "G-sharp", 6: "D-sharp", 7: "A-sharp"}


def key_name(fifths: int, mode: str) -> str:
    """The name of a key from its signature: (3, "minor") is F-sharp minor."""
    table = _MINOR_TONICS if mode == "minor" else _MAJOR_TONICS
    return f"{table[max(-7, min(7, fifths))]} {mode}"


def notate(score: Score, *, split_pitch: int = 60, max_measures: int = 4000) -> NotatedScore:
    """Convert a Score into printable measures."""
    warnings: list[str] = []
    timeline = score.timeline
    if timeline is None:
        timeline = Timeline.constant(score.tempo_bpm or 120.0)
        warnings.append("The source had no meter, so 4/4 was assumed for the barlines.")

    estimated = False
    if not timeline.keys:
        fifths, mode = estimate_key(score.notes)
        estimated = True
        warnings.append(
            f"No key signature was stated; {key_name(fifths, mode)} was estimated from the notes."
        )
        default_key = (fifths, mode)
    else:
        default_key = (timeline.keys[0].fifths, timeline.keys[0].mode)

    qnotes, grids, moved = _quantise(score, timeline, split_pitch)
    if moved:
        warnings.append(
            f"{moved} note(s) were moved to the nearest notatable position; "
            "the printed rhythm is an approximation of the performed timing."
        )
    _close_small_gaps(qnotes)

    last_beat = max((q.end for q in qnotes), default=ZERO)
    # Step well inside the final note: bar_at() nudges forward to absorb float
    # fuzz, so a piece ending exactly on a barline would otherwise gain an
    # empty bar.
    n_bars = timeline.bar_at(float(last_beat) - 1e-3) if last_beat > 0 else 1
    if n_bars > max_measures:
        warnings.append(f"Only the first {max_measures} bars were notated.")
        n_bars = max_measures

    measures: list[NMeasure] = []
    previous_meter: tuple[int, int] | None = None
    previous_key: tuple[int, str] | None = None
    last_tempo: float | None = None
    for bar in range(1, n_bars + 1):
        start = F(timeline.bar_start(bar)).limit_denominator(960)
        length = F(timeline.bar_beats(bar)).limit_denominator(960)
        num, den = timeline.meter_at_bar(bar)
        key = timeline.key_at(float(start))
        fifths, mode = (key.fifths, key.mode) if key else default_key
        m = NMeasure(
            number=bar, length=length, numerator=num, denominator=den, fifths=fifths, mode=mode,
            show_time=(num, den) != previous_meter, show_key=(fifths, mode) != previous_key,
            is_pickup=(bar == 1 and timeline.pickup_beats > 0),
        )
        bpm = timeline.bpm_at(float(start))
        if last_tempo is None or abs(bpm - last_tempo) / last_tempo >= 0.08:
            m.tempo_bpm = bpm
            last_tempo = bpm
        previous_meter, previous_key = (num, den), (fifths, mode)
        measures.append(m)

    # Tempo changes keep their exact position. Only a change of 8% or more
    # from the last printed mark is printed; smaller ones still reach playback.
    printed: float | None = None
    for tempo in timeline.tempos:
        beat = F(tempo.beat).limit_denominator(960)
        bar = timeline.bar_at(float(beat))
        if bar > n_bars:
            continue
        m = measures[bar - 1]
        offset = min(max(ZERO, beat - F(timeline.bar_start(bar)).limit_denominator(960)), m.length)
        marked = printed is None or abs(tempo.bpm - printed) / printed >= 0.08
        if marked:
            printed = tempo.bpm
        m.tempo_changes.append((offset, tempo.bpm, marked))

    bar_starts = [F(timeline.bar_start(b)).limit_denominator(960) for b in range(1, n_bars + 2)]
    total_forced = 0
    clipped = 0
    continuation: Counter[str] = Counter()  # per-note tie segment counter

    for staff in (1, 2):
        staff_notes = [q for q in qnotes if q.staff == staff]
        voices, forced = _assign_voices(staff_notes, staff)
        total_forced += forced
        staff_grids = grids.get(staff, {})

        for vi, voice in enumerate(voices):
            voice_number = (1 if staff == 1 else 5) + vi
            per_bar: dict[int, list[NEvent]] = {}
            for chord in voice:
                # Walk the chord across barlines, tying each continuation.
                cursor = chord.start
                first_piece = True
                while cursor < chord.end:
                    bar = timeline.bar_at(float(cursor))
                    if bar > n_bars:
                        clipped += 1
                        break
                    m = measures[bar - 1]
                    bar_start, bar_end = bar_starts[bar - 1], bar_starts[bar]
                    seg_end = min(chord.end, bar_end)
                    pieces = _chord_pieces(cursor - bar_start, seg_end - bar_start, m, staff_grids)
                    for pi, (s, e, name, dots, tuplet) in enumerate(pieces):
                        is_first = first_piece and pi == 0
                        is_last = seg_end == chord.end and pi == len(pieces) - 1
                        pitches = []
                        for q in sorted(chord.members, key=lambda q: q.note.pitch):
                            step, alter, octave = spell(q.note.pitch, m.fifths, q.note.spelling, m.mode)
                            note_id = q.note.id
                            if note_id and not is_first:
                                continuation[note_id] += 1
                                note_id = f"{note_id}-t{continuation[note_id]}"
                            pitches.append(
                                NPitch(
                                    q.note.pitch, step, alter, octave, note_id=note_id,
                                    tie_start=not is_last, tie_stop=not is_first,
                                )
                            )
                        # De-duplicate a pitch struck twice in one chord.
                        unique: dict[int, NPitch] = {}
                        for p in pitches:
                            unique.setdefault(p.midi, p)
                        per_bar.setdefault(bar, []).append(
                            NEvent(
                                s, e - s, list(unique.values()), note_type=name, dots=dots, tuplet=tuplet,
                                articulations=_articulations(chord.members) if is_first else (),
                                dynamic=next((q.note.dynamic for q in chord.members if q.note.dynamic), None)
                                if is_first else None,
                                rolled=is_first and any(q.note.rolled for q in chord.members),
                            )
                        )
                    first_piece = False
                    cursor = seg_end

            for m in measures:
                events = sorted(per_bar.get(m.number, []), key=lambda ev: ev.start)
                if not events and vi > 0:
                    continue  # a secondary voice exists only where it has notes
                filled: list[NEvent] = []
                cursor = ZERO
                for ev in events:
                    if ev.start > cursor:
                        filled.extend(_gap_rests(cursor, ev.start, m, staff_grids, vi > 0))
                    filled.append(ev)
                    cursor = max(cursor, ev.start + ev.duration)
                if not events:
                    name, dots = _NOTATABLE.get(m.length, ("whole", 0))
                    filled.append(NEvent(ZERO, m.length, note_type=name, dots=dots, whole_bar_rest=True))
                elif cursor < m.length:
                    filled.extend(_gap_rests(cursor, m.length, m, staff_grids, vi > 0))
                _mark_tuplet_groups(filled)
                m.staves.setdefault(staff, []).append(NVoice(voice_number, filled))

    if total_forced:
        warnings.append(
            f"{total_forced} overlapping note(s) were simplified to fit two voices per staff."
        )
    if clipped:
        warnings.append(f"{clipped} note(s) beyond the last notated bar were left out.")

    for pedal in score.pedals:
        for t, bucket in ((pedal.start, "pedal_starts"), (pedal.end, "pedal_stops")):
            beat = F(timeline.beat_at(t)).limit_denominator(24)
            bar = min(max(1, timeline.bar_at(float(beat))), n_bars)
            offset = min(max(ZERO, beat - bar_starts[bar - 1]), measures[bar - 1].length)
            getattr(measures[bar - 1], bucket).append(offset)

    warnings.extend(w for w in score.warnings if w not in warnings)
    return NotatedScore(
        title=score.title, composer=score.composer, measures=measures,
        warnings=warnings, key_was_estimated=estimated,
    )


def _chord_pieces(
    start: Fraction, end: Fraction, m: NMeasure, grids: Grids
) -> list[tuple[Fraction, Fraction, str, int, tuple[int, int] | None]]:
    """Split one in-bar span into printable pieces, honouring tuplet beats.

    A beat the quantiser put on a triplet grid must be written as a tuplet -
    unless this span covers the whole beat, in which case it is just a quarter
    note and may merge with its neighbours.
    """
    out: list[tuple[Fraction, Fraction, str, int, tuple[int, int] | None]] = []

    def window(at: Fraction) -> tuple[int, Fraction, Fraction]:
        k = floor(at)
        return grids.get((m.number, k), 4), F(k), min(F(k) + 1, m.length)

    def needs_tuplet(at: Fraction) -> bool:
        div, w_start, w_end = window(at)
        covers_whole = at <= w_start and end >= w_end
        return div in (3, 6) and not covers_whole

    cursor = start
    while cursor < end:
        div, _w_start, w_end = window(cursor)
        if needs_tuplet(cursor):
            seg_end = min(end, w_end)
            ratio = (3, 2) if div == 3 else (6, 4)
            for s, e, name, dots in _tuplet_pieces(cursor, seg_end, div):
                out.append((s, e, name, dots, ratio))
            cursor = seg_end
            continue
        run_end = min(end, w_end)
        while run_end < end and not needs_tuplet(run_end):
            run_end = min(end, window(run_end)[2])
        for s, e in _split_metric(cursor, run_end, m):
            name, dots = _NOTATABLE.get(e - s, ("quarter", 0))
            out.append((s, e, name, dots, None))
        cursor = run_end
    return out


def _gap_rests(
    start: Fraction, end: Fraction, m: NMeasure, grids: Grids, hidden: bool
) -> list[NEvent]:
    return [
        NEvent(s, e - s, note_type=name, dots=dots, tuplet=tuplet, hidden=hidden)
        for s, e, name, dots, tuplet in _chord_pieces(start, end, m, grids)
    ]


def _mark_tuplet_groups(events: list[NEvent]) -> None:
    """Flag the first and last event of each tuplet group (one group per beat)."""
    previous: NEvent | None = None
    for ev in events:
        if ev.tuplet:
            fresh = (
                previous is None
                or not previous.tuplet
                or (previous.start + previous.duration) % 1 == 0
            )
            if fresh:
                ev.tuplet_start = True
                if previous is not None and previous.tuplet:
                    previous.tuplet_stop = True
        elif previous is not None and previous.tuplet:
            previous.tuplet_stop = True
        previous = ev
    if previous is not None and previous.tuplet:
        previous.tuplet_stop = True
