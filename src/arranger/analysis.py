"""Reading the source: where is the tune, what are the chords, what is the bass.

Everything the renderer, planner and fidelity scorer need to know about a
source score is worked out here, once, deterministically. None of it is
notation and none of it is model judgement; it is measurement.

Dependency-free.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, replace

from .ir import Note, Score

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

_CLUSTER_SECONDS = 0.02   # notes this close together are one attack
_CANDIDATES = 3           # top starting notes considered at each attack
_ELIGIBLE_MARGIN = 0.25   # how far below the most melodic part a part may score


# --- melody ---------------------------------------------------------------


def _as_melody_note(n: Note, *, duration: float | None = None) -> Note:
    """A melody note for the right hand, keeping its link to the source note."""
    return replace(
        n,
        duration=n.duration if duration is None else duration,
        staff=1,
        voice=1,
        role="melody",
        source_id=n.source_id or n.id,
        beats=n.beats if duration is None else None,
    )


def _make_monophonic(melody: list[Note]) -> list[Note]:
    """Merge overlapping re-strikes of one pitch, then clip each note at the next.

    A note lasting 400ms that re-triggers 5ms later is an overlapping
    duplicate, not a repeat. And a melodic line is monophonic by definition:
    source durations are sustained, often by pedal, so consecutive melody notes
    overlap, and an overlapping "line" reads to the verifier as a right hand
    holding six notes across two octaves.
    """
    merged: list[Note] = []
    for n in melody:
        if merged:
            prev = merged[-1]
            if prev.pitch == n.pitch and n.onset < prev.offset:
                merged[-1] = replace(
                    prev, duration=max(prev.duration, n.offset - prev.onset), beats=None
                )
                continue
        merged.append(n)
    for i in range(len(merged) - 1):
        gap = merged[i + 1].onset - merged[i].onset
        if merged[i].duration > gap:
            merged[i] = replace(merged[i], duration=max(gap, 0.02), beats=None)
    return merged


def extract_melody_skyline(source: Score, floor_drop: int = 9) -> list[Note]:
    """The original extractor: at each onset, the highest sounding note.

    Kept as the reference the path-based extractor is compared against, and
    because it is the definition the first six build-log entries were measured
    with. Notes more than `floor_drop` semitones below the median top note are
    treated as accompaniment showing through a rest.
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
    melody = [
        replace(_as_melody_note(top), onset=t, duration=top.offset - t if top.onset < t else top.duration)
        for t, top in tops if top.pitch >= floor
    ]
    return _make_monophonic(melody)


def _track_priors(source: Score) -> dict[int, float]:
    """How melody-like each imported part is, 0..1. Empty when there is one part."""
    by_track: dict[int, list[Note]] = {}
    for n in source.notes:
        if n.track is not None:
            by_track.setdefault(n.track, []).append(n)
    if len(by_track) < 2:
        return {}
    names = {t.index: t.name.lower() for t in source.tracks}
    lead_words = ("melody", "vocal", "voice", "lead", "solo", "violin", "flute", "right", "rh", "sopran")
    back_words = ("bass", "drum", "left", "lh", "pad", "accomp", "rhythm", "chord", "strings")
    means = {t: sum(n.pitch for n in ns) / len(ns) for t, ns in by_track.items()}
    low, high = min(means.values()), max(means.values())
    priors: dict[int, float] = {}
    for track, notes in by_track.items():
        register = (means[track] - low) / (high - low) if high > low else 0.5
        attacks = Counter(round(n.onset, 2) for n in notes)
        monophony = sum(1 for c in attacks.values() if c == 1) / len(attacks)
        ordered = sorted(notes, key=lambda n: n.onset)
        steps = [abs(b.pitch - a.pitch) for a, b in zip(ordered, ordered[1:], strict=False)]
        stepwise = sum(1 for s in steps if 0 < s <= 4) / len(steps) if steps else 0.0
        score = 0.45 * register + 0.30 * monophony + 0.25 * stepwise
        name = names.get(track, "")
        if any(w in name for w in lead_words):
            score += 0.35
        if any(w in name for w in back_words):
            score -= 0.35
        priors[track] = min(1.0, max(0.0, score))
    return priors


