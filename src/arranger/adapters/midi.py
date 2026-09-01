"""MIDI adapter.

The implementation currently lives in `arranger.io` for backwards
compatibility. New entry points should import from this adapter package so
future MusicXML/audio adapters can sit beside it.
"""

from __future__ import annotations

from ..io import MidiError, read_midi

__all__ = ["MidiError", "read_midi"]
