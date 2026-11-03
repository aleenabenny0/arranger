"""Playability verification. Zero third-party dependencies, by design."""

from .constraints import verify
from .solver import HandSolution, solve_hands
from .verdict import Certainty, Rule, Severity, SolverStatus, Verdict, Violation

__all__ = [
    "verify", "solve_hands", "HandSolution", "Certainty", "Rule", "Severity",
    "SolverStatus", "Verdict", "Violation",
]
