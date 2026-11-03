"""The physical model of one specific player.

This file is the reason the project is interesting. "Playable" is not a
property of a score; it is a relation between a score and a pair of hands.
Encoding *whose* hands makes the whole system personal and the constraints
falsifiable: you can sit at the piano and find out whether max_span is right.

Every number here should be measurable by you in under a minute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

ALL_FINGERS = (1, 2, 3, 4, 5)   # 1 = thumb, in both hands


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
    # Free displacement allowed regardless of time - the hand is not a point,
    # and small repositioning happens within a single position.
    leap_slack: int = 5

    # --- Skill ---
    # 1-10, roughly RCM grades. Used by the difficulty rater, not the verifier.
    skill_level: int = 5

    # --- Per-hand differences (all optional) ---
    # Hands are not always a matched pair: an injury, a smaller left hand, a
    # finger that does not bend. None means "same as the shared value above".
    left_max_span: int | None = None
    right_max_span: int | None = None
    left_comfortable_span: int | None = None
    right_comfortable_span: int | None = None
    # Which fingers can be used, 1 = thumb .. 5 = little finger. Fewer fingers
    # means fewer simultaneous notes and shorter reaches between the fingers
    # that remain.
    left_fingers: tuple[int, ...] = ALL_FINGERS
    right_fingers: tuple[int, ...] = ALL_FINGERS

    # Fastest comfortable repetition of one key with one hand, notes per
    # second. Measure it: repeat a single note evenly for five seconds, count.
    max_repeat_rate: float = 8.0

    def __post_init__(self) -> None:
        # JSON has no tuples; accept lists so a loaded profile equals a built one.
        self.left_fingers = tuple(self.left_fingers)
        self.right_fingers = tuple(self.right_fingers)

    # --- per-hand accessors, "L" or "R" -----------------------------------

    def span_limit(self, hand: str) -> int:
        override = self.left_max_span if hand == "L" else self.right_max_span
        return self.max_span if override is None else override

    def comfortable_limit(self, hand: str) -> int:
        override = self.left_comfortable_span if hand == "L" else self.right_comfortable_span
        value = self.comfortable_span if override is None else override
        return min(value, self.span_limit(hand))

    def fingers(self, hand: str) -> tuple[int, ...]:
        return self.left_fingers if hand == "L" else self.right_fingers

    def note_limit(self, hand: str) -> int:
        return min(self.max_notes_per_hand, len(self.fingers(hand)))

    def validate(self) -> list[str]:
        """Catch profiles that are internally nonsense before they mislead you."""
        problems = []
        if self.lowest_pitch >= self.highest_pitch:
            problems.append("lowest_pitch must be below highest_pitch")
        if not (0 <= self.lowest_pitch <= 127 and 0 <= self.highest_pitch <= 127):
            problems.append("pitch range must lie within 0-127")
        if self.comfortable_span > self.max_span:
            problems.append("comfortable_span cannot exceed max_span")
        if not 1 <= self.max_span <= 24:
            problems.append("max_span must be 1-24 semitones")
        if self.comfortable_span < 1:
            problems.append("comfortable_span must be at least 1")
        if self.max_notes_per_hand < 1 or self.max_notes_per_hand > 5:
            problems.append("max_notes_per_hand must be 1-5")
        if not 0 < self.max_leap_rate <= 1e6:
            problems.append("max_leap_rate must be positive")
        if not 0 <= self.leap_slack <= 24:
            problems.append("leap_slack must be 0-24 semitones")
        if not 1 <= self.skill_level <= 10:
            problems.append("skill_level must be 1-10")
        if not 0.5 <= self.max_repeat_rate <= 30:
            problems.append("max_repeat_rate must be between 0.5 and 30 notes per second")
        for hand, label in (("L", "left"), ("R", "right")):
            span = getattr(self, f"{label}_max_span")
            comfortable = getattr(self, f"{label}_comfortable_span")
            if span is not None and not 1 <= span <= 24:
                problems.append(f"{label}_max_span must be 1-24 semitones")
            if comfortable is not None and comfortable > self.span_limit(hand):
                problems.append(f"{label}_comfortable_span cannot exceed that hand's max span")
            fingers = self.fingers(hand)
            if (
                not fingers
                or any(not isinstance(f, int) or isinstance(f, bool) or not 1 <= f <= 5 for f in fingers)
                or len(set(fingers)) != len(fingers)
            ):
                problems.append(f"{label}_fingers must be distinct finger numbers from 1 to 5")
        return problems

    def to_dict(self) -> dict:
        data = asdict(self)
        data["left_fingers"] = list(self.left_fingers)
        data["right_fingers"] = list(self.right_fingers)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerProfile":
        if not isinstance(data, dict):
            raise ValueError("a profile must be a JSON object")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            # Fail loudly. A typo'd key that silently does nothing is how you
            # spend an evening wondering why max_span changed nothing.
            raise ValueError(f"unknown profile keys: {sorted(unknown)}")
        try:
            profile = cls(**data)
        except TypeError as exc:
            raise ValueError(f"invalid profile: {exc}") from None
        numeric = ("lowest_pitch", "highest_pitch", "max_span", "comfortable_span",
                   "max_notes_per_hand", "max_leap_rate", "leap_slack", "skill_level",
                   "max_repeat_rate")
        for key in numeric:
            value = getattr(profile, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid profile: {key} must be a number")
        if problems := profile.validate():
            raise ValueError("invalid profile: " + "; ".join(problems))
        return profile

    @classmethod
    def load(cls, path: str | Path) -> "PlayerProfile":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")


# Useful reference points for calibration and for the eval harness. The first
# three are the originals. The rest exist because "intermediate" says nothing
# about hand size, and hand size is the limit people most often hit.
PRESETS = {
    "beginner": PlayerProfile(
        name="beginner", max_span=9, comfortable_span=7,
        max_notes_per_hand=3, max_leap_rate=30.0, skill_level=2,
        lowest_pitch=36, highest_pitch=84, max_repeat_rate=5.0,
    ),
    "intermediate": PlayerProfile(name="intermediate"),
    "advanced": PlayerProfile(
        name="advanced", max_span=14, comfortable_span=12,
        max_leap_rate=120.0, skill_level=8, max_repeat_rate=11.0,
    ),
    "late_beginner": PlayerProfile(
        name="late_beginner", max_span=10, comfortable_span=8, max_notes_per_hand=4,
        max_leap_rate=45.0, skill_level=3, lowest_pitch=33, highest_pitch=91,
        max_repeat_rate=6.0,
    ),
    "small_hands": PlayerProfile(
        name="small_hands", max_span=10, comfortable_span=8, max_leap_rate=70.0, skill_level=5,
    ),
    "large_hands": PlayerProfile(
        name="large_hands", max_span=16, comfortable_span=13, max_leap_rate=90.0, skill_level=6,
    ),
}

PRESET_DESCRIPTIONS = {
    "beginner": "First year or two. Reaches a sixth, three notes per hand, slow position changes.",
    "late_beginner": "Comfortable with simple pieces. Reaches a seventh, four notes per hand.",
    "intermediate": "Reaches an octave with all five fingers, moderate leaps.",
    "advanced": "Reaches a ninth, fast leaps, dense textures.",
    "small_hands": "Intermediate skill, but an octave is a stretch.",
    "large_hands": "Reaches a tenth comfortably.",
}


@dataclass
class Calibration:
    """Answers from a guided measurement session at the piano.

    Each field is one thing the player can test in under a minute. Anything
    left as None keeps the starting preset's value, so a partial calibration
    is still an improvement on a guess.
    """

    base_preset: str = "intermediate"
    name: str = "my hands"
    # White keys from thumb to little finger that can be pressed together
    # without strain / at full stretch. 8 white keys is an octave.
    left_comfortable_white_keys: int | None = None
    left_max_white_keys: int | None = None
    right_comfortable_white_keys: int | None = None
    right_max_white_keys: int | None = None
    # Seconds to play a two-octave leap (same hand) cleanly and land in time.
    two_octave_leap_seconds: float | None = None
    # Notes per second when repeating one key evenly.
    repeated_notes_per_second: float | None = None
    skill_level: int | None = None
    left_fingers: tuple[int, ...] | None = None
    right_fingers: tuple[int, ...] | None = None
    lowest_pitch: int | None = None
    highest_pitch: int | None = None
    notes: list[str] = field(default_factory=list)


# Semitones spanned by n adjacent white keys, measured C upward: 2 keys = a
# second (2), 8 keys = an octave (12), 10 keys = a tenth (16).
_WHITE_KEY_SEMITONES = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11, 8: 12, 9: 14, 10: 16, 11: 17, 12: 19}


def white_keys_to_semitones(white_keys: int) -> int:
    if white_keys not in _WHITE_KEY_SEMITONES:
        raise ValueError("white-key reach must be between 1 and 12 keys")
    return _WHITE_KEY_SEMITONES[white_keys]


def profile_from_calibration(c: Calibration) -> PlayerProfile:
    """Turn measurements into a profile. Raises ValueError on impossible answers."""
    if c.base_preset not in PRESETS:
        raise ValueError(f"unknown preset '{c.base_preset}'")
    base = PRESETS[c.base_preset]
    data = base.to_dict()
    data["name"] = c.name[:80] or "my hands"

    spans = {}
    for hand in ("left", "right"):
        maximum = getattr(c, f"{hand}_max_white_keys")
        comfortable = getattr(c, f"{hand}_comfortable_white_keys")
        if maximum is not None:
            spans[f"{hand}_max_span"] = white_keys_to_semitones(maximum)
        if comfortable is not None:
            spans[f"{hand}_comfortable_span"] = white_keys_to_semitones(comfortable)
    data.update(spans)
    # The shared values are the more limited hand: anything that does not know
    # which hand it is for must be safe for both.
    maxima = [v for k, v in spans.items() if k.endswith("_max_span")]
    comforts = [v for k, v in spans.items() if k.endswith("_comfortable_span")]
    if maxima:
        data["max_span"] = min(maxima)
    if comforts:
        data["comfortable_span"] = min(min(comforts), data["max_span"])
    data["comfortable_span"] = min(data["comfortable_span"], data["max_span"])
    for hand in ("left", "right"):
        key = f"{hand}_comfortable_span"
        limit = data.get(f"{hand}_max_span") or data["max_span"]
        if data.get(key) is not None:
            data[key] = min(data[key], limit)

    if c.two_octave_leap_seconds is not None:
        if not 0.05 <= c.two_octave_leap_seconds <= 5:
            raise ValueError("two_octave_leap_seconds must be between 0.05 and 5")
        data["max_leap_rate"] = round((24 - data["leap_slack"]) / c.two_octave_leap_seconds, 1)
    if c.repeated_notes_per_second is not None:
        data["max_repeat_rate"] = float(c.repeated_notes_per_second)
    if c.skill_level is not None:
        data["skill_level"] = c.skill_level
    for hand in ("left", "right"):
        fingers = getattr(c, f"{hand}_fingers")
        if fingers is not None:
            data[f"{hand}_fingers"] = list(fingers)
    if c.lowest_pitch is not None:
        data["lowest_pitch"] = c.lowest_pitch
    if c.highest_pitch is not None:
        data["highest_pitch"] = c.highest_pitch
    return PlayerProfile.from_dict(data)
