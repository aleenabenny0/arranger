"""Did the arrangement keep the music?

The verifier answers "can this be played". On its own that is a metric with a
degenerate optimum: an empty score is perfectly playable. The first successful
agent run found this in four attempts, scoring zero violations by removing the
left hand from 87 of 239 bars.

Nothing was wrong with the model's reasoning. It optimised exactly the thing
it was given. The mistake was giving it one number when the goal has two
parts: playable *and* still the song.

Every component here was added because some cheap output scored well without
it. The tests in `tests/test_fidelity.py` are that list of cheap outputs.

Dependency-free, like the verifier, and for the same reason: this is scoring
machinery, and scoring machinery that can break is worse than none.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace

from .analysis import TEMPLATES, bass_line, detect_chords, extract_melody
from .ir import Note, Score

ONSET_TOLERANCE = 0.05   # seconds: "the same moment" for a listener

WEIGHTS = {
    "melodic_recall": 0.35,
    "rhythm": 0.10,
    "contour": 0.05,
    "harmonic_coverage": 0.20,
    "bass": 0.10,
    "accompaniment": 0.20,
}


@dataclass
class Fidelity:
    """How much of the source survived. Each component is between 0 and 1.

    The first three are the originals and keep their positions. The rest
    default to 1.0 so a `Fidelity(a, b, c)` built by older code still means
    what it meant.
    """

    melodic_recall: float       # source melody notes still in the right hand, right pitch class, right moment
    harmonic_coverage: float    # bars whose chord the accompaniment still states
    accompaniment: float        # bars with a left hand, scaled by how much it does
    rhythm: float = 1.0         # right-hand attacks match the melody's: nothing missing, nothing added
    contour: float = 1.0        # the tune still goes up where it went up
    bass: float = 1.0           # bars whose bottom note is still the source's bass

    def score(self) -> float:
        """One number, weighted by how much each loss hurts.

        The melody carries the most weight because losing it means the piece
        is no longer recognisable. Accompaniment and harmony together outweigh
        it, deliberately: under the first weighting (0.55/0.30/0.15) deleting
        every left-hand note landed precisely on the acceptance floor, because
        the melody alone usually implies the chord. A floor is only a floor if
        the degenerate case is checked against it, not reasoned about.
        """
        return sum(WEIGHTS[name] * getattr(self, name) for name in WEIGHTS)

    def summary(self) -> str:
        return (
            f"melody {self.melodic_recall:.0%}, harmony {self.harmonic_coverage:.0%}, "
            f"accompaniment {self.accompaniment:.0%}, rhythm {self.rhythm:.0%}, "
            f"contour {self.contour:.0%}, bass {self.bass:.0%} (score {self.score():.2f})"
        )

    def weakest(self) -> tuple[str, float]:
        """The component costing the most, for feedback that says what to fix."""
        name = max(WEIGHTS, key=lambda k: WEIGHTS[k] * (1.0 - getattr(self, k)))
        return name, getattr(self, name)


def _right_hand(arranged: Score) -> list[Note]:
    """The line a listener hears as the tune in the arrangement.

    If the arrangement commits to staves, it is the upper staff, and *only*
    the upper staff: matching melody notes against the whole texture let a
    left hand that happened to double the tune cover for a right hand that
    dropped it. Without staves, fall back to extracting the top line.
    """
    upper = [n for n in arranged.notes if n.staff == 1]
    if upper or any(n.staff is not None for n in arranged.notes):
        return sorted(upper, key=lambda n: n.onset)
    return extract_melody(arranged)


def _nearest(onsets: list[float], t: float) -> int | None:
    """Index of the onset closest to t, if within tolerance."""
    i = bisect_left(onsets, t)
    best: int | None = None
    for j in (i - 1, i):
        if 0 <= j < len(onsets) and abs(onsets[j] - t) <= ONSET_TOLERANCE:
            if best is None or abs(onsets[j] - t) < abs(onsets[best] - t):
                best = j
    return best


def measure(source: Score, arranged: Score) -> Fidelity:
    """Compare an arrangement against the source it came from."""
    source_melody = extract_melody(source)
    if not source_melody:
        return Fidelity(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    # An arrangement may be slower than its source (plan.tempo_scale). Compare
    # in the source's time, or a faithful slow arrangement scores as if every
    # note were late.
    stretch = 1.0
    if source.tempo_bpm > 0 and arranged.tempo_bpm > 0:
        stretch = arranged.tempo_bpm / source.tempo_bpm
    if abs(stretch - 1.0) > 1e-9:
        arranged = Score(
            notes=[replace(n, onset=n.onset * stretch, duration=n.duration * stretch) for n in arranged.notes],
            tempo_bpm=source.tempo_bpm, timeline=source.timeline,
        )

    right = _right_hand(arranged)
    right_onsets = [n.onset for n in right]

    # --- melody and rhythm -------------------------------------------------
    # Pitch class, not absolute pitch: octave folding is a legitimate and
    # encouraged transformation, and comparing absolute pitch would penalise
    # the very fix that makes wide melodies playable.
    matched: list[tuple[Note, Note]] = []
    used: set[int] = set()
    for want in source_melody:
        lo = bisect_left(right_onsets, want.onset - ONSET_TOLERANCE)
        for j in range(lo, len(right)):
            if right_onsets[j] > want.onset + ONSET_TOLERANCE:
                break
            if j not in used and right[j].pitch % 12 == want.pitch % 12:
                used.add(j)
                matched.append((want, right[j]))
                break
    melodic_recall = len(matched) / len(source_melody)

    source_onsets = sorted({round(n.onset, 3) for n in source_melody})
    arranged_onsets = sorted({round(t, 3) for t in right_onsets})
    kept = sum(1 for t in source_onsets if _nearest(arranged_onsets, t) is not None)
    wanted = sum(1 for t in arranged_onsets if _nearest(source_onsets, t) is not None)
    recall = kept / len(source_onsets)
    precision = wanted / len(arranged_onsets) if arranged_onsets else 0.0
    rhythm = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    # --- contour -------------------------------------------------------------
    # Folding keeps every pitch class and can still turn a rising line into a
    # falling one. Count how often consecutive notes still move the same way.
    agree = total = 0
    for (a_src, a_arr), (b_src, b_arr) in zip(matched, matched[1:], strict=False):
        want = (b_src.pitch > a_src.pitch) - (b_src.pitch < a_src.pitch)
        if want == 0:
            continue
        total += 1
        got = (b_arr.pitch > a_arr.pitch) - (b_arr.pitch < a_arr.pitch)
        agree += want == got
    contour = agree / total if total else (1.0 if matched else 0.0)

    # --- harmony, bass, accompaniment: bar by bar ------------------------------
    chords = detect_chords(source, source_melody)
    source_bass = bass_line(source)
    melody_ids = {(round(n.onset, 3), n.pitch) for n in source_melody}

    left_by_bar: dict[int, list[Note]] = {}
    all_by_bar: dict[int, set[int]] = {}
    for n in arranged.notes:
        if n.bar is None:
            continue
        all_by_bar.setdefault(n.bar, set()).add(n.pitch % 12)
        if n.staff == 2:
            left_by_bar.setdefault(n.bar, []).append(n)

    source_attacks: dict[int, set[float]] = {}
    for n in source.notes:
        if n.bar is not None and (round(n.onset, 3), n.pitch) not in melody_ids:
            source_attacks.setdefault(n.bar, set()).add(round(n.onset, 3))
    activity = {bar: len(onsets) for bar, onsets in source_attacks.items()}

    harmony_total = bass_total = accompaniment_total = 0.0
    accompanied_bars = [bar for bar in chords if activity.get(bar, 0) > 0] or list(chords)
    for bar, (root, quality) in chords.items():
        left = left_by_bar.get(bar, [])
        left_pcs = {n.pitch % 12 for n in left}
        third = (root + TEMPLATES[quality][1]) % 12
        # The root must come from the accompaniment: the melody touching the
        # root in passing does not ground the harmony. The third may come from
        # anywhere, since a good reduction often leaves it to the right hand.
        credit = 0.0
        if root in left_pcs:
            credit += 0.6
        if third in all_by_bar.get(bar, set()):
            credit += 0.4
        harmony_total += credit

    for bar in accompanied_bars:
        left = left_by_bar.get(bar, [])
        if not left:
            continue
        # Presence is most of it; doing as much as the source did is the rest.
        # The target is capped so a busy source does not make every simple,
        # playable left hand look like a failure.
        attacks = len({round(n.onset, 3) for n in left})
        target = max(1, min(activity.get(bar, 1), 3))
        accompaniment_total += 0.7 + 0.3 * min(1.0, attacks / target)

        want_bass = source_bass.get(bar)
        if want_bass is not None:
            first = min(left, key=lambda n: (round(n.onset, 3), n.pitch))
            lowest = min(n.pitch for n in left if n.onset <= first.onset + ONSET_TOLERANCE)
            if lowest % 12 == want_bass % 12:
                bass_total += 1.0
            elif lowest % 12 == chords[bar][0]:
                bass_total += 0.7   # the root under an inverted chord: grounded, not faithful

    harmonic_coverage = harmony_total / len(chords) if chords else 0.0
    accompaniment = accompaniment_total / len(accompanied_bars) if accompanied_bars else 0.0
    bass = bass_total / len(accompanied_bars) if accompanied_bars else 0.0

    return Fidelity(
        melodic_recall=melodic_recall,
        harmonic_coverage=harmonic_coverage,
        accompaniment=accompaniment,
        rhythm=rhythm,
        contour=contour,
        bass=bass,
    )
