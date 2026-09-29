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

from .limits import (
    MAX_BAR_NUMBER,
    MAX_LABEL_CHARS,
    MAX_PLAN_REDUCTIONS,
    MAX_PLAN_SECTIONS,
    MAX_PLAN_TEXT_CHARS,
)


MIN_TEMPO_SCALE = 0.4


def _is_int(value: object) -> bool:
    """A real integer. `True` is an int in Python; it is not a bar number."""
    return isinstance(value, int) and not isinstance(value, bool)


class LHPattern(StrEnum):
    """What the left hand does. See .claude/skills/left-hand-patterns/."""

    BLOCK = "block"                  # chord struck as one unit
    PEDAL_TONE = "pedal_tone"        # root held under everything
    BROKEN_OCTAVE = "broken_octave"  # root, octave, root, octave
    ARPEGGIO = "arpeggio"            # root, fifth, octave, third
    ALBERTI = "alberti"              # root, fifth, third, fifth
    WALKING = "walking"              # single-note line stepping between roots
    STRIDE = "stride"                # bass note on strong beats, chord above on weak
    BROKEN_TENTH = "broken_tenth"    # root, fifth, tenth: a wide chord played in sequence


# Lowest difficulty at which each pattern is reasonable, from the selection
# table in .claude/skills/left-hand-patterns/. A plan should not exceed the
# player's skill_level by more than one.
PATTERN_MIN_DIFFICULTY: dict[LHPattern, int] = {
    LHPattern.BLOCK: 1,
    LHPattern.PEDAL_TONE: 1,
    LHPattern.BROKEN_OCTAVE: 3,
    LHPattern.ARPEGGIO: 4,
    LHPattern.ALBERTI: 4,
    LHPattern.WALKING: 5,
    LHPattern.BROKEN_TENTH: 6,
    LHPattern.STRIDE: 7,
}


def patterns_for_skill(skill_level: int) -> tuple[LHPattern, ...]:
    """The skill-level ceilings from the left-hand-patterns skill, verbatim."""
    allowed = [LHPattern.BLOCK, LHPattern.PEDAL_TONE]
    if skill_level >= 4:
        allowed += [LHPattern.ARPEGGIO, LHPattern.ALBERTI, LHPattern.BROKEN_OCTAVE,
                    LHPattern.WALKING]
    if skill_level >= 7:
        allowed += [LHPattern.STRIDE, LHPattern.BROKEN_TENTH]
    return tuple(allowed)


class Voicing(StrEnum):
    """How left-hand chords are laid out from one to the next."""

    ROOT = "root"      # always root position: predictable, but the hand jumps
    SMOOTH = "smooth"  # pick the inversion nearest the previous chord


class BassMode(StrEnum):
    """Which note sits at the bottom."""

    ROOT = "root"      # the chord root
    SOURCE = "source"  # the bass note the source actually played (keeps slash chords and bass lines)


