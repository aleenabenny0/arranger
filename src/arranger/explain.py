"""What was actually changed, and how hard the result is.

A plan's `reductions` list is a claim, written by whoever wrote the plan. This
module does not read it. Everything here is measured from the source and the
rendered arrangement, so an explanation can never describe a change that did
not happen or miss one that did.

Difficulty lives here too, and is deliberately separate from playability
findings: "every note is within reach" and "this is a grade 6 piece" are
different statements, and conflating them is how a tool ends up guaranteeing
things it cannot know.

Dependency-free.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .analysis import TEMPLATES, bass_line, detect_chords, extract_melody
from .ir import Score
from .plan import ArrangementPlan, LHPattern

PATTERN_NAMES = {
    LHPattern.BLOCK: "held chords",
    LHPattern.PEDAL_TONE: "a single held bass note",
    LHPattern.BROKEN_OCTAVE: "broken octaves",
    LHPattern.ARPEGGIO: "arpeggios",
    LHPattern.ALBERTI: "an Alberti figure",
    LHPattern.WALKING: "a walking bass line",
    LHPattern.STRIDE: "a bass note followed by chords",
    LHPattern.BROKEN_TENTH: "broken tenths",
}


@dataclass
class TransformationReport:
    sentences: list[str] = field(default_factory=list)
    melody_notes: int = 0
    melody_kept: int = 0
    melody_moved_by_octave: int = 0
    source_notes: int = 0
    arranged_notes: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    bass_bars_kept: int = 0
    bass_bars_replaced_by_root: int = 0
    bass_bars_lost: int = 0
    colour_bars_simplified: int = 0
    tempo_scale: float = 1.0
    pedal_bars: int = 0
    rolled_chords: int = 0
    patterns: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(vars(self))


def explain(plan: ArrangementPlan, source: Score, arranged: Score, changes: list[str] | None = None) -> TransformationReport:
    """Describe the arrangement in terms of what happened to the source's notes."""
    report = TransformationReport(tempo_scale=float(plan.tempo_scale))
    report.source_notes, report.arranged_notes = len(source.notes), len(arranged.notes)
    sentences: list[str] = list(changes or [])

    melody = extract_melody(source)
    right = {n.source_id: n for n in arranged.notes if n.staff == 1 and n.source_id}
    report.melody_notes = len(melody)
    moved_bars: set[int] = set()
    for n in melody:
        out = right.get(n.source_id)
        if out is None:
            continue
        report.melody_kept += 1
        if out.pitch != n.pitch and (out.pitch - n.pitch) % 12 == 0:
            report.melody_moved_by_octave += 1
            if n.bar is not None:
                moved_bars.add(n.bar)

    if report.melody_notes:
        if report.melody_kept == report.melody_notes:
            sentences.append(f"All {report.melody_notes} melody notes are kept in the right hand.")
        else:
            sentences.append(
                f"{report.melody_kept} of {report.melody_notes} melody notes are kept in the right hand."
            )
    if report.melody_moved_by_octave:
        sentences.append(
            f"{report.melody_moved_by_octave} melody notes in {_bars(moved_bars)} were moved by an "
            "octave to keep the right hand within reach. Their letter names are unchanged."
        )

    # --- what the accompaniment lost, by role -----------------------------
    melody_ids = {n.source_id for n in melody}
    chords = detect_chords(source, melody)
    left_pcs: dict[int, set[int]] = {}
    all_pcs: dict[int, set[int]] = {}
    for n in arranged.notes:
        if n.bar is None:
            continue
        all_pcs.setdefault(n.bar, set()).add(n.pitch % 12)
        if n.staff == 2:
            left_pcs.setdefault(n.bar, set()).add(n.pitch % 12)

    seen_pc: dict[int, Counter] = {}
    for n in source.notes:
        if n.bar is not None:
            seen_pc.setdefault(n.bar, Counter())[n.pitch % 12] += 1
    dropped: Counter = Counter()
    for n in source.notes:
        if n.id in melody_ids or n.bar is None:
            continue
        pc = n.pitch % 12
        if pc in all_pcs.get(n.bar, set()):
            if seen_pc[n.bar][pc] > 1:
                dropped["repeated or doubled notes"] += 1   # the pitch survives; this copy does not
                seen_pc[n.bar][pc] -= 1
            continue
        chord = chords.get(n.bar)
        if chord is None:
            dropped["other notes"] += 1
            continue
        root, quality = chord
        tones = [(root + o) % 12 for o in TEMPLATES[quality]]
        if pc == tones[0]:
            dropped["chord roots"] += 1
        elif pc in tones[1:3]:
            dropped["inner chord tones"] += 1
        elif pc in tones[3:]:
            dropped["sevenths"] += 1
        else:
            dropped["passing and colour tones"] += 1
    report.dropped = dict(dropped)
    if dropped:
        order = ["repeated or doubled notes", "inner chord tones", "passing and colour tones", "sevenths", "chord roots", "other notes"]
        parts = [f"{dropped[k]} {k}" for k in order if dropped.get(k)]
        sentences.append("Left out of the accompaniment: " + ", ".join(parts) + ".")

    # --- bass -----------------------------------------------------------------
    source_bass = bass_line(source)
    arranged_left = [n for n in arranged.notes if n.staff == 2 and n.bar is not None]
    lowest: dict[int, int] = {}
    for n in arranged_left:
        lowest[n.bar] = min(lowest.get(n.bar, 128), n.pitch)
    for bar, want in source_bass.items():
        if bar not in chords:
            continue
        got = lowest.get(bar)
        if got is None:
            report.bass_bars_lost += 1
        elif got % 12 == want % 12:
            report.bass_bars_kept += 1
        elif got % 12 == chords[bar][0]:
            report.bass_bars_replaced_by_root += 1
        else:
            report.bass_bars_lost += 1
    total = report.bass_bars_kept + report.bass_bars_replaced_by_root + report.bass_bars_lost
    if total:
        text = f"The original bass note is kept in {report.bass_bars_kept} of {total} bars"
        if report.bass_bars_replaced_by_root:
            text += f"; in {report.bass_bars_replaced_by_root} it is replaced by the chord's root"
        if report.bass_bars_lost:
            text += f"; {report.bass_bars_lost} bars have a different or no bass note"
        sentences.append(text + ".")

    for bar, (root, quality) in chords.items():
        template = TEMPLATES[quality]
        if len(template) > 3 and (root + template[3]) % 12 not in all_pcs.get(bar, set()):
            report.colour_bars_simplified += 1
    if report.colour_bars_simplified:
        sentences.append(
            f"Seventh chords are played as plain triads in {report.colour_bars_simplified} bars."
        )

    # --- how the left hand is played ----------------------------------------------
    bars_by_pattern: Counter = Counter()
    for section in plan.sections:
        if section.lh_voices > 0:
            bars_by_pattern[PATTERN_NAMES[LHPattern(section.lh_pattern)]] += section.end_bar - section.start_bar + 1
    report.patterns = dict(bars_by_pattern)
    if bars_by_pattern:
        parts = [f"{name} ({count} bars)" for name, count in bars_by_pattern.most_common()]
        sentences.append("The left hand plays " + ", ".join(parts) + ".")
    silent = [s for s in plan.sections if s.lh_voices == 0]
    if silent:
        sentences.append(f"{len(silent)} section(s) are melody only, with no left hand.")

    report.rolled_chords = len({(n.bar, round(n.onset, 1)) for n in arranged.notes if n.rolled})
    if report.rolled_chords:
        sentences.append(f"{report.rolled_chords} wide chords are rolled rather than struck together.")
    report.pedal_bars = len(arranged.pedals)
    if report.pedal_bars:
        sentences.append(f"The sustain pedal is marked in {report.pedal_bars} bars so the hands can move.")
    if plan.tempo_scale < 1.0:
        sentences.append(
            f"The tempo is {plan.tempo_scale:.0%} of the original "
            f"({arranged.tempo_bpm:.0f} instead of {source.tempo_bpm:.0f} beats per minute), because "
            "some passages move faster than this player's hands at full speed."
        )
    report.sentences = sentences
    return report


