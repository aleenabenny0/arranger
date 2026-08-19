"""The arrangement plan: the only artifact the model is allowed to produce.

A plan describes *decisions*, not notes. "The left hand plays broken octaves
on the chord roots, bars 1-16, and the melody moves down an octave." The
renderer turns that into notes; the verifier checks those notes; failures come
back as violations the model can act on by editing one field.

Why this is worth the extra layer, at length:
`docs/build-log/why-plans-not-notes.md`

Dependency-free on purpose, like the verifier. Pydantic would be nicer for
validation errors, but every dependency is another thing that can break a
setup, and `validate()` covers what actually goes wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import StrEnum
from pathlib import Path


class LHPattern(StrEnum):
    """What the left hand does. See .claude/skills/left-hand-patterns/."""

    BLOCK = "block"                  # chord struck as one unit
    PEDAL_TONE = "pedal_tone"        # root held under everything
    BROKEN_OCTAVE = "broken_octave"  # root, octave, root, octave
    ARPEGGIO = "arpeggio"            # root, fifth, octave, third
    ALBERTI = "alberti"              # root, fifth, third, fifth
    WALKING = "walking"              # single-note line stepping between roots


class ReductionKind(StrEnum):
    """What got dropped. Ordered by how much musical damage each does."""

    DOUBLING = "doubling"            # same pitch class in two octaves
    INNER_VOICE = "inner_voice"      # thirds/fifths in the middle
    BASS_MOVEMENT = "bass_movement"  # walking bass becomes held roots
    HARMONIC_COLOUR = "harmonic_colour"  # 9ths/11ths/13ths become triads
    COUNTERMELODY = "countermelody"  # a secondary tune. Expensive; justify it.


@dataclass
class Reduction:
    """One thing removed, and why.

    `rationale` is for the human at the piano wondering where the
    countermelody went — not for the machine. Write it in plain language.
    """

    kind: ReductionKind
    start_bar: int
    end_bar: int
    rationale: str = ""


@dataclass
class Section:
    """A stretch of bars treated the same way.

    Sections exist because a piece is not uniform: a quiet verse and a big
    chorus want different left hands. Splitting into sections is how the
    model expresses that without describing individual notes.
    """

    start_bar: int
    end_bar: int
    lh_pattern: LHPattern = LHPattern.BLOCK
    # Move the melody by this many semitones. Usually 0 or ±12, to bring an
    # out-of-range part back onto the keyboard.
    melody_shift: int = 0
    # Which octave the left hand's root sits in. 2 is low, 3 is standard.
    lh_octave: int = 3
    # Notes per left-hand chord. Lower is easier and thinner.
    lh_voices: int = 3
    # Roll wide chords instead of striking them. Rolled notes are not
    # simultaneous, so this is the cheapest fix for a hand_span violation.
    roll_wide_chords: bool = False
    # Fold melody notes that stray outside a window this many semitones wide
    # back in by octaves. 0 disables it.
    #
    # This exists because melody_shift moves a whole section uniformly and so
    # cannot fix a leap *within* a section — and on real music, nearly every
    # residual violation is a right-hand leap or span. Without a lever aimed
    # at that, the model has nothing useful to do with the feedback it gets.
    # See docs/build-log/m5-action-space.md.
    melody_fold_window: int = 0
    label: str = ""

    def bars(self) -> range:
        return range(self.start_bar, self.end_bar + 1)


@dataclass
class ArrangementPlan:
    """A complete description of how to arrange one piece."""

    title: str = "untitled"
    target_skill: int = 5
    sections: list[Section] = field(default_factory=list)
    reductions: list[Reduction] = field(default_factory=list)
    # Bars where the sustain pedal is down. Currently advisory — the verifier
    # does not read this yet (see limitations.md), but plans should record it
    # so the information is there when it does.
    pedal_bars: list[int] = field(default_factory=list)
    notes: str = ""  # free-form reasoning from whoever wrote the plan

    def validate(self) -> list[str]:
        """Problems that would make this plan render into nonsense.

        Called before rendering. Catching a bad plan here produces one clear
        error; letting it render produces a broken score and a confusing pile
        of violations that look like the arrangement's fault.
        """
        problems = []
        if not self.sections:
            problems.append("plan has no sections")

        for i, s in enumerate(self.sections):
            if s.start_bar > s.end_bar:
                problems.append(f"section {i}: start_bar after end_bar")
            if s.start_bar < 1:
                problems.append(f"section {i}: bars are numbered from 1")
            if not 0 <= s.lh_voices <= 5:
                problems.append(f"section {i}: lh_voices must be 0-5")
            if not 0 <= s.lh_octave <= 6:
                problems.append(f"section {i}: lh_octave must be 0-6")
            if s.melody_fold_window and not 7 <= s.melody_fold_window <= 24:
                problems.append(
                    f"section {i}: melody_fold_window must be 0 or 7-24 "
                    "(narrower than a 5th destroys the tune)"
                )
            if abs(s.melody_shift) > 24:
                problems.append(
                    f"section {i}: melody_shift of {s.melody_shift} is more than "
                    "two octaves; that is almost certainly a mistake"
                )

        # Overlapping sections mean a bar has two different left hands, and
        # whichever section renders last silently wins. Better to refuse.
        covered: dict[int, int] = {}
        for i, s in enumerate(self.sections):
            for bar in s.bars():
                if bar in covered:
                    problems.append(
                        f"sections {covered[bar]} and {i} both cover bar {bar}"
                    )
                    break
                covered[bar] = i

        return problems

    def section_for_bar(self, bar: int) -> Section | None:
        for s in self.sections:
            if s.start_bar <= bar <= s.end_bar:
                return s
        return None

    # --- serialisation ---------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)

    @classmethod
    def from_dict(cls, data: dict) -> "ArrangementPlan":
        """Build a plan from parsed JSON.

        This is the boundary where model output enters the system, so it is
        strict: unknown keys are an error, not something to ignore. A model
        that invents `lh_style` instead of `lh_pattern` should be told, not
        silently given the default.
        """
        known = {"title", "target_skill", "sections", "reductions",
                 "pedal_bars", "notes"}
        if unknown := set(data) - known:
            raise ValueError(f"unknown plan keys: {sorted(unknown)}")

        sections = []
        for i, raw in enumerate(data.get("sections", [])):
            fields = {f for f in Section.__dataclass_fields__}
            if unknown := set(raw) - fields:
                raise ValueError(f"section {i}: unknown keys {sorted(unknown)}")
            raw = dict(raw)
            if "lh_pattern" in raw:
                try:
                    raw["lh_pattern"] = LHPattern(raw["lh_pattern"])
                except ValueError:
                    raise ValueError(
                        f"section {i}: '{raw['lh_pattern']}' is not a left-hand "
                        f"pattern. Choose from: {', '.join(p for p in LHPattern)}"
                    ) from None
            sections.append(Section(**raw))

        reductions = []
        for i, raw in enumerate(data.get("reductions", [])):
            raw = dict(raw)
            if "kind" in raw:
                try:
                    raw["kind"] = ReductionKind(raw["kind"])
                except ValueError:
                    raise ValueError(
                        f"reduction {i}: '{raw['kind']}' is not a reduction kind. "
                        f"Choose from: {', '.join(k for k in ReductionKind)}"
                    ) from None
            reductions.append(Reduction(**raw))

        return cls(
            title=data.get("title", "untitled"),
            target_skill=data.get("target_skill", 5),
            sections=sections,
            reductions=reductions,
            pedal_bars=data.get("pedal_bars", []),
            notes=data.get("notes", ""),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ArrangementPlan":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json() + "\n")


def simple_plan(
    last_bar: int, pattern: LHPattern = LHPattern.BLOCK, skill: int = 4
) -> ArrangementPlan:
    """One section covering the whole piece. The baseline to beat.

    Every agent run should be compared against this: if the model cannot do
    better than "block chords throughout", the model is not earning its cost.
    """
    return ArrangementPlan(
        title="baseline",
        target_skill=skill,
        sections=[Section(start_bar=1, end_bar=last_bar, lh_pattern=pattern)],
        notes="Baseline: uniform treatment, no musical judgement applied.",
    )
