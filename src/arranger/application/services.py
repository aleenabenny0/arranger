"""Use-case layer for Arranger.

This module intentionally contains orchestration, not musical rules. The
domain modules (`ir`, `plan`, `render`, `verify`, `profile`) stay dependency
free and testable; interfaces such as CLIs, APIs, and background workers call
these use cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fidelity import Fidelity, measure
from ..ir import Score
from ..plan import ArrangementPlan
from ..ports import ArrangementModel
from ..profile import PlayerProfile
from ..render import render
from ..verify import Verdict, verify

if TYPE_CHECKING:
    from ..agent import RunResult


def verify_score(score: Score, profile: PlayerProfile) -> Verdict:
    """Check whether a score is playable for one player."""
    return verify(score, profile)


def render_plan(plan: ArrangementPlan, source: Score) -> Score:
    """Turn an arrangement plan into the internal score representation."""
    return render(plan, source)


def fidelity_for(source: Score, arranged: Score) -> Fidelity:
    """Measure how much of the source music survived the arrangement."""
    return measure(source, arranged)


def baseline_for(
    source: Score, profile: PlayerProfile
) -> tuple[float, int, str, int, int]:
    """Compute the deterministic brute-force baseline the agent must beat."""
    from ..agent import brute_force_baseline

    return brute_force_baseline(source, profile)


def arrange_score(
    source: Score,
    profile: PlayerProfile,
    model: ArrangementModel | None = None,
    *,
    max_attempts: int | None = None,
    verbose: bool = True,
    countdown: bool = True,
) -> "RunResult":
    """Arrange a score using the bounded repair loop."""
    from ..agent import arrange

    kwargs = {
        "model": model,
        "verbose": verbose,
        "countdown": countdown,
    }
    if max_attempts is not None:
        kwargs["max_attempts"] = max_attempts
    return arrange(source, profile, **kwargs)
