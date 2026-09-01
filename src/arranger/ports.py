"""Boundary protocols for infrastructure that sits outside the core.

The domain code is deliberately small and mostly pure. These protocols name
the places where the system talks to the outside world: model clients, file
formats, queues, databases, or web handlers can live behind these boundaries
without changing the verifier, renderer, or plan schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .ir import Score


class ArrangementModel(Protocol):
    """A chat-style model client used by the repair loop."""

    input_tokens: int
    output_tokens: int

    def __call__(self, messages: list[dict]) -> str:
        """Return model text for the current conversation."""


class ScoreReader(Protocol):
    """Adapter that turns an external score artifact into the internal IR."""

    def __call__(self, path: str | Path) -> Score:
        """Read a path-like value and return a domain score."""
