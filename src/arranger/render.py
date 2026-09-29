"""Turn a plan into notes.

Deterministic. Same plan plus same source always gives the same Score. No
model involved, no randomness, no cleverness. If the arrangement is bad, that
is a bad *plan*, and the plan is a small readable file you can inspect.

The pipeline:
    source Score -> melody line + chord segments -> left hand from pattern
                 -> combined Score -> verifier

The renderer is not trying to be a good arranger; it is trying to be a
*predictable* one, so that when something sounds wrong you can point at the
decision that caused it. What it does guarantee:

- the melody is never dropped or thinned, only moved by whole octaves;
- left-hand figures follow the meter (three pulses in 3/4, six in 6/8) when
  the source has one, and fall back to four equal steps when it does not;
- the left hand only plays while the source is sounding, so a silent bar
  stays silent and a final short chord is not padded to the barline;
- every output note has a stable id and, where it derives from a source note,
  a `source_id` saying which.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .analysis import (
    TEMPLATES,
    ChordSegment,
    bar_spans,
    detect_chord_segments,
    detect_chords,
    extract_melody,
    last_bar,
)
from .ir import Note, PedalSpan, Score
from .plan import ArrangementPlan, BassMode, HarmonicRhythm, LHPattern, Section, Voicing
from .timeline import TempoChange, Timeline

__all__ = [
    "TEMPLATES", "RenderError", "detect_chords", "extract_melody", "last_bar", "render",
]

ROLL_STAGGER_SECONDS = 0.03
_LH_VELOCITY_RATIO = 0.8


class RenderError(ValueError):
    pass


# --- pulses: where in the bar the left hand may strike --------------------


@dataclass(frozen=True)
class _Pulse:
    onset: float                # seconds
    duration: float
    beat: float | None          # quarter-note beats, when the source has a timeline
    beats: float | None
    strong: bool


def _pulses(source: Score, bar: int, start: float, end: float) -> list[_Pulse]:
    """The rhythmic grid a left-hand figure is laid on, for one bar.

    Simple meters pulse on the beat, except two-beat bars, which pulse on the
    half-beat so a four-note figure still fits. Compound meters pulse on the
    eighth, grouped in threes. With no timeline the bar is cut in four.
    """
    timeline = source.timeline
    if timeline is None:
        step = max(end - start, 0.05) / 4
        return [_Pulse(start + i * step, step, None, None, i % 2 == 0) for i in range(4)]

    num, den = timeline.meter_at_bar(bar)
    bar_start = timeline.bar_start(bar)
    length = timeline.bar_beats(bar)
    unit = 4.0 / den
    compound = den >= 8 and num % 3 == 0 and num > 3
    if compound:
        step, group = unit, 3               # 6/8: strong on 1 and 4
    elif num == 2:
        step, group = unit / 2, 2           # 2/4 pulses in eighths: strong on each beat
    elif num % 2 == 0:
        step, group = unit, num // 2        # 4/4: strong on 1 and 3
    else:
        step, group = unit, num             # 3/4, 5/4: strong on the downbeat only

    out: list[_Pulse] = []
    # A pickup bar is the *end* of a bar: count its pulses from the far side
    # so the strong beats line up with the full bars that follow.
    full = num * unit
    offset = (full - length) if length < full - 1e-9 else 0.0
    index = round(offset / step)
    position = 0.0
    while position < length - 1e-9:
        size = min(step, length - position)
        on = timeline.seconds_at(bar_start + position)
        off = timeline.seconds_at(bar_start + position + size)
        out.append(_Pulse(on, off - on, bar_start + position, size, index % group == 0))
        position += size
        index += 1
    return out


# --- voicing ---------------------------------------------------------------


def _voice(root_pc: int, quality: str, octave: int, voices: int) -> list[int]:
    """Chord tones as MIDI pitches, low to high, root position."""
    base = 12 * (octave + 1) + root_pc
    offsets = TEMPLATES[quality][:max(1, voices)]
    return [base + o for o in offsets]


def _nearest(pc: int, target: float) -> int:
    """The pitch with pitch class `pc` closest to `target`."""
    base = int(round(target))
    candidates = [base + d for d in range(-6, 7) if (base + d) % 12 == pc % 12]
    return min(candidates, key=lambda p: (abs(p - target), p))


def _voicing(
    section: Section, segment: ChordSegment, previous: list[int] | None
) -> list[int]:
    """The pitches available to the pattern for this chord, low to high.

    Root voicing stacks the chord upward from the root. With `bass: source`
    the bottom note is whatever the source really has in the bass, which keeps
    first inversions and pedal points. Smooth voicing places each tone as near
    the previous chord as it can, so the hand barely moves; the first chord of
    a section is always root position, so the harmony is stated plainly before
    it starts to glide.
    """
    root_position = _voice(segment.root, segment.quality, section.lh_octave, section.lh_voices)
    bass_pc = segment.bass_pc if section.bass == BassMode.SOURCE else segment.root

    if section.voicing == Voicing.SMOOTH and previous and len(root_position) > 1:
        centre = sum(previous) / len(previous)
        pcs = [p % 12 for p in root_position]
        if bass_pc not in pcs:
            pcs[0] = bass_pc
        placed = sorted({_nearest(pc, centre) for pc in pcs})
        floor = 12 * (section.lh_octave + 1) - 7
        while placed[0] < floor:
            placed = sorted(p + 12 if p == placed[0] else p for p in placed)
        return placed

    if bass_pc == segment.root:
        return root_position
    bass = 12 * (section.lh_octave + 1) + bass_pc
    if bass > root_position[0] + 6:
        bass -= 12   # keep the bass under the chord, not in the middle of it
    upper = [p if p > bass else p + 12 for p in root_position if p % 12 != bass_pc]
    return sorted([bass, *upper])[: max(1, section.lh_voices)]


# --- left hand realisation ------------------------------------------------


def _figure(pattern: LHPattern, pitches: list[int], count: int, next_root: int | None) -> list[list[int]]:
    """Which pitches sound on each of `count` pulses. One inner list per pulse."""
    root = pitches[0]
    third = pitches[min(1, len(pitches) - 1)]
    fifth = pitches[min(2, len(pitches) - 1)]

    if pattern == LHPattern.BROKEN_OCTAVE:
        cycle = [[root], [root + 12]]
    elif pattern == LHPattern.ARPEGGIO:
        cycle = [[root], [fifth], [root + 12], [third]] if count != 3 else [[root], [fifth], [root + 12]]
    elif pattern == LHPattern.ALBERTI:
        cycle = [[root], [fifth], [third], [fifth]] if count != 3 else [[root], [fifth], [third]]
    elif pattern == LHPattern.BROKEN_TENTH:
        tenth = root + 12 + (third - root)
        cycle = [[root], [root + 7], [tenth], [root + 7]] if count != 3 else [[root], [root + 7], [tenth]]
    elif pattern == LHPattern.WALKING:
        line = [[p] for p in (pitches[:4] or [root])]
        if next_root is not None and count >= 4 and next_root % 12 != root % 12:
            # Approach the next root by step from whichever side is nearer.
            target = _nearest(next_root % 12, line[-1][0])
            line = (line * 2)[: count - 1] + [[target - 1 if target > line[-1][0] else target + 1]]
        cycle = line
    else:
        raise RenderError(f"unhandled pattern {pattern}")
    return [cycle[i % len(cycle)] for i in range(count)]


def _left_hand_for_segment(
    section: Section,
    segment: ChordSegment,
    pulses: list[_Pulse],
    pitches: list[int],
    next_root: int | None,
    active: tuple[float, float],
) -> list[Note]:
    """Realise one chord segment according to the section's pattern."""
    start = max(segment.start, active[0])
    end = min(segment.end, active[1])
    if end - start < 0.02:
        return []
    out: list[Note] = []

    def add(pitch: int, onset: float, duration: float, *, beat=None, beats=None,
            rolled: bool = False, bass: bool = False) -> None:
        out.append(
            Note(pitch=pitch, onset=onset, duration=max(duration, 0.05), staff=2,
                 beat=beat, beats=beats, rolled=rolled,
                 role="bass" if bass else "accompaniment")
        )

    pattern = section.lh_pattern
    beat = beats = None
    if segment.start_beat is not None and segment.end_beat is not None and start == segment.start:
        beat, beats = segment.start_beat, segment.end_beat - segment.start_beat
        if end < segment.end:
            beats = None   # cut short by silence: let the exporter measure it

    if pattern == LHPattern.PEDAL_TONE:
        add(pitches[0], start, end - start, beat=beat, beats=beats, bass=True)
        return out

    if pattern == LHPattern.BLOCK:
        roll = section.roll_wide_chords and len(pitches) > 1
        for i, p in enumerate(pitches):
            # Stagger by 30ms. This is not cosmetic: rolled notes are not
            # simultaneous, so the hand-span rule stops applying to them.
            # It is the cheapest legal fix for a wide chord.
            shift = i * ROLL_STAGGER_SECONDS if roll else 0.0
            add(p, start + shift, end - start - shift, beat=beat, beats=beats,
                rolled=roll, bass=i == 0)
        return out

    mine = [p for p in pulses if segment.start - 1e-9 <= p.onset < segment.end - 1e-9
            and active[0] - 1e-9 <= p.onset < active[1] - 1e-9]
    if not mine:
        return out

    if pattern == LHPattern.STRIDE:
        chord = [p + 12 for p in pitches[1:]] or [pitches[0] + 12]
        roll = section.roll_wide_chords and len(chord) > 1
        for i, pulse in enumerate(mine):
            if pulse.strong or i == 0:
                add(pitches[0], pulse.onset, pulse.duration, beat=pulse.beat, beats=pulse.beats, bass=True)
            else:
                for j, p in enumerate(chord):
                    shift = j * ROLL_STAGGER_SECONDS if roll else 0.0
                    add(p, pulse.onset + shift, pulse.duration - shift, beat=pulse.beat,
                        beats=pulse.beats, rolled=roll)
        return out

    for i, (pulse, sounding) in enumerate(zip(mine, _figure(pattern, pitches, len(mine), next_root), strict=True)):
        for p in sounding:
            add(p, pulse.onset, pulse.duration, beat=pulse.beat, beats=pulse.beats, bass=i == 0)
    return out


