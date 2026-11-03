"""The deterministic half of arranging: evaluate a plan, and improve one without a model.

Everything here is pure computation on a source, a profile and a plan. It is
what runs when there are no model credentials, and it is what a model's plan
is measured against when there are: a model earns its cost only by beating
the candidate this module produces for free.

Dependency-free.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Callable

from .fidelity import Fidelity, measure
from .ir import Score
from .plan import ArrangementPlan, LHPattern
from .planner import FIDELITY_FLOOR, FIDELITY_WEIGHT, create_initial_plan
from .profile import PlayerProfile
from .render import render
from .repair import RepairSuggestion, suggest_repairs
from .verify import Verdict, verify

# Recorded with every saved arrangement so an old result can be explained, and
# reproduced, after the engine changes. Bump it when output for the same input
# would differ.
ALGORITHM_VERSION = "2026.09.2"


class Cancelled(Exception):
    """The caller asked the work to stop."""


def cost(hard: int, fidelity: Fidelity) -> float:
    """The single number every search here minimises.

    Hard violations, plus a penalty for each point of fidelity below the
    floor. Fidelity above the floor earns nothing: the goal is a playable
    arrangement that is still the song, not the most faithful arrangement
    imaginable, and rewarding surplus fidelity would trade playability for it.
    """
    return hard + FIDELITY_WEIGHT * max(0.0, FIDELITY_FLOOR - fidelity.score())


@dataclass
class Candidate:
    """One plan, rendered and judged."""

    plan: ArrangementPlan
    arranged: Score
    verdict: Verdict
    fidelity: Fidelity
    cost: float
    origin: str                      # "deterministic" | "local_repair" | "model" | "user"

    @property
    def hard(self) -> int:
        return len(self.verdict.hard)

    @property
    def accepted(self) -> bool:
        return self.verdict.playable and self.fidelity.score() >= FIDELITY_FLOOR

    def better_than(self, other: "Candidate | None") -> bool:
        """Strictly better. Ties keep the incumbent, so a model cannot displace
        an equally good deterministic plan just by arriving later."""
        if other is None:
            return True
        # Equal cost: prefer the faster tempo, then less strain.
        mine = (self.cost, -self.plan.tempo_scale, len(self.verdict.strain))
        theirs = (other.cost, -other.plan.tempo_scale, len(other.verdict.strain))
        return mine < theirs


def evaluate_plan(
    plan: ArrangementPlan, source: Score, profile: PlayerProfile, *, origin: str,
    time_budget: float | None = None,
) -> Candidate:
    """Render, verify and score one plan. Raises RenderError on an invalid plan."""
    arranged = render(plan, source)
    verdict = verify(arranged, profile, time_budget=time_budget)
    fidelity = measure(source, arranged)
    return Candidate(plan, arranged, verdict, fidelity, round(cost(len(verdict.hard), fidelity), 4), origin)


def apply_suggestion(plan: ArrangementPlan, suggestion: RepairSuggestion) -> ArrangementPlan | None:
    """A copy of `plan` with one suggested edit made, or None if it changes nothing."""
    edited = copy.deepcopy(plan)
    if suggestion.field == "pedal_bars":
        bar = int(suggestion.value)
        if bar in edited.pedal_bars:
            return None
        edited.pedal_bars = sorted({*edited.pedal_bars, bar})
        return edited
    if suggestion.field == "tempo_scale":
        if float(suggestion.value) >= edited.tempo_scale:
            return None
        edited.tempo_scale = float(suggestion.value)
        return edited
    if suggestion.section_index is None or not 0 <= suggestion.section_index < len(edited.sections):
        return None
    section = edited.sections[suggestion.section_index]
    value = suggestion.value
    if suggestion.field == "lh_pattern":
        value = LHPattern(value)
    if getattr(section, suggestion.field) == value:
        return None
    setattr(section, suggestion.field, value)
    return edited


def local_repair(
    candidate: Candidate,
    source: Score,
    profile: PlayerProfile,
    *,
    max_rounds: int = 40,
    max_trials_per_round: int = 14,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Candidate:
    """Hill-climb on the verifier's own suggestions. No model involved.

    Each round asks the repair rules what they would change, tries the top few
    edits one at a time, and keeps the first that lowers the cost. It stops
    when a round finds nothing better, when the plan is accepted, or when time
    runs out. The result is never worse than what came in.
    """
    best = candidate
    for _ in range(max_rounds):
        if best.accepted:
            break
        suggestions = suggest_repairs(best.verdict, best.plan)[:max_trials_per_round]
        improved = False
        for suggestion in suggestions:
            if should_cancel is not None and should_cancel():
                raise Cancelled()
            if deadline is not None and time.monotonic() > deadline:
                return best
            edited = apply_suggestion(best.plan, suggestion)
            if edited is None or edited.validate():
                continue
            trial = evaluate_plan(edited, source, profile, origin="local_repair")
            if trial.better_than(best):
                note = f"Local repair: {suggestion.reason}"
                if note not in trial.plan.notes:
                    trial.plan.notes = (trial.plan.notes + " " + note).strip()[:2000]
                best, improved = trial, True
                break
        if not improved:
            break
    return best


def arrange_deterministic(
    source: Score,
    profile: PlayerProfile,
    *,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> Candidate:
    """The best arrangement this system can produce without a model."""
    if progress:
        progress(0.05, "Analysing the piece")
    # The planner renders and checks the whole piece once per candidate, so on a
    # very long piece it could spend the entire budget. It gets half of what is
    # left; when that runs out it settles for its safest figures, and repair
    # still has time to work.
    planner_deadline = None if deadline is None else time.monotonic() + max(0.0, deadline - time.monotonic()) / 2

    def stop_planning() -> bool:
        if should_cancel is not None and should_cancel():
            return True
        return planner_deadline is not None and time.monotonic() > planner_deadline

    plan = create_initial_plan(source, profile, stop=stop_planning)
    if should_cancel is not None and should_cancel():
        raise Cancelled()
    if progress:
        progress(0.55, "Checking playability")
    draft = evaluate_plan(plan, source, profile, origin="deterministic")
    if progress:
        progress(0.7, "Repairing what the check found")
    best = local_repair(draft, source, profile, deadline=deadline, should_cancel=should_cancel)
    if progress:
        progress(0.85, "Refining voicing and bass")
    return refine_musically(best, source, profile, deadline=deadline, should_cancel=should_cancel)


# Whole-plan changes that make an arrangement more musical without making it
# harder. Order matters: each is judged against the result of the one before.
_REFINEMENTS: tuple[tuple[str, str, str], ...] = (
    ("voicing", "smooth", "Inversions chosen to keep the left hand still."),
    ("harmonic_rhythm", "detected", "Left hand follows chord changes inside the bar."),
    ("bass", "source", "Bass follows the source's own bass line."),
)


def refine_musically(
    candidate: Candidate,
    source: Score,
    profile: PlayerProfile,
    *,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Candidate:
    """Try the free improvements: smoother voicing, real harmonic rhythm, the real bass.

    `cost` ignores fidelity above the floor, on purpose, so it cannot see
    these. They are accepted on a stricter test instead: no new HARD finding,
    no more strain, and fidelity that is actually higher or strain that is
    actually lower. CLAUDE.md: voice leading beats voicing prettiness, and a
    chord's real bass is worth keeping when keeping it costs nothing.
    """
    best = candidate
    for field_name, value, reason in _REFINEMENTS:
        if should_cancel is not None and should_cancel():
            raise Cancelled()
        if deadline is not None and time.monotonic() > deadline:
            break
        edited = copy.deepcopy(best.plan)
        changed = False
        for section in edited.sections:
            if section.lh_voices > 0 and str(getattr(section, field_name)) != value:
                setattr(section, field_name, type(getattr(section, field_name))(value))
                changed = True
        if not changed or edited.validate():
            continue
        trial = evaluate_plan(edited, source, profile, origin=best.origin)
        no_harder = trial.hard <= best.hard and len(trial.verdict.strain) <= len(best.verdict.strain)
        gains = (
            trial.fidelity.score() > best.fidelity.score() + 0.002
            or len(trial.verdict.strain) < len(best.verdict.strain)
        )
        if no_harder and gains and trial.cost <= best.cost:
            if reason not in trial.plan.notes:
                trial.plan.notes = (trial.plan.notes + " " + reason).strip()[:2000]
            best = trial
    return best
