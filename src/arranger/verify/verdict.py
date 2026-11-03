"""The output contract of the verifier.

This is the most important interface in the project. The repair subagent never
reads stdout, never parses prose, never sees a stack trace. It sees this.

Design rule: every violation must carry enough information to *act on*.
"span too wide" is useless. "bar 14 beat 3, left hand, C2-A3 is 21 semitones,
your max is 12, drop or transpose one of these three pitches" is actionable.
A violation the model cannot act on is a bug in the verifier, not the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import StrEnum


class Severity(StrEnum):
    # Physically impossible. Blocks acceptance. No judgement call involved.
    HARD = "hard"
    # Possible but uncomfortable. Advisory; contributes to difficulty score.
    STRAIN = "strain"


class Rule(StrEnum):
    RANGE = "range"
    HAND_SPAN = "hand_span"
    HAND_POLYPHONY = "hand_polyphony"
    LEAP_INFEASIBLE = "leap_infeasible"
    TOTAL_POLYPHONY = "total_polyphony"
    # Added with the phrase solver. STRAIN only, per CLAUDE.md: a new rule is
    # not promoted to HARD until it has been checked against human-made
    # arrangements in evals/corpus/.
    LEGATO_BREAK = "legato_break"        # a note must be let go early to make the next position
    HAND_CROSSING = "hand_crossing"      # the left hand plays above the right
    FAST_REPETITION = "fast_repetition"  # one key restruck faster than this player repeats
    FINGER_STRETCH = "finger_stretch"    # chord fits the hand's span but not between its fingers


class Certainty(StrEnum):
    """How much a finding proves.

    PROVEN: within the modelled problem, every way of dividing the notes
    between the hands was considered and none avoids this. It is a statement
    about the model, not about all of human technique.

    UNPROVEN: the search was cut short (time or width limit), so a better
    division of the hands may exist that it never tried. Treat it as "could
    not find a way", never as "there is no way".
    """

    PROVEN = "proven"
    UNPROVEN = "unproven"


class SolverStatus(StrEnum):
    FEASIBLE = "feasible"        # an assignment with no HARD finding was found
    INFEASIBLE = "infeasible"    # exhaustive within the model; none exists
    UNKNOWN = "unknown"          # search was cut short and found none


@dataclass
class Violation:
    rule: Rule
    severity: Severity
    time: float                      # seconds
    bar: int | None = None
    hand: str | None = None
    pitches: list[int] = field(default_factory=list)
    measured: float = 0.0            # what we observed
    limit: float = 0.0               # what the profile allows
    message: str = ""                # human-readable, for you not the model
    note_ids: list[str] = field(default_factory=list)   # which notes, for highlighting
    certainty: Certainty = Certainty.PROVEN

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rule"] = str(self.rule)
        d["severity"] = str(self.severity)
        d["certainty"] = str(self.certainty)
        return d


@dataclass
class Verdict:
    title: str
    profile: str
    playable: bool
    violations: list[Violation] = field(default_factory=list)
    # Populated by later milestones; present from day one so the JSON shape
    # never changes on consumers.
    harmonic_fidelity: float | None = None
    melodic_recall: float | None = None
    difficulty_est: float | None = None
    # How the hand-assignment search ended. See SolverStatus.
    solver_status: SolverStatus = SolverStatus.FEASIBLE
    # note id -> "L" / "R", for notes that have ids. Lets the engraver put
    # unstaffed notes on the staff of the hand that will play them.
    hands: dict[str, str] = field(default_factory=dict)

    @property
    def hard(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.HARD]

    @property
    def strain(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.STRAIN]

    def summary(self) -> dict:
        """Counts by rule. This is what the scorecard aggregates."""
        counts: dict[str, int] = {}
        for v in self.violations:
            key = f"{v.rule}:{v.severity}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "title": self.title,
                "profile": self.profile,
                "playable": self.playable,
                "n_hard": len(self.hard),
                "n_strain": len(self.strain),
                "summary": self.summary(),
                "violations": [v.to_dict() for v in self.violations],
                "harmonic_fidelity": self.harmonic_fidelity,
                "melodic_recall": self.melodic_recall,
                "difficulty_est": self.difficulty_est,
                "solver_status": str(self.solver_status),
            },
            indent=indent,
        )
