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
from ..musicianship import (
    analyze_note_importance,
    guidance_for_context,
    reduction_priority,
    summarize_importance,
)
from ..planner import (
    candidate_ranking_rows,
    create_initial_plan,
    detect_regions,
    rank_regions_by_difficulty,
    score_region_difficulty,
)
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


def plan_score(source: Score, profile: PlayerProfile) -> ArrangementPlan:
    """Create a deterministic first-pass arrangement plan."""
    return create_initial_plan(source, profile)


def plan_analysis(source: Score, profile: PlayerProfile) -> dict:
    """Return musician-style analysis used by planning and future ML ranking."""
    regions = detect_regions(source, profile)
    ranked = rank_regions_by_difficulty(source, profile)
    summary = summarize_importance(source)
    guidance = guidance_for_context(summary)
    importance = analyze_note_importance(source)
    removable = reduction_priority(source)

    return {
        "regions": [
            {
                "start_bar": region.start_bar,
                "end_bar": region.end_bar,
                "note_count": region.note_count,
                "average_density": round(region.average_density, 3),
                "max_simultaneous": region.max_simultaneous,
                "melody_span": region.melody_span,
                "max_melody_leap": region.max_melody_leap,
                "root_changes": region.root_changes,
                "difficulty": score_region_difficulty(
                    region, profile, source.tempo_bpm
                ).__dict__,
            }
            for region in regions
        ],
        "hardest_regions": [
            {
                "start_bar": region.start_bar,
                "end_bar": region.end_bar,
                "difficulty": difficulty.__dict__,
            }
            for region, difficulty in ranked
        ],
        "note_importance": [item.to_dict() for item in importance],
        "reduction_priority": [item.to_dict() for item in removable[:12]],
        "retrieved_guidance": [item.to_dict() for item in guidance],
        "candidate_rankings": [
            row.to_dict() for row in candidate_ranking_rows(source, profile)
        ],
    }


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
