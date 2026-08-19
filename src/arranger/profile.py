"""The physical model of one specific player.

This file is the reason the project is interesting. "Playable" is not a
property of a score; it is a relation between a score and a pair of hands.
Encoding *whose* hands makes the whole system personal and the constraints
falsifiable: you can sit at the piano and find out whether max_span is right.

Every number here should be measurable by you in under a minute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class PlayerProfile:
    name: str = "default"
    instrument: str = "piano"

    # --- Instrument range (MIDI). Standard 88-key piano is 21..108. ---
    lowest_pitch: int = 21
    highest_pitch: int = 108

    # --- Hand geometry ---
    # max_span: largest interval in semitones you can hold as a block chord.
    # Measure it: reach for a 9th (14). Comfortable? Try a 10th (16).
    # 12 = an octave, the honest default for most adult hands.
    max_span: int = 12
    # Span you can reach but not comfortably; allowed, but flagged as "strain".
    comfortable_span: int = 9
    # Fingers available. 5 is not negotiable, but thumb-on-two-keys reduces it.
    max_notes_per_hand: int = 5

    # --- Movement ---
    # How fast the hand can relocate, in semitones per second.
    # Measure it: play a two-octave leap cleanly, time it. 24 semitones in
    # 0.35s is about 68 st/s, which is a reasonable intermediate value.
    max_leap_rate: float = 70.0
    # Free displacement allowed regardless of time — the hand is not a point,
    # and small repositioning happens within a single position.
    leap_slack: int = 5

    # --- Skill ---
    # 1-10, roughly RCM grades. Used by the difficulty rater, not the verifier.
    skill_level: int = 5

    def validate(self) -> list[str]:
        """Catch profiles that are internally nonsense before they mislead you."""
        problems = []
        if self.lowest_pitch >= self.highest_pitch:
            problems.append("lowest_pitch must be below highest_pitch")
        if self.comfortable_span > self.max_span:
            problems.append("comfortable_span cannot exceed max_span")
        if self.max_notes_per_hand < 1 or self.max_notes_per_hand > 5:
            problems.append("max_notes_per_hand must be 1-5")
        if self.max_leap_rate <= 0:
            problems.append("max_leap_rate must be positive")
        if not 1 <= self.skill_level <= 10:
            problems.append("skill_level must be 1-10")
        return problems

    @classmethod
    def load(cls, path: str | Path) -> "PlayerProfile":
        data = json.loads(Path(path).read_text())
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            # Fail loudly. A typo'd key that silently does nothing is how you
            # spend an evening wondering why max_span changed nothing.
            raise ValueError(f"unknown profile keys: {sorted(unknown)}")
        profile = cls(**data)
        if problems := profile.validate():
            raise ValueError("invalid profile: " + "; ".join(problems))
        return profile

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")


# Useful reference points for calibration and for the eval harness.
PRESETS = {
    "beginner": PlayerProfile(
        name="beginner", max_span=9, comfortable_span=7,
        max_notes_per_hand=3, max_leap_rate=30.0, skill_level=2,
        lowest_pitch=36, highest_pitch=84,
    ),
    "intermediate": PlayerProfile(name="intermediate"),
    "advanced": PlayerProfile(
        name="advanced", max_span=14, comfortable_span=12,
        max_leap_rate=120.0, skill_level=8,
    ),
}
