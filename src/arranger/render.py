"""Turn a plan into notes.

Deterministic. Same plan plus same source always gives the same Score. No
model involved, no randomness, no cleverness. If the arrangement is bad, that
is a bad *plan*, and the plan is a small readable file you can inspect.

The pipeline:
    source Score -> melody line + chord per bar -> left hand from pattern
                 -> combined Score -> verifier

Everything here is intentionally simple. The renderer is not trying to be a
good arranger; it is trying to be a *predictable* one, so that when something
sounds wrong you can point at the decision that caused it.
"""

from __future__ import annotations

from collections import Counter

from .ir import Note, Score
from .plan import ArrangementPlan, LHPattern, Section

# Chord templates as semitone offsets from the root. Deliberately few: the
# point is a usable left hand, not a jazz harmony engine.
TEMPLATES: dict[str, tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dom7": (0, 4, 7, 10),
    "min7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
    "dim": (0, 3, 6),
    "sus4": (0, 5, 7),
}


class RenderError(ValueError):
    pass


# --- analysis ------------------------------------------------------------


def extract_melody(source: Score, floor_drop: int = 9) -> list[Note]:
    """The top line: at each onset, the highest sounding note.

    Crude, and right most of the time. The melody is almost always on top in
    the music this tool targets. Where it isn't — an inner-voice tune, a bass
    melody — this will pick the wrong line, and that is a known limitation
    rather than a bug to be surprised by later.

    Two corrections make it usable on real files:

    **A floor.** Taking the top note at every onset sounds right until the
    melody rests. Then the highest sounding note is an accompaniment note two
    octaves down, and the "melody" dives into the bass and back — producing a
    right hand that spans fifteen semitones and leaps constantly. Notes more
    than `floor_drop` semitones below the median top note are treated as
    accompaniment, not melody. A rest in the melody is a rest, not an excuse
    to grab whatever is lowest.

    **Overlap merging.** A note sustained across several onsets must stay one
    note. Comparing against the previous note's *end* is wrong: a note lasting
    400ms that re-triggers 5ms later is an overlapping duplicate, not a
    repeat, and comparing to the end misses it entirely.
    """
    tops: list[tuple[float, Note]] = []
    for t in source.onsets:
        sounding = source.sounding_at(t)
        if sounding:
            tops.append((t, max(sounding, key=lambda n: n.pitch)))
    if not tops:
        return []

    pitches = sorted(n.pitch for _, n in tops)
    floor = pitches[len(pitches) // 2] - floor_drop

    melody: list[Note] = []
    for t, top in tops:
        if top.pitch < floor:
            continue  # accompaniment showing through a gap in the melody
        if melody:
            prev = melody[-1]
            if prev.pitch == top.pitch and t < prev.offset:
                melody[-1] = Note(
                    pitch=prev.pitch, onset=prev.onset,
                    duration=max(prev.duration, top.offset - prev.onset),
                    staff=1, bar=prev.bar,
                )
                continue
        melody.append(
            Note(pitch=top.pitch, onset=t, duration=top.duration, staff=1, bar=top.bar)
        )

    # A melodic line is monophonic by definition. Source durations are
    # sustained — often by pedal — so consecutive melody notes overlap, and an
    # overlapping "line" reads to the verifier as a right hand holding six
    # notes across two octaves. Clip each note where the next one starts.
    for i in range(len(melody) - 1):
        gap = melody[i + 1].onset - melody[i].onset
        if melody[i].duration > gap:
            melody[i] = Note(
                pitch=melody[i].pitch, onset=melody[i].onset,
                duration=max(gap, 0.02), staff=1, bar=melody[i].bar,
            )
    return melody


def detect_chords(source: Score, melody: list[Note]) -> dict[int, tuple[int, str]]:
    """One chord per bar: (root pitch class, quality).

    Scores every root/quality template against the pitch classes present in
    the bar. Notes below the melody are weighted double, since accompaniment
    defines the harmony more reliably than a passing melodic tone does.
    """
    melody_pitches = {(round(n.onset, 3), n.pitch) for n in melody}

    by_bar: dict[int, Counter] = {}
    bass_of_bar: dict[int, int] = {}
    for n in source.notes:
        if n.bar is None:
            continue
        weight = 1 if (round(n.onset, 3), n.pitch) in melody_pitches else 2
        by_bar.setdefault(n.bar, Counter())[n.pitch % 12] += weight
        if n.bar not in bass_of_bar or n.pitch < bass_of_bar[n.bar]:
            bass_of_bar[n.bar] = n.pitch

    chords: dict[int, tuple[int, str]] = {}
    for bar, classes in by_bar.items():
        bass_pc = bass_of_bar[bar] % 12
        best, best_score = (0, "maj"), -1e9
        for root in range(12):
            for quality, offsets in TEMPLATES.items():
                wanted = {(root + o) % 12 for o in offsets}
                # Reward pitch classes that fit; penalise those that don't.
                # Without the penalty, larger templates always win.
                score = sum(
                    count if pc in wanted else -0.5 * count
                    for pc, count in classes.items()
                )
                score += 0.5 * classes.get(root, 0)  # slight bias to a real root
                score -= 0.1 * len(offsets)          # prefer simpler chords
                # The bass note is the single strongest evidence of the root.
                # Without this, G-B-D under an E melody reads as E minor 7 —
                # the same pitches, but with a root the bass flatly contradicts.
                # Chord symbols exist to tell the left hand where to sit, so
                # getting the root wrong is the one error that matters here.
                if root == bass_pc:
                    score += 3.0
                if score > best_score:
                    best, best_score = (root, quality), score
        chords[bar] = best
    return chords


# --- left hand realisation ----------------------------------------------


def _voice(root_pc: int, quality: str, octave: int, voices: int) -> list[int]:
    """Chord tones as MIDI pitches, low to high."""
    base = 12 * (octave + 1) + root_pc
    offsets = TEMPLATES[quality][:max(1, voices)]
    return [base + o for o in offsets]


def _left_hand_for_bar(
    section: Section, chord: tuple[int, str], start: float, end: float
) -> list[Note]:
    """Realise one bar of left hand according to the section's pattern."""
    root_pc, quality = chord
    pitches = _voice(root_pc, quality, section.lh_octave, section.lh_voices)
    root = pitches[0]
    span = max(end - start, 0.05)
    out: list[Note] = []

    def add(pitch: int, onset: float, duration: float) -> None:
        out.append(Note(pitch=pitch, onset=onset, duration=max(duration, 0.05), staff=2))

    if section.lh_pattern == LHPattern.PEDAL_TONE:
        add(root, start, span)

    elif section.lh_pattern == LHPattern.BLOCK:
        if section.roll_wide_chords and len(pitches) > 1:
            # Stagger by 30ms. This is not cosmetic: rolled notes are not
            # simultaneous, so the hand-span rule stops applying to them.
            # It is the cheapest legal fix for a wide chord.
            for i, p in enumerate(pitches):
                add(p, start + i * 0.03, span - i * 0.03)
        else:
            for p in pitches:
                add(p, start, span)

    elif section.lh_pattern == LHPattern.BROKEN_OCTAVE:
        step = span / 4
        for i in range(4):
            add(root if i % 2 == 0 else root + 12, start + i * step, step)

    elif section.lh_pattern == LHPattern.ARPEGGIO:
        shape = [pitches[0], pitches[min(2, len(pitches) - 1)],
                 pitches[0] + 12, pitches[min(1, len(pitches) - 1)]]
        step = span / 4
        for i, p in enumerate(shape):
            add(p, start + i * step, step)

    elif section.lh_pattern == LHPattern.ALBERTI:
        fifth = pitches[min(2, len(pitches) - 1)]
        third = pitches[min(1, len(pitches) - 1)]
        step = span / 4
        for i, p in enumerate([root, fifth, third, fifth]):
            add(p, start + i * step, step)

    elif section.lh_pattern == LHPattern.WALKING:
        step = span / 4
        for i, p in enumerate(pitches[:4] or [root]):
            add(p, start + i * step, step)

    else:
        raise RenderError(f"unhandled pattern {section.lh_pattern}")

    return out


# --- the public entry point ---------------------------------------------


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
    chords = detect_chords(source, melody)

    bar_span: dict[int, tuple[float, float]] = {}
    for n in source.notes:
        if n.bar is None:
            continue
        start, end = bar_span.get(n.bar, (n.onset, n.offset))
        bar_span[n.bar] = (min(start, n.onset), max(end, n.offset))

    # A bar's span is derived from its notes' durations, and under sustain
    # those run well past the barline. Left unclipped, every left-hand chord
    # overlaps the next one and the accompaniment stacks on itself. Clip each
    # bar to where the following bar begins.
    ordered = sorted(bar_span)
    for i, bar in enumerate(ordered[:-1]):
        start, end = bar_span[bar]
        next_start = bar_span[ordered[i + 1]][0]
        bar_span[bar] = (start, min(end, next_start))

    out: list[Note] = []

    # Right hand: the melody, shifted and optionally folded. The melody is
    # never dropped or thinned — see CLAUDE.md. Folding moves notes by whole
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
                fold_centres[i] = sorted(in_section)[len(in_section) // 2]

    for n in melody:
        section = plan.section_for_bar(n.bar) if n.bar else None
        pitch = n.pitch + (section.melody_shift if section else 0)

        if section is not None and section.melody_fold_window:
            idx = plan.sections.index(section)
            centre = fold_centres.get(idx)
            if centre is not None:
                half = section.melody_fold_window / 2
                # Octaves only. Any other interval would change the note.
                while pitch - centre > half:
                    pitch -= 12
                while centre - pitch > half:
                    pitch += 12

        out.append(
            Note(pitch=pitch, onset=n.onset, duration=n.duration,
                 staff=1, bar=n.bar)
        )

    # Left hand: one realisation per bar, from the section's pattern.
    for bar, (start, end) in sorted(bar_span.items()):
        section = plan.section_for_bar(bar)
        if section is None or section.lh_voices == 0:
            continue  # bar not covered, or deliberately melody-only
        chord = chords.get(bar)
        if chord is None:
            continue
        for note in _left_hand_for_bar(section, chord, start, end):
            out.append(
                Note(pitch=note.pitch, onset=note.onset, duration=note.duration,
                     staff=2, bar=bar)
            )

    return Score(notes=out, tempo_bpm=source.tempo_bpm, title=plan.title)


def last_bar(source: Score) -> int:
    return max((n.bar for n in source.notes if n.bar is not None), default=1)
