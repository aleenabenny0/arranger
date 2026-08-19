"""Playability verification. Zero third-party dependencies, by design."""

from .constraints import verify
from .verdict import Rule, Severity, Verdict, Violation

__all__ = ["verify", "Rule", "Severity", "Verdict", "Violation"]
