"""Application use cases.

Code outside the package should prefer these functions over wiring modules
together by hand. They keep CLI, API, UI, and future job-worker entry points
pointing at the same orchestration surface.
"""

from .services import (
    arrange_score,
    baseline_for,
    fidelity_for,
    render_plan,
    verify_score,
)

__all__ = [
    "arrange_score",
    "baseline_for",
    "fidelity_for",
    "render_plan",
    "verify_score",
]
