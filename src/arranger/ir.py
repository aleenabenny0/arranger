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

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Literal

if TYPE_CHECKING:  # timeline is pure data too, but only needed for typing here
    from .timeline import Timeline

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

    Everything after `voice` is optional notation and provenance data. The
    verifier reads none of it except `id` (so a finding can point at the exact
    notes) and `rolled`. It exists so one representation can serve both
    clocks: seconds for the hands, beats for the page.

    id:        stable within a score; survives save/load and export.
    velocity:  MIDI 1-127. None means "unknown", not "silent".
    beat:      score position in quarter-note beats from the start.
    beats:     notated length in quarter-note beats.
    track:     index into Score.tracks for the part this note came from.
    source_id: id of the source note this one was derived from, if any. This
               is the provenance link that lets the UI say "this melody note is
               that original note, moved down an octave".
    role:      "melody" | "bass" | "harmony" | "accompaniment" | None.
    spelling:  (step, alter) such as ("B", -1). Octave follows from pitch.
               Only set when the source stated it or the key context decided
               it; never guessed at parse time.
    articulations: e.g. ("staccato", "accent").
    dynamic:   e.g. "mf", when the source marked one at this note.
    rolled:    part of an arpeggiated chord: played in sequence, not together.
    confidence: 0..1 for transcribed notes; None for symbolic sources.
    """

    pitch: int
    onset: float
    duration: float
    staff: int | None = None
    bar: int | None = None
    voice: int = 1
    id: str | None = None
    velocity: int | None = None
    beat: float | None = None
    beats: float | None = None
    track: int | None = None
    source_id: str | None = None
    role: str | None = None
    spelling: tuple[str, int] | None = None
    articulations: tuple[str, ...] = ()
    dynamic: str | None = None
    rolled: bool = False
    confidence: float | None = None

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


@dataclass(frozen=True, slots=True)
class PedalSpan:
    """Sustain pedal held from `start` to `end`, in performed seconds."""

    start: float
    end: float

    def covers(self, t: float, eps: float = 1e-6) -> bool:
        return self.start - eps <= t < self.end - eps


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """One part of the source, as imported. Lets the user say which is the tune."""

    index: int
    name: str = ""
    program: int | None = None      # General MIDI program, when known
    channel: int | None = None
    is_percussion: bool = False
    note_count: int = 0
    lowest_pitch: int | None = None
    highest_pitch: int | None = None


@dataclass
class Score:
    """A whole arrangement, flattened to note events."""

    notes: list[Note] = field(default_factory=list)
    tempo_bpm: float = 100.0
    title: str = "untitled"
    # Optional notation context. A Score without a timeline is still fully
    # verifiable; it just cannot be engraved without guessing the meter.
    timeline: "Timeline | None" = None
    pedals: list[PedalSpan] = field(default_factory=list)
    tracks: list[TrackInfo] = field(default_factory=list)
    composer: str = ""
    source_format: str | None = None   # "midi" | "musicxml" | "audio" | "json"
    # Things the importer could not represent faithfully. Shown to the user;
    # never silently dropped.
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.notes.sort(key=lambda n: (n.onset, n.pitch))
        self.pedals.sort(key=lambda p: p.start)
        self._index_key: tuple[int, int] | None = None
        self._onset_index: list[float] = []
        self._reach_index: list[float] = []

    def _ensure_index(self) -> None:
        """Build the sounding-note lookup, once per distinct note list.

        `sounding_at` used to scan every note for every query, which made the
        verifier quadratic: 14 seconds to plan a 2,000-note piece. The index
        is two parallel arrays over the onset-sorted notes: each note's onset,
        and the furthest offset reached by any note up to that position. A
        query walks backwards from the last note starting at or before `t`
        and stops the moment nothing earlier can still be sounding.
        """
        key = (id(self.notes), len(self.notes))
        if self._index_key == key:
            return
        self.notes.sort(key=lambda n: (n.onset, n.pitch))
        self._onset_index = [n.onset for n in self.notes]
        reach: list[float] = []
        furthest = float("-inf")
        for n in self.notes:
            furthest = max(furthest, n.offset)
            reach.append(furthest)
        self._reach_index = reach
        self._index_key = key

    @property
    def onsets(self) -> list[float]:
        """Distinct onset times, ascending.

        These are the only instants worth checking. A hand-span violation can
        only begin when some note begins, so sampling at onsets is exhaustive,
        not an approximation.
        """
        return sorted({n.onset for n in self.notes})

    def sounding_at(self, t: float) -> list[Note]:
        self._ensure_index()
        eps = 1e-6
        hi = bisect_right(self._onset_index, t + eps)
        out: list[Note] = []
        for i in range(hi - 1, -1, -1):
            if self._reach_index[i] - eps <= t:
                break  # nothing at or before i lasts until t
            note = self.notes[i]
            if note.sounds_at(t, eps):
                out.append(note)
        out.reverse()
        return out

    def pedal_down_at(self, t: float) -> bool:
        return any(p.covers(t) for p in self.pedals)

    def last_bar(self) -> int:
        return max((n.bar for n in self.notes if n.bar is not None), default=1)

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