def extract_melody(source: Score, floor_drop: int = 9) -> list[Note]:
    """The tune, as a monophonic line for the right hand.

    If the user has marked notes `role="melody"`, that is the answer: they can
    hear which line is the tune and no heuristic should overrule them.

    Otherwise the line is found as the cheapest path through the piece. At
    each attack the candidates are the highest few notes that *start* there,
    plus "no melody note here". A path pays for choosing a lower note, for
    dropping below the register the top line lives in, for large leaps, and
    for picking a note from a part that does not look like a melody part; it
    pays a little to stay silent while the last melody note still sounds and
    more to stay silent otherwise.

    That cost structure is what fixes the two failures of taking the top note
    at every onset. When the melody rests, the accompaniment underneath is far
    away and below the register floor, so silence is cheaper than diving two
    octaves. And a note held over moving inner voices stays one note, because
    only notes that start at an attack are candidates.
    """
    flagged = [n for n in source.notes if n.role == "melody"]
    if flagged:
        by_attack: dict[float, Note] = {}
        for n in sorted(flagged, key=lambda n: (n.onset, -n.pitch)):
            by_attack.setdefault(round(n.onset, 3), n)
        return _make_monophonic([_as_melody_note(n) for n in by_attack.values()])

    notes = source.notes
    if not notes:
        return []

    # With several imported parts, some are plainly not the tune: a bass, a
    # pad, a drum-like ostinato. They must not set the melody's register or
    # count as "something held above", or a high pad hides the singer under it.
    # Parts within reach of the most melodic one stay eligible, so a tune that
    # passes from violin to flute is still followed.
    priors = _track_priors(source)
    if priors:
        best = max(priors.values())
        eligible = {t for t, v in priors.items() if v >= best - _ELIGIBLE_MARGIN}
        notes = [n for n in notes if n.track is None or n.track in eligible] or notes
    in_play = {id(n) for n in notes}

    # Group attacks, keep the highest few starters in each.
    clusters: list[list[Note]] = []
    for n in notes:
        if clusters and n.onset - clusters[-1][0].onset <= _CLUSTER_SECONDS:
            clusters[-1].append(n)
        else:
            clusters.append([n])
    # The register floor is measured from the highest note *sounding* at each
    # attack, not the highest one starting there. While the tune holds a long
    # note the left hand keeps attacking underneath; counting those attacks
    # would drag the floor down into the accompaniment it exists to exclude.
    def held_at(t: float) -> list[Note]:
        return [n for n in source.sounding_at(t) if id(n) in in_play]

    tops = sorted(
        max(max((n.pitch for n in held_at(c[0].onset)), default=0), max(n.pitch for n in c))
        for c in clusters
    )
    floor = tops[len(tops) // 2] - floor_drop

    # A path is (cost, tail) where tail is a linked list (note, parent). Copying
    # the whole line at every attack would be quadratic in the piece length.
    Path = tuple[float, tuple | None]
    best_none: Path = (0.0, None)
    best_pick: list[Path] = []

    for cluster in clusters:
        t = cluster[0].onset
        by_pitch: dict[int, Note] = {}
        for n in cluster:
            by_pitch.setdefault(n.pitch, n)    # a doubled pitch is one candidate
        candidates = sorted(by_pitch.values(), key=lambda n: -n.pitch)[:_CANDIDATES]
        # Any note already sounding above a candidate means it is an inner voice.
        held_above = max(
            (n.pitch for n in held_at(t) if n.onset < t - _CLUSTER_SECONDS), default=None
        )
        previous = [best_none, *best_pick]

        def transition(path: Path, n: Note, t: float = t) -> float:
            cost, tail = path
            if tail is None:
                return cost
            last: Note = tail[0]
            leap = abs(n.pitch - last.pitch)
            leap_cost = 0.25 * min(leap, 12) + 0.6 * max(0, leap - 12)
            if t - last.offset > 2.0:
                leap_cost *= 0.5   # a new phrase may start anywhere
            return cost + leap_cost

        new_pick: list[Path] = []
        for rank, n in enumerate(candidates):
            emit = 2.0 * rank
            if n.pitch < floor:
                emit += 4.0
            if held_above is not None and held_above > n.pitch + 2:
                emit += 1.5
            if priors:
                emit += 1.0 - priors.get(n.track if n.track is not None else -1, 0.5)
            cost, tail = min(((transition(p, n), p[1]) for p in previous), key=lambda x: x[0])
            new_pick.append((cost + emit, (n, tail)))

        # What silence costs depends on what is being passed over. A note that
        # sits on top of everything sounding and in the melody's register is
        # almost certainly the tune, so skipping it is expensive. A note under
        # a held note, or down in the accompaniment, is cheap to skip.
        top = candidates[0]
        exposed = held_above is None or top.pitch >= held_above
        plausible = exposed and top.pitch >= floor

        def silence(path: Path, t: float = t, plausible: bool = plausible) -> float:
            cost, tail = path
            still_sounding = tail is not None and tail[0].offset > t + _CLUSTER_SECONDS
            if plausible:
                return cost + (1.5 if still_sounding else 3.5)
            return cost + (0.3 if still_sounding else 1.0)

        best_none = min(((silence(p), p[1]) for p in previous), key=lambda x: x[0])
        best_pick = new_pick

    _, tail = min([best_none, *best_pick], key=lambda x: x[0])
    line: list[Note] = []
    while tail is not None:
        line.append(tail[0])
        tail = tail[1]
    line.reverse()
    return _make_monophonic([_as_melody_note(n) for n in line])


# --- harmony --------------------------------------------------------------


def _best_chord(classes: Counter, bass_pc: int | None) -> tuple[tuple[int, str], float]:
    """Best (root, quality) for a weighted pitch-class histogram, and how well it fits."""
    total = sum(classes.values()) or 1.0
    best, best_score = (0, "maj"), -1e9
    for root in range(12):
        for quality, offsets in TEMPLATES.items():
            wanted = {(root + o) % 12 for o in offsets}
            # Reward pitch classes that fit; penalise those that do not.
            # Without the penalty, larger templates always win.
            score = sum(
                count if pc in wanted else -0.5 * count for pc, count in classes.items()
            )
            score += 0.5 * classes.get(root, 0)  # slight bias to a real root
            score -= 0.1 * len(offsets)          # prefer simpler chords
            # The bass note is the single strongest evidence of the root.
            # Without this, G-B-D under an E melody reads as E minor 7 - the
            # same pitches, but with a root the bass flatly contradicts.
            #
            # The bonus is scaled by how much of the template is really there.
            # C-E-G over an E bass is C major in first inversion, not E minor:
            # E minor needs a B that nobody is playing. At full strength the
            # bonus outvoted that missing note and the left hand was handed
            # the wrong chord for every inverted bar.
            if bass_pc is not None and root == bass_pc:
                present = sum(1 for pc in wanted if classes.get(pc, 0) > 0)
                score += 3.0 * present / len(wanted)
            if score > best_score:
                best, best_score = (root, quality), score
    wanted = {(best[0] + o) % 12 for o in TEMPLATES[best[1]]}
    fit = sum(c for pc, c in classes.items() if pc in wanted) / total
    return best, fit


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

    return {
        bar: _best_chord(classes, bass_of_bar[bar] % 12)[0] for bar, classes in by_bar.items()
    }


@dataclass(frozen=True)
class ChordSegment:
    """A stretch of one bar governed by one chord."""

    bar: int
    start: float          # seconds
    end: float
    root: int             # pitch class
    quality: str
    bass_pc: int          # pitch class the source actually has at the bottom
    start_beat: float | None = None
    end_beat: float | None = None

    @property
    def chord(self) -> tuple[int, str]:
        return self.root, self.quality


def bar_spans(source: Score) -> dict[int, tuple[float, float]]:
    """(start, end) seconds for every bar that has music.

    With a timeline the barlines are exact. Without one they are inferred from
    the notes: a bar starts at its first onset and ends where the next bar
    starts. Durations run past the barline under sustain, so without that clip
    every left-hand chord would overlap the next one.
    """
    bars = sorted({n.bar for n in source.notes if n.bar is not None})
    if source.timeline is not None:
        return {bar: source.timeline.bar_seconds(bar) for bar in bars}
    spans: dict[int, tuple[float, float]] = {}
    for n in source.notes:
        if n.bar is None:
            continue
        start, end = spans.get(n.bar, (n.onset, n.offset))
        spans[n.bar] = (min(start, n.onset), max(end, n.offset))
    for i, bar in enumerate(bars[:-1]):
        start, end = spans[bar]
        spans[bar] = (start, min(end, spans[bars[i + 1]][0]))
    return spans


def _split_points(source: Score, bar: int) -> list[float]:
    """Beats, relative to the bar start, where harmony most often changes."""
    timeline = source.timeline
    if timeline is None:
        return []
    num, den = timeline.meter_at_bar(bar)
    length = timeline.bar_beats(bar)
    unit = 4.0 / den
    if den >= 8 and num % 3 == 0 and num > 3:
        step = unit * 3 * (2 if num >= 12 else 1)
    elif num % 2 == 0 and num >= 4:
        step = unit * num / 2
    else:
        return []
    points, x = [], step
    while x < length - 1e-9:
        points.append(x)
        x += step
    return points


def detect_chord_segments(source: Score, melody: list[Note]) -> dict[int, list[ChordSegment]]:
    """Chords per bar, split where the harmony really changes inside the bar.

    A bar is split only when both halves are confidently different chords:
    each has at least two pitch classes and its best chord explains most of
    what sounds there. A passing tone is not a chord change, and a left hand
    that lurches to a new chord on weak evidence is worse than one that holds.
    """
    spans = bar_spans(source)
    whole = detect_chords(source, melody)
    melody_keys = {(round(n.onset, 3), n.pitch) for n in melody}
    timeline = source.timeline
    out: dict[int, list[ChordSegment]] = {}

    onsets = [n.onset for n in source.notes]   # source.notes is kept sorted by onset

    def histogram(start: float, end: float) -> tuple[Counter, int | None]:
        """Weighted pitch classes sounding in [start, end), and the early bass."""
        classes: Counter = Counter()
        lowest: Note | None = None
        # Notes already sounding at `start`, then notes that begin inside the
        # window. Scanning every note for every segment was quadratic.
        first = bisect_left(onsets, start)
        last = bisect_left(onsets, end)
        held = [n for n in source.sounding_at(start) if n.onset < start]
        for n in [*held, *source.notes[first:last]]:
            overlap = min(n.offset, end) - max(n.onset, start)
            if overlap <= 0:
                continue
            weight = 1.0 if (round(n.onset, 3), n.pitch) in melody_keys else 2.0
            classes[n.pitch % 12] += overlap * weight
            early = max(n.onset, start) <= start + 0.34 * (end - start)
            if early and (lowest is None or n.pitch < lowest.pitch):
                lowest = n
        return classes, (lowest.pitch % 12 if lowest is not None else None)

    for bar, (start, end) in spans.items():
        if bar not in whole:
            continue
        root, quality = whole[bar]
        start_beat = timeline.bar_start(bar) if timeline else None
        bar_beats = timeline.bar_beats(bar) if timeline else None
        _, bar_bass = histogram(start, end)
        single = [
            ChordSegment(
                bar, start, end, root, quality, bar_bass if bar_bass is not None else root,
                start_beat, (start_beat + bar_beats) if timeline else None,
            )
        ]
        points = _split_points(source, bar)
        if not points or timeline is None:
            out[bar] = single
            continue

        edges = [0.0, *points, bar_beats]
        pieces: list[ChordSegment] = []
        confident = True
        for a, b in zip(edges, edges[1:], strict=False):
            s, e = timeline.seconds_at(start_beat + a), timeline.seconds_at(start_beat + b)
            classes, bass = histogram(s, e)
            if len(classes) < 2:
                confident = False
                break
            scale = 10.0 / (sum(classes.values()) or 1.0)
            classes = Counter({pc: w * scale for pc, w in classes.items()})
            chord, fit = _best_chord(classes, bass)
            if fit < 0.7:
                confident = False
                break
            pieces.append(
                ChordSegment(bar, s, e, chord[0], chord[1], bass if bass is not None else chord[0],
                             start_beat + a, start_beat + b)
            )
        if not confident or len({p.chord for p in pieces}) < 2:
            out[bar] = single
            continue
        # Merge neighbours that agree, so 12/8 does not become four identical chords.
        merged = [pieces[0]]
        for piece in pieces[1:]:
            if piece.chord == merged[-1].chord:
                merged[-1] = replace(merged[-1], end=piece.end, end_beat=piece.end_beat)
            else:
                merged.append(piece)
        out[bar] = merged
    return out


def bass_line(source: Score) -> dict[int, int]:
    """Lowest pitch sounding at the start of each bar: the bass the ear follows."""
    spans = bar_spans(source)
    by_bar: dict[int, list[Note]] = {}
    for n in source.notes:
        if n.bar is not None:
            by_bar.setdefault(n.bar, []).append(n)
    out: dict[int, int] = {}
    for bar, (start, end) in spans.items():
        window = start + 0.34 * (end - start)
        pool = [n.pitch for n in by_bar.get(bar, []) if n.onset <= window]
        pool += [n.pitch for n in source.sounding_at(start)]
        if pool:
            out[bar] = min(pool)
    return out


def last_bar(source: Score) -> int:
    return max((n.bar for n in source.notes if n.bar is not None), default=1)