def _bars(bars: set[int]) -> str:
    ordered = sorted(bars)
    if len(ordered) == 1:
        return f"bar {ordered[0]}"
    if len(ordered) <= 4:
        return "bars " + ", ".join(map(str, ordered))
    return f"{len(ordered)} bars ({ordered[0]} to {ordered[-1]})"


# --- difficulty ---------------------------------------------------------------------


@dataclass
class Difficulty:
    level: float                      # 1 (first lessons) to 10 (advanced repertoire)
    label: str
    factors: dict[str, float] = field(default_factory=dict)
    note: str = (
        "An estimate from note density, reach, leaps and how independently the hands move. "
        "It is not a graded-exam level."
    )

    def to_dict(self) -> dict:
        return dict(vars(self))


_LABELS = [(2.5, "beginner"), (4.5, "late beginner"), (6.5, "intermediate"), (8.5, "advanced"), (11, "very advanced")]


def estimate_difficulty(score: Score) -> Difficulty:
    """How demanding a two-hand score is, independent of any particular player."""
    notes = score.notes
    duration = score.duration()
    if not notes or duration <= 0:
        return Difficulty(1.0, "beginner", {})

    by_hand: dict[int, list] = {1: [], 2: []}
    for n in notes:
        by_hand[2 if n.staff == 2 else 1].append(n)

    attacks = {hand: sorted({round(n.onset, 3) for n in ns}) for hand, ns in by_hand.items()}
    busiest = 0.0
    for times in attacks.values():
        # Peak attacks per second over any four-second stretch: a piece is as
        # hard as its hardest passage, not its average one.
        j = 0
        for i, t in enumerate(times):
            while times[j] < t - 4.0:
                j += 1
            busiest = max(busiest, (i - j + 1) / 4.0)

    spans, chord_sizes, leaps = [], [], []
    for ns in by_hand.values():
        groups: dict[float, list[int]] = {}
        for n in ns:
            groups.setdefault(round(n.onset, 3), []).append(n.pitch)
        ordered = sorted(groups.items())
        for _, pitches in ordered:
            spans.append(max(pitches) - min(pitches))
            chord_sizes.append(len(pitches))
        for (t0, p0), (t1, p1) in zip(ordered, ordered[1:], strict=False):
            gap = max(t1 - t0, 0.05)
            leaps.append(abs(sum(p1) / len(p1) - sum(p0) / len(p0)) / gap)

    def percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    both = set(attacks[1]) & set(attacks[2])
    either = set(attacks[1]) | set(attacks[2])
    independence = 0.0
    if attacks[1] and attacks[2] and either:
        independence = 1.0 - len(both) / len(either)

    factors = {
        "speed": min(1.0, busiest / 8.0),                         # 8 attacks a second in one hand is virtuosic
        "reach": min(1.0, percentile(spans, 0.9) / 14.0),
        "chords": min(1.0, max(0.0, percentile(chord_sizes, 0.9) - 1) / 4.0),
        "leaps": min(1.0, percentile(leaps, 0.9) / 60.0),
        "independence": independence if len(attacks[2]) > 4 else 0.0,
    }
    weights = {"speed": 0.34, "reach": 0.16, "chords": 0.14, "leaps": 0.2, "independence": 0.16}
    level = round(1.0 + 9.0 * sum(weights[k] * factors[k] for k in weights), 1)
    label = next(name for ceiling, name in _LABELS if level < ceiling)
    return Difficulty(level, label, {k: round(v, 3) for k, v in factors.items()})