# --- the public entry point ---------------------------------------------


def _active_window(source: Score, start: float, end: float) -> tuple[float, float] | None:
    """The part of [start, end) during which the source is making sound."""
    first: float | None = None
    last: float | None = None
    for n in source.sounding_at(start):
        first = start
        last = max(last or start, min(n.offset, end))
    for n in source.notes:
        if n.onset >= end:
            break
        if n.onset >= start:
            first = n.onset if first is None else min(first, n.onset)
            last = max(last or n.onset, min(n.offset, end))
    if first is None or last is None:
        return None
    return first, last


def render(plan: ArrangementPlan, source: Score) -> Score:
    """Apply a plan to a source, producing an arrangement.

    Raises RenderError on an invalid plan rather than rendering something
    misleading. A clear failure here is cheaper than a plausible-looking score
    that turns out to encode a contradiction.
    """
    if problems := plan.validate():
        raise RenderError("invalid plan: " + "; ".join(problems))
    if not source.notes:
        raise RenderError("source score is empty")

    melody = extract_melody(source)
    segments = detect_chord_segments(source, melody)
    spans = bar_spans(source)
    timeline = source.timeline

    out: list[Note] = []

    # Right hand: the melody, shifted and optionally folded. The melody is
    # never dropped or thinned - see CLAUDE.md. Folding moves notes by whole
    # octaves, so every pitch class survives; the tune is recognisable even
    # where its contour is compressed.
    fold_centres: dict[int, float] = {}
    for i, section in enumerate(plan.sections):
        if section.melody_fold_window:
            in_section = [
                n.pitch for n in melody
                if n.bar and section.start_bar <= n.bar <= section.end_bar
            ]
            if in_section:
                # The window is centred on where the melody ends up, not where
                # it started. Centred on the unshifted line, a section moved
                # down an octave to fit the keyboard was folded straight back up.
                fold_centres[i] = sorted(in_section)[len(in_section) // 2] + section.melody_shift

    section_index = {id(s): i for i, s in enumerate(plan.sections)}
    melody_velocity: dict[int, list[int]] = {}
    for k, n in enumerate(melody):
        section = plan.section_for_bar(n.bar) if n.bar else None
        pitch = n.pitch + (section.melody_shift if section else 0)

        if section is not None and section.melody_fold_window:
            centre = fold_centres.get(section_index[id(section)])
            if centre is not None:
                half = section.melody_fold_window / 2
                # Octaves only. Any other interval would change the note.
                while pitch - centre > half:
                    pitch -= 12
                while centre - pitch > half:
                    pitch += 12

        moved = pitch - n.pitch
        beat, beats = n.beat, n.beats
        if timeline is not None:
            beat = timeline.beat_at(n.onset) if beat is None else beat
            if beats is None:
                beats = max(timeline.beat_at(n.onset + n.duration) - beat, 1e-3)
        out.append(
            replace(
                n, pitch=pitch, staff=1, id=f"m{k}", beat=beat, beats=beats,
                # A spelling survives an octave move; any other shift needs respelling.
                spelling=n.spelling if moved % 12 == 0 else None,
                track=None,
            )
        )
        if n.bar is not None and n.velocity:
            melody_velocity.setdefault(n.bar, []).append(n.velocity)

    # Left hand: one realisation per chord segment, from the section's pattern.
    right_hand = Score(notes=list(out))   # indexed, for "what is the melody holding right now"
    ordered_bars = sorted(spans)
    previous_voicing: list[int] | None = None
    previous_section: Section | None = None
    counter = 0
    for position, bar in enumerate(ordered_bars):
        start, end = spans[bar]
        section = plan.section_for_bar(bar)
        if section is None or section.lh_voices == 0:
            previous_voicing = None
            continue  # bar not covered, or deliberately melody-only
        bar_segments = segments.get(bar)
        if not bar_segments:
            continue
        active = _active_window(source, start, end) if timeline is not None else (start, end)
        if active is None:
            continue  # the source is silent here; so is the left hand
        if section.harmonic_rhythm == HarmonicRhythm.BAR and len(bar_segments) > 1:
            longest = max(bar_segments, key=lambda s: s.end - s.start)
            first = bar_segments[0]
            bar_segments = [
                replace(first, end=bar_segments[-1].end, end_beat=bar_segments[-1].end_beat,
                        root=longest.root, quality=longest.quality)
            ]
        if section is not previous_section:
            previous_voicing = None   # a new section states its harmony in root position
        previous_section = section

        pulses = _pulses(source, bar, start, end)
        velocities = melody_velocity.get(bar)
        lh_velocity = (
            max(30, min(110, round(_LH_VELOCITY_RATIO * sum(velocities) / len(velocities))))
            if velocities else None
        )
        for index, segment in enumerate(bar_segments):
            pitches = _voicing(section, segment, previous_voicing)
            previous_voicing = pitches
            next_root: int | None = None
            if index + 1 < len(bar_segments):
                next_root = bar_segments[index + 1].root
            elif position + 1 < len(ordered_bars):
                following = segments.get(ordered_bars[position + 1])
                next_root = following[0].root if following else None
            for note in _left_hand_for_segment(section, segment, pulses, pitches, next_root, active):
                # The left hand stays under the tune. A figure that climbs onto
                # the very key the melody is holding is a collision on a real
                # keyboard and a doubling in any case, and doublings go first:
                # move it down by octaves until it is clear.
                ceiling = min((m.pitch for m in right_hand.sounding_at(note.onset)), default=None)
                pitch = note.pitch
                while ceiling is not None and pitch >= ceiling and pitch - 12 >= 0:
                    pitch -= 12
                out.append(replace(note, pitch=pitch, bar=bar, id=f"l{counter}", velocity=lh_velocity))
                counter += 1

    pedals: list[PedalSpan] = []
    for bar in sorted(set(plan.pedal_bars)):
        if bar in spans:
            start, end = spans[bar]
            if end > start:
                pedals.append(PedalSpan(start, end))

    tempo_bpm = source.tempo_bpm
    scale = float(plan.tempo_scale)
    if scale != 1.0:
        # Slower, uniformly: beats are untouched, every second stretches.
        out = [replace(n, onset=n.onset / scale, duration=n.duration / scale) for n in out]
        pedals = [PedalSpan(p.start / scale, p.end / scale) for p in pedals]
        tempo_bpm = source.tempo_bpm * scale
        if timeline is not None:
            timeline = Timeline(
                [TempoChange(t.beat, t.bpm * scale) for t in timeline.tempos],
                timeline.meters, timeline.keys, timeline.pickup_beats,
            )

    return Score(
        notes=out, tempo_bpm=tempo_bpm, title=plan.title, timeline=timeline,
        pedals=pedals, composer=source.composer, source_format=source.source_format,
    )
