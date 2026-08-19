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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rule"] = str(self.rule)
        d["severity"] = str(self.severity)
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
            },
            indent=indent,
        )
