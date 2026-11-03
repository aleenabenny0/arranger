"""Deterministic repair suggestions for arrangement plans.

Each verifier finding maps to the plan edits most likely to clear it, ordered
by how little music they cost. That ordering is CLAUDE.md's reduction order
applied to fixes: change how something is played (roll it, pedal it, revoice
it) before removing anything, and remove doublings and inner voices before
touching bass movement. The melody is never on the list.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from .plan import ArrangementPlan, LHPattern, Voicing
from .verify import Rule, Verdict


@dataclass(frozen=True)
class RepairSuggestion:
    section_index: int | None
    field: str
    value: int | float | str | bool
    reason: str
    priority: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def _section_index_for_bar(plan: ArrangementPlan, bar: int | None) -> int | None:
    if bar is None:
        return None
    for index, section in enumerate(plan.sections):
        if section.start_bar <= bar <= section.end_bar:
            return index
    return None


_MOVING_PATTERNS = {
    LHPattern.ARPEGGIO, LHPattern.ALBERTI, LHPattern.BROKEN_OCTAVE, LHPattern.WALKING,
    LHPattern.STRIDE, LHPattern.BROKEN_TENTH,
}


def suggest_repairs(verdict: Verdict, plan: ArrangementPlan) -> list[RepairSuggestion]:
    """Turn verifier failures into concrete plan edits, cheapest first."""
    suggestions: list[RepairSuggestion] = []
    seen: set[tuple[int | None, str, int | str | bool]] = set()

    def add(section_index: int | None, field: str, value: int | str | bool, reason: str, priority: int) -> None:
        key = (section_index, field, value)
        if key not in seen:
            suggestions.append(RepairSuggestion(section_index, field, value, reason, priority))
            seen.add(key)

    for violation in verdict.hard:
        index = _section_index_for_bar(plan, violation.bar)
        section = plan.sections[index] if index is not None else None

        if violation.hand == "R" and violation.rule in (Rule.HAND_SPAN, Rule.LEAP_INFEASIBLE):
            # The right hand is the melody. It is never thinned; it is folded.
            window = 12 if section is None or not section.melody_fold_window else max(7, section.melody_fold_window - 2)
            add(index, "melody_fold_window", window,
                "Right-hand reach or leap belongs to the melody; fold it within the section by octaves.", 1)

        elif violation.rule == Rule.HAND_SPAN and violation.hand == "L":
            if section is not None and section.lh_pattern in (LHPattern.BLOCK, LHPattern.STRIDE):
                add(index, "roll_wide_chords", True, "Rolling makes a wide chord playable without losing a note.", 1)
            if violation.bar is not None:
                add(None, "pedal_bars", violation.bar,
                    "With the pedal down the hand can leave the bass note and reach the chord.", 1)
            if section is not None and section.lh_voices > 1:
                add(index, "lh_voices", section.lh_voices - 1, "Drop one inner voice from a chord that is too wide.", 2)
            add(index, "lh_pattern", LHPattern.PEDAL_TONE.value, "Hold the root alone where no chord fits the hand.", 3)

        elif violation.rule == Rule.HAND_POLYPHONY:
            voices = section.lh_voices if section is not None else 2
            add(index, "lh_voices", max(1, voices - 1), "Too many notes in one hand; drop an inner voice.", 1)

        elif violation.rule == Rule.LEAP_INFEASIBLE:
            if section is not None and section.voicing != Voicing.SMOOTH:
                add(index, "voicing", Voicing.SMOOTH.value,
                    "Choose inversions close to the previous chord so the hand barely moves.", 1)
            if section is not None and section.lh_pattern in _MOVING_PATTERNS:
                add(index, "lh_pattern", LHPattern.BLOCK.value, "A held chord removes the leaps inside the figure.", 2)
            add(index, "lh_pattern", LHPattern.PEDAL_TONE.value, "Use a stationary left hand to remove fast leaps.", 3)

        elif violation.rule == Rule.TOTAL_POLYPHONY:
            voices = section.lh_voices if section is not None else 2
            add(index, "lh_voices", max(1, voices - 1), "Too many keys held at once; thin the left hand.", 1)

        elif violation.rule == Rule.RANGE:
            if section is None:
                continue
            if violation.measured < violation.limit:
                if violation.hand == "R" or (violation.pitches and violation.pitches[0] >= 60):
                    add(index, "melody_shift", section.melody_shift + 12, "The melody is below this player's range.", 1)
                else:
                    add(index, "lh_octave", min(6, section.lh_octave + 1), "The left hand is below this player's range.", 1)
            else:
                add(index, "melody_shift", section.melody_shift - 12, "The melody is above this player's range.", 1)

    # Last resort, and only for what slowing down can fix: a hand that cannot
    # travel fast enough. It cannot fix a reach or too many notes.
    # Every slower step is offered, mildest first. One step often clears
    # nothing (a leap that needs half tempo is not helped by 85%), and a
    # search that only accepts improvements would stop there.
    if any(v.rule == Rule.LEAP_INFEASIBLE for v in verdict.hard):
        for rank, slower in enumerate((0.85, 0.7, 0.6, 0.5, 0.4)):
            if slower < plan.tempo_scale - 1e-9:
                add(None, "tempo_scale", slower,
                    "The hands cannot move this fast at full tempo; play the piece slower.", 4 + rank)

    return sorted(suggestions, key=lambda item: (item.priority, -1 if item.section_index is None else item.section_index, item.field))


def repair_prompt(verdict: Verdict, plan: ArrangementPlan, limit: int = 6) -> str:
    suggestions = suggest_repairs(verdict, plan)[:limit]
    if not suggestions:
        return "No deterministic repair suggestions were available."

    lines = ["DETERMINISTIC REPAIR SUGGESTIONS:"]
    for suggestion in suggestions:
        if suggestion.field == "pedal_bars":
            lines.append(f"- Add bar {suggestion.value} to pedal_bars: {suggestion.reason}")
            continue
        if suggestion.field == "tempo_scale":
            lines.append(f"- Only if nothing else works, set tempo_scale = {suggestion.value}: {suggestion.reason}")
            continue
        target = (
            f"section {suggestion.section_index}"
            if suggestion.section_index is not None
            else "the affected section"
        )
        lines.append(
            f"- Set {target}.{suggestion.field} = {suggestion.value!r}: "
            f"{suggestion.reason}"
        )
    return "\n".join(lines)