class HarmonicRhythm(StrEnum):
    """How often the left hand changes chord."""

    BAR = "bar"            # one chord per bar
    DETECTED = "detected"  # follow chord changes found inside the bar


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
    # Voice leading beats voicing prettiness (CLAUDE.md): "smooth" keeps the
    # hand still by choosing inversions, at the cost of not always having the
    # root on the bottom.
    voicing: Voicing = Voicing.ROOT
    # "source" keeps the bass line the piece really has. It matters most where
    # the bass is not the root: first inversions, pedal points, walking lines.
    bass: BassMode = BassMode.ROOT
    # "detected" lets the left hand change chord mid-bar where the source does.
    harmonic_rhythm: HarmonicRhythm = HarmonicRhythm.BAR

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
    # Play the whole piece at this fraction of the source tempo. It is the one
    # lever for a right hand that is simply too fast for this player: the
    # melody may not be thinned, but it may be played slower, which is what a
    # teacher would say. 1.0 is the source tempo. It is a last resort, tried
    # only after every fix that keeps the tempo.
    tempo_scale: float = 1.0

    def validate(self) -> list[str]:
        """Problems that would make this plan render into nonsense.

        Called before rendering. Catching a bad plan here produces one clear
        error; letting it render produces a broken score and a confusing pile
        of violations that look like the arrangement's fault.

        Every check is O(sections). An earlier version walked
        `range(start_bar, end_bar + 1)` to find overlaps, so a plan with
        `end_bar = 10**9` cost a gigabyte and a minute. Plans come from models
        and from the network; validation must never be the expensive part.
        """
        problems = []
        if not self.sections:
            problems.append("plan has no sections")
        if len(self.sections) > MAX_PLAN_SECTIONS:
            problems.append(f"plan has {len(self.sections)} sections; the limit is {MAX_PLAN_SECTIONS}")
            return problems
        if len(self.reductions) > MAX_PLAN_REDUCTIONS:
            problems.append(f"plan has {len(self.reductions)} reductions; the limit is {MAX_PLAN_REDUCTIONS}")
            return problems
        if not _is_int(self.target_skill) or not 1 <= self.target_skill <= 10:
            problems.append("target_skill must be an integer 1-10")
        if (
            isinstance(self.tempo_scale, bool) or not isinstance(self.tempo_scale, (int, float))
            or not MIN_TEMPO_SCALE <= self.tempo_scale <= 1.0
        ):
            problems.append(f"tempo_scale must be a number from {MIN_TEMPO_SCALE} to 1.0")
        if not isinstance(self.notes, str) or len(self.notes) > MAX_PLAN_TEXT_CHARS:
            problems.append(f"notes must be text of at most {MAX_PLAN_TEXT_CHARS} characters")
        if not isinstance(self.title, str) or len(self.title) > MAX_LABEL_CHARS:
            problems.append(f"title must be text of at most {MAX_LABEL_CHARS} characters")

        well_typed: list[tuple[int, Section]] = []
        for i, s in enumerate(self.sections):
            ints = {
                "start_bar": s.start_bar, "end_bar": s.end_bar, "melody_shift": s.melody_shift,
                "lh_octave": s.lh_octave, "lh_voices": s.lh_voices,
                "melody_fold_window": s.melody_fold_window,
            }
            wrong = [name for name, value in ints.items() if not _is_int(value)]
            if wrong:
                problems.append(f"section {i}: {', '.join(wrong)} must be whole numbers")
                continue
            if not isinstance(s.roll_wide_chords, bool):
                problems.append(f"section {i}: roll_wide_chords must be true or false")
                continue
            if not isinstance(s.label, str) or len(s.label) > MAX_LABEL_CHARS:
                problems.append(f"section {i}: label must be text of at most {MAX_LABEL_CHARS} characters")
                continue
            try:
                LHPattern(s.lh_pattern)
                Voicing(s.voicing)
                BassMode(s.bass)
                HarmonicRhythm(s.harmonic_rhythm)
            except ValueError as exc:
                problems.append(f"section {i}: {exc}")
                continue
            well_typed.append((i, s))

            if s.start_bar > s.end_bar:
                problems.append(f"section {i}: start_bar after end_bar")
            if s.start_bar < 1:
                problems.append(f"section {i}: bars are numbered from 1")
            if s.end_bar > MAX_BAR_NUMBER:
                problems.append(f"section {i}: end_bar {s.end_bar} is beyond the limit of {MAX_BAR_NUMBER}")
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
        # Sorted by start, an overlap can only be with the furthest-reaching
        # section seen so far.
        ordered = sorted(
            ((i, s) for i, s in well_typed if s.start_bar <= s.end_bar),
            key=lambda item: (item[1].start_bar, item[0]),
        )
        reach_index, reach_end = -1, 0
        for i, s in ordered:
            if reach_index >= 0 and s.start_bar <= reach_end:
                first, second = sorted((reach_index, i))
                problems.append(f"sections {first} and {second} both cover bar {s.start_bar}")
            if s.end_bar > reach_end:
                reach_index, reach_end = i, s.end_bar

        for i, r in enumerate(self.reductions):
            if not _is_int(r.start_bar) or not _is_int(r.end_bar):
                problems.append(f"reduction {i}: bars must be whole numbers")
            elif r.start_bar < 1 or r.end_bar < r.start_bar or r.end_bar > MAX_BAR_NUMBER:
                problems.append(f"reduction {i}: bar range {r.start_bar}-{r.end_bar} is not valid")
            if not isinstance(r.rationale, str) or len(r.rationale) > MAX_PLAN_TEXT_CHARS:
                problems.append(f"reduction {i}: rationale is too long")

        if not isinstance(self.pedal_bars, list) or len(self.pedal_bars) > MAX_BAR_NUMBER:
            problems.append("pedal_bars must be a list of bar numbers")
        elif any(not _is_int(b) or not 1 <= b <= MAX_BAR_NUMBER for b in self.pedal_bars):
            problems.append(f"pedal_bars must be whole numbers from 1 to {MAX_BAR_NUMBER}")

        return problems

    def validate_for_source(self, last_bar: int, *, skill_level: int | None = None) -> list[str]:
        """Problems that only show up against the piece being arranged.

        `validate()` asks whether the plan makes sense at all. This asks
        whether it fits *this* source: does it cover the music, does it refer
        to bars that exist, is it within the player's skill ceiling.
        """
        problems = self.validate()
        if problems:
            return problems
        covered = sorted((s.start_bar, s.end_bar) for s in self.sections)
        uncovered: list[str] = []
        cursor = 1
        for start, end in covered:
            if start > cursor:
                uncovered.append(f"{cursor}-{min(start - 1, last_bar)}" if start - 1 > cursor else str(cursor))
            cursor = max(cursor, end + 1)
            if cursor > last_bar:
                break
        if cursor <= last_bar:
            uncovered.append(f"{cursor}-{last_bar}" if last_bar > cursor else str(cursor))
        if uncovered:
            problems.append(
                "bars " + ", ".join(uncovered[:8]) + " are not covered by any section; "
                "they would have no left hand"
            )
        beyond = [i for i, s in enumerate(self.sections) if s.start_bar > last_bar]
        if beyond:
            problems.append(
                f"section(s) {', '.join(map(str, beyond[:8]))} start after the last bar ({last_bar})"
            )
        if any(b > last_bar for b in self.pedal_bars):
            problems.append(f"pedal_bars refers to bars after the last bar ({last_bar})")
        if skill_level is not None:
            too_hard = sorted(
                {
                    str(s.lh_pattern) for s in self.sections
                    if s.lh_voices > 0 and PATTERN_MIN_DIFFICULTY[LHPattern(s.lh_pattern)] > skill_level + 1
                }
            )
            if too_hard:
                problems.append(
                    f"pattern(s) {', '.join(too_hard)} are more than one level above "
                    f"this player's skill_level of {skill_level}"
                )
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
        if not isinstance(data, dict):
            raise ValueError("a plan must be a JSON object")
        known = {"title", "target_skill", "sections", "reductions",
                 "pedal_bars", "notes", "tempo_scale"}
        if unknown := set(data) - known:
            raise ValueError(f"unknown plan keys: {sorted(unknown)}")

        raw_sections = data.get("sections", [])
        raw_reductions = data.get("reductions", [])
        if not isinstance(raw_sections, list) or not isinstance(raw_reductions, list):
            raise ValueError("sections and reductions must be lists")
        if len(raw_sections) > MAX_PLAN_SECTIONS:
            raise ValueError(f"too many sections ({len(raw_sections)}); the limit is {MAX_PLAN_SECTIONS}")
        if len(raw_reductions) > MAX_PLAN_REDUCTIONS:
            raise ValueError(f"too many reductions; the limit is {MAX_PLAN_REDUCTIONS}")

        enums = {"lh_pattern": LHPattern, "voicing": Voicing, "bass": BassMode,
                 "harmonic_rhythm": HarmonicRhythm}
        sections = []
        for i, raw in enumerate(raw_sections):
            if not isinstance(raw, dict):
                raise ValueError(f"section {i}: must be an object")
            fields = {f for f in Section.__dataclass_fields__}
            if unknown := set(raw) - fields:
                raise ValueError(f"section {i}: unknown keys {sorted(unknown)}")
            if "start_bar" not in raw or "end_bar" not in raw:
                raise ValueError(f"section {i}: start_bar and end_bar are required")
            raw = dict(raw)
            for key, enum in enums.items():
                if key in raw:
                    try:
                        raw[key] = enum(raw[key])
                    except ValueError:
                        noun = "a left-hand pattern" if key == "lh_pattern" else f"a valid {key}"
                        raise ValueError(
                            f"section {i}: '{raw[key]}' is not {noun}. "
                            f"Choose from: {', '.join(p for p in enum)}"
                        ) from None
            sections.append(Section(**raw))

        reductions = []
        for i, raw in enumerate(raw_reductions):
            if not isinstance(raw, dict):
                raise ValueError(f"reduction {i}: must be an object")
            if unknown := set(raw) - set(Reduction.__dataclass_fields__):
                raise ValueError(f"reduction {i}: unknown keys {sorted(unknown)}")
            missing = {"kind", "start_bar", "end_bar"} - set(raw)
            if missing:
                raise ValueError(f"reduction {i}: missing {sorted(missing)}")
            raw = dict(raw)
            try:
                raw["kind"] = ReductionKind(raw["kind"])
            except ValueError:
                raise ValueError(
                    f"reduction {i}: '{raw['kind']}' is not a reduction kind. "
                    f"Choose from: {', '.join(k for k in ReductionKind)}"
                ) from None
            reductions.append(Reduction(**raw))

        plan = cls(
            title=data.get("title", "untitled"),
            target_skill=data.get("target_skill", 5),
            sections=sections,
            reductions=reductions,
            pedal_bars=data.get("pedal_bars", []),
            notes=data.get("notes", ""),
            tempo_scale=data.get("tempo_scale", 1.0),
        )
        # Types are checked here, at the boundary, so nothing downstream ever
        # meets `start_bar="1"` and turns it into a TypeError and a 500.
        type_problems = [p for p in plan.validate() if "must be" in p or "limit" in p]
        if type_problems:
            raise ValueError("; ".join(type_problems))
        return plan

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
