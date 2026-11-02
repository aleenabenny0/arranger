"""The deterministic half of arranging: evaluate a plan, and improve one without a model.

Everything here is pure computation on a source, a profile and a plan. It is
what runs when there are no model credentials, and it is what a model's plan
is measured against when there are: a model earns its cost only by beating
the candidate this module produces for free.

Two candidates are compared on one ordering, `Candidate.quality`, everywhere:
in the planner's choice of draft, in local repair, in the model loop and in
the evaluation harness. Playability and the fidelity floor come first and
cannot be traded for anything; after that a faster tempo, then fewer strain
findings, then more of the music kept. The ordering is what lets a plan with
musical judgement beat a uniform one: an exhaustive search over whole-piece
figures reaches "playable and faithful" on most music, and a plan that
does the same with less strain and more of the piece intact is better, not
equal.

Dependency-free.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable

from .fidelity import Fidelity, measure
from .ir import Score
from .plan import ArrangementPlan, LHPattern, Section, patterns_for_skill
from .planner import FIDELITY_FLOOR, FIDELITY_RESOLUTION, FIDELITY_WEIGHT, create_initial_plan
from .profile import PlayerProfile
from .render import RenderError, last_bar, render
from .repair import RepairSuggestion, suggest_repairs
from .verify import Verdict, verify

# Recorded with every saved arrangement so an old result can be explained, and
# reproduced, after the engine changes. Bump it when output for the same input
# would differ.
ALGORITHM_VERSION = "2026.11.1"

# The whole-piece figures an exhaustive search tries. Three voice counts and
# three fold windows per pattern; the patterns themselves depend on the
# player's skill. Kept here, next to the engine that must never lose to them.
UNIFORM_VOICES = (1, 2, 3)
UNIFORM_FOLDS = (0, 12, 16)


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


QualityKey = tuple[float, float, int, float]


def quality_key(
    plan_cost: float, tempo_scale: float, strain: int, fidelity_score: float,
) -> QualityKey:
    """The ordering every comparison uses. Lower is better.

    1. `cost`: hard violations plus the fidelity shortfall. Nothing below
       trades against this.
    2. tempo: a plan that keeps more of the original tempo wins. Slowing the
       piece is the last resort, so it must cost something against a plan
       that did not need it.
    3. strain: fewer tiring passages.
    4. fidelity above the floor: more of the music kept, to
       `planner.FIDELITY_RESOLUTION` decimal places; closer than that is a tie.
    """
    return (
        round(plan_cost, 4),
        -round(tempo_scale, 4),
        strain,
        -round(fidelity_score, FIDELITY_RESOLUTION),
    )


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
    def strain(self) -> int:
        return len(self.verdict.strain)

    @property
    def accepted(self) -> bool:
        return self.verdict.playable and self.fidelity.score() >= FIDELITY_FLOOR

    @property
    def quality(self) -> QualityKey:
        return quality_key(self.cost, self.plan.tempo_scale, self.strain, self.fidelity.score())

    def better_than(self, other: "Candidate | None") -> bool:
        """Strictly better on `quality`. Ties keep the incumbent, so a model
        cannot displace an equally good deterministic plan just by arriving
        later."""
        if other is None:
            return True
        return self.quality < other.quality

    def describe(self) -> str:
        """One line a person or a model can read: what this candidate scores."""
        return (
            f"{self.hard} hard, {self.strain} strain, fidelity {self.fidelity.score():.3f}, "
            f"tempo {self.plan.tempo_scale:.0%}, cost {self.cost:.2f}"
        )


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


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def _check(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise Cancelled()


def _annotate(candidate: Candidate, note: str) -> None:
    if note not in candidate.plan.notes:
        candidate.plan.notes = (candidate.plan.notes + " " + note).strip()[:2000]


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
            _check(should_cancel)
            if _out_of_time(deadline):
                return best
            edited = apply_suggestion(best.plan, suggestion)
            if edited is None or edited.validate():
                continue
            trial = evaluate_plan(edited, source, profile, origin="local_repair")
            if trial.better_than(best):
                _annotate(trial, f"Local repair: {suggestion.reason}")
                best, improved = trial, True
                break
        if not improved:
            break
    return best


def polish(
    candidate: Candidate,
    source: Score,
    profile: PlayerProfile,
    *,
    max_rounds: int = 20,
    max_trials_per_round: int = 10,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Candidate:
    """Reduce strain in a plan that already plays, one section at a time.

    Local repair stops the moment a plan is accepted; what it leaves behind is
    playable but may still tire the player where a uniform figure would not.
    This pass feeds the STRAIN findings through the same repair rules and keeps
    an edit only when the whole ordering improves: it can never add a hard
    finding, slow the piece or drop below the fidelity floor to buy comfort,
    because `better_than` ranks those first.
    """
    best = candidate
    if not best.accepted:
        return best
    for _ in range(max_rounds):
        if best.strain == 0:
            break
        suggestions = suggest_repairs(best.verdict, best.plan, include_strain=True)
        improved = False
        for suggestion in suggestions[:max_trials_per_round]:
            _check(should_cancel)
            if _out_of_time(deadline):
                return best
            if suggestion.field == "tempo_scale":
                continue  # comfort is never worth the tempo; the ordering would refuse it anyway
            edited = apply_suggestion(best.plan, suggestion)
            if edited is None or edited.validate():
                continue
            trial = evaluate_plan(edited, source, profile, origin="local_repair")
            if trial.better_than(best):
                _annotate(trial, f"Polish: {suggestion.reason}")
                best, improved = trial, True
                break
        if not improved:
            break
    return best


def uniform_plans(
    source: Score, profile: PlayerProfile, *, legal_only: bool = True,
) -> Iterator[ArrangementPlan]:
    """Every whole-piece plan an exhaustive search would try, easiest first.

    With `legal_only` the patterns are the ones this player's skill allows,
    which is the same rule the planner and plan validation apply to every
    other plan. The full set is what `agent.brute_force_baseline` searches.
    """
    end = last_bar(source)
    allowed = patterns_for_skill(profile.skill_level) if legal_only else tuple(LHPattern)
    ordered = [p for p in (LHPattern.BLOCK, LHPattern.PEDAL_TONE) if p in allowed]
    ordered += [p for p in LHPattern if p in allowed and p not in ordered]
    for pattern in ordered:
        for voices in UNIFORM_VOICES:
            for fold in UNIFORM_FOLDS:
                yield ArrangementPlan(
                    title=f"{source.title} whole-piece {pattern.value}",
                    target_skill=profile.skill_level,
                    sections=[Section(1, end, pattern, lh_voices=voices, melody_fold_window=fold,
                                      label="whole piece")],
                    notes="Whole-piece figure, the same for every bar.",
                )


def seed_uniform(
    candidate: Candidate | None,
    source: Score,
    profile: PlayerProfile,
    *,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    legal_only: bool = True,
) -> Candidate:
    """Compare `candidate` against every whole-piece figure and keep the best.

    This is what makes the deterministic engine a superset of the exhaustive
    search it is measured against: whatever a uniform plan can reach, the
    engine has seen. Bounded by `deadline`; the figures come easiest first, so
    a cut-short pass has still tried the ones that usually win.
    """
    best = candidate
    for plan in uniform_plans(source, profile, legal_only=legal_only):
        _check(should_cancel)
        if _out_of_time(deadline):
            break
        try:
            trial = evaluate_plan(plan, source, profile, origin="deterministic")
        except RenderError:
            continue
        if trial.better_than(best):
            best = trial
    assert best is not None
    return best


def arrange_deterministic(
    source: Score,
    profile: PlayerProfile,
    *,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> Candidate:
    """The best arrangement this system can produce without a model.

    Planner draft, local repair, then the whole-piece figures as a floor that
    cannot be fallen through, then the musical refinements and a strain
    polish. Every stage keeps the better candidate on the same ordering, so
    the result is never worse than any single stage would have produced.
    """
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
    _check(should_cancel)
    if progress:
        progress(0.5, "Checking playability")
    draft = evaluate_plan(plan, source, profile, origin="deterministic")
    if progress:
        progress(0.6, "Repairing what the check found")
    best = local_repair(draft, source, profile, deadline=deadline, should_cancel=should_cancel)

    if progress:
        progress(0.7, "Comparing with whole-piece figures")
    # The uniform figures get a slice of what is left, never all of it, so the
    # refinements after them always run.
    seed_deadline = None if deadline is None else time.monotonic() + max(0.0, deadline - time.monotonic()) * 0.5
    seeded = seed_uniform(best, source, profile, deadline=seed_deadline, should_cancel=should_cancel)
    if seeded is not best:
        best = local_repair(seeded, source, profile, deadline=deadline, should_cancel=should_cancel)

    if progress:
        progress(0.85, "Refining voicing and bass")
    best = refine_musically(best, source, profile, deadline=deadline, should_cancel=should_cancel)
    if progress:
        progress(0.93, "Easing the hardest passages")
    return polish(best, source, profile, deadline=deadline, should_cancel=should_cancel)


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
        _check(should_cancel)
        if _out_of_time(deadline):
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
        no_harder = trial.hard <= best.hard and trial.strain <= best.strain
        gains = (
            trial.fidelity.score() > best.fidelity.score() + 0.002
            or trial.strain < best.strain
        )
        if no_harder and gains and trial.cost <= best.cost:
            _annotate(trial, reason)
            best = trial
    return best
