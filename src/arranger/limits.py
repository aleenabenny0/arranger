"""Resource limits and typed import errors, shared by every importer.

Every file this system reads comes from a stranger. Each importer takes an
`ImportLimits` and must stop *before* doing unbounded work, not after. The
error types carry a stable `code` and a message that is safe to show a user;
anything diagnostic (byte offsets, parser internals) goes in `detail`, which
callers log and never return.

Dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImportLimits:
    max_bytes: int = 8 * 1024 * 1024           # raw upload
    max_uncompressed_bytes: int = 32 * 1024 * 1024  # after unzip (.mxl)
    max_archive_entries: int = 64
    max_compression_ratio: float = 200.0
    max_tracks: int = 64
    max_notes: int = 60_000
    max_events: int = 600_000                  # MIDI events / XML elements
    max_bars: int = 4_000
    max_duration_seconds: float = 3 * 60 * 60  # three hours of music
    max_xml_depth: int = 64
    max_title_chars: int = 200

    # Audio
    max_audio_bytes: int = 40 * 1024 * 1024
    max_audio_seconds: float = 12 * 60


DEFAULT_LIMITS = ImportLimits()

# Hard ceilings for anything that reaches the engine, however it got there
# (file import, JSON API, saved record). Plan validation and the API schemas
# both read these so there is one number to change.
MAX_SCORE_NOTES = 60_000
MAX_BAR_NUMBER = 4_000
MAX_PLAN_SECTIONS = 128
MAX_PLAN_REDUCTIONS = 256
MAX_PLAN_TEXT_CHARS = 4_000
MAX_LABEL_CHARS = 200


class ScoreImportError(ValueError):
    """Base class. `code` is stable API; `public` is safe to show; `detail` is not."""

    code = "import_failed"

    def __init__(self, public: str, *, detail: str = ""):
        super().__init__(public)
        self.public = public
        self.detail = detail or public


class UnsupportedFormat(ScoreImportError):
    code = "unsupported_format"


class MalformedFile(ScoreImportError):
    code = "malformed_file"


class LimitExceeded(ScoreImportError):
    code = "limit_exceeded"


class EmptyScore(ScoreImportError):
    code = "empty_score"


class UnsafeContent(ScoreImportError):
    """The file tried something a music file has no business doing."""

    code = "unsafe_content"
