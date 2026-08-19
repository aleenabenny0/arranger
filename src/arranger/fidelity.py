"""Did the arrangement keep the music?

The verifier answers "can this be played". On its own that is a metric with a
degenerate optimum: an empty score is perfectly playable. The first successful
agent run found this in four attempts, scoring zero violations by removing the
left hand from 87 of 239 bars.

Nothing was wrong with the model's reasoning. It optimised exactly the thing
it was given. The mistake was giving it one number when the goal has two
parts: playable *and* still the song.

Dependency-free, like the verifier, and for the same reason: this is scoring
machinery, and scoring machinery that can break is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import Score
from .render import TEMPLATES, detect_chords, extract_melody


@dataclass
class Fidelity:
    """How much of the source survived, in three numbers between 0 and 1."""

    melodic_recall: float       # fraction of source melody notes still present
    harmonic_coverage: float    # fraction of bars whose chord is still implied
    accompaniment: float        # fraction of bars with any left hand at all

    def score(self) -> float:
        """One number, weighted by how much each loss hurts.

        The melody carries the most weight because losing it means the piece
        is no longer recognisable. Accompaniment carries the least: thinning
        it is legitimate arranging, and some passages are genuinely better
        unaccompanied. The weights make deletion costly without banning it.
        """
        return (
            0.55 * self.melodic_recall
            + 0.30 * self.harmonic_coverage
            + 0.15 * self.accompaniment
        )

    def summary(self) -> str:
        return (
            f"melody {self.melodic_recall:.0%}, harmony {self.harmonic_coverage:.0%}, "
            f"accompaniment {self.accompaniment:.0%} (score {self.score():.2f})"
        )


def measure(source: Score, arranged: Score) -> Fidelity:
    """Compare an arrangement against the source it came from."""
    source_melody = extract_melody(source)
    if not source_melody:
        return Fidelity(0.0, 0.0, 0.0)

    # Melody notes are compared by pitch class, not absolute pitch, because
    # octave folding is a legitimate and encouraged transformation. Comparing
    # absolute pitch would penalise the very fix that makes wide melodies
    # playable.
    wanted = {(round(n.onset, 1), n.pitch % 12) for n in source_melody}
    have = {(round(n.onset, 1), n.pitch % 12) for n in arranged.notes}
    melodic_recall = len(wanted & have) / len(wanted)

    chords = detect_chords(source, source_melody)
    arranged_by_bar: dict[int, set[int]] = {}
    for n in arranged.notes:
        if n.bar is not None:
            arranged_by_bar.setdefault(n.bar, set()).add(n.pitch % 12)

    covered = 0
    for bar, (root, quality) in chords.items():
        present = arranged_by_bar.get(bar, set())
        tones = {(root + o) % 12 for o in TEMPLATES[quality]}
        # A chord counts as implied if the root or the third is present. The
        # third is what distinguishes major from minor, so a bar keeping only
        # the root is grounded but colourless — half credit would be fairer,
        # but a binary test is easier to defend and harder to game.
        if root in present or (root + TEMPLATES[quality][1]) % 12 in present:
            covered += 1
    harmonic_coverage = covered / len(chords) if chords else 0.0

    bars_with_lh = {n.bar for n in arranged.notes if n.staff == 2 and n.bar}
    source_bars = {n.bar for n in source.notes if n.bar}
    accompaniment = len(bars_with_lh) / len(source_bars) if source_bars else 0.0

    return Fidelity(melodic_recall, harmonic_coverage, accompaniment)
