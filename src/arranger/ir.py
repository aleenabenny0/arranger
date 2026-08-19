"""Intermediate representation for verification.

This module is deliberately dependency-free. Everything the verifier needs to
know about a piece of music lives here, as plain data.

Why not just verify MusicXML directly? Because then every test would need a
MusicXML file, and every bug would be ambiguous: is the constraint wrong, or
is the parser wrong? Keeping a tiny IR in the middle means constraint tests
are three lines long and mean exactly one thing.

Loaders (MusicXML -> Score, MIDI -> Score) live in arranger.io and are the
only place that touches third-party libraries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

Hand = Literal["L", "R"]

# Middle C. MIDI 60. Written down because you will second-guess it at 1am.
MIDDLE_C = 60


@dataclass(frozen=True, slots=True)
class Note:
    """A single sounding note.

    pitch:    MIDI number. 60 = middle C, 21 = A0 (lowest piano key), 108 = C8.
    onset:    seconds from the start of the piece.
    duration: seconds. Must be > 0.
    staff:    1 = upper (usually right hand), 2 = lower. None means the
              arranger did not commit to a staff and we must infer the hand.
    bar:      optional, for human-readable violation reports.
    """

    pitch: int
    onset: float
    duration: float
    staff: int | None = None
    bar: int | None = None
    voice: int = 1

    @property
    def offset(self) -> float:
        return self.onset + self.duration

    def sounds_at(self, t: float, eps: float = 1e-6) -> bool:
        """True if the note is sounding at time t.

        Half-open interval: a note ending exactly when another begins is not
        simultaneous with it. Without this, every legato passage would report
        phantom hand-span violations.
        """
        return self.onset - eps <= t < self.offset - eps


@dataclass
class Score:
    """A whole arrangement, flattened to note events."""

    notes: list[Note] = field(default_factory=list)
    tempo_bpm: float = 100.0
    title: str = "untitled"

    def __post_init__(self) -> None:
        self.notes.sort(key=lambda n: (n.onset, n.pitch))

    @property
    def onsets(self) -> list[float]:
        """Distinct onset times, ascending.

        These are the only instants worth checking. A hand-span violation can
        only begin when some note begins, so sampling at onsets is exhaustive,
        not an approximation.
        """
        return sorted({n.onset for n in self.notes})

    def sounding_at(self, t: float) -> list[Note]:
        return [n for n in self.notes if n.sounds_at(t)]

    def duration(self) -> float:
        return max((n.offset for n in self.notes), default=0.0)

    @classmethod
    def from_tuples(
        cls, rows: Iterable[tuple], tempo_bpm: float = 100.0, title: str = "untitled"
    ) -> "Score":
        """Build a Score from (pitch, onset, duration[, staff]) tuples.

        Test-authoring convenience. Keeps constraint tests readable.
        """
        notes = []
        for row in rows:
            pitch, onset, dur = row[0], float(row[1]), float(row[2])
            staff = row[3] if len(row) > 3 else None
            notes.append(Note(pitch=pitch, onset=onset, duration=dur, staff=staff))
        return cls(notes=notes, tempo_bpm=tempo_bpm, title=title)


def pitch_name(midi: int) -> str:
    """MIDI number -> readable name, e.g. 60 -> 'C4'. Sharps only.

    This is for violation messages, not for notation. Correct enharmonic
    spelling is a key-context problem and belongs in the engraver.
    """
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[midi % 12]}{midi // 12 - 1}"
