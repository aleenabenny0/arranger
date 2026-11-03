"""What the user says about the source before it is arranged.

Importers guess: which part is the tune, what the tempo of a recording is,
whether that blip at 0:42 is a note. The user can hear the answer. A
`SourceSelection` is their corrections, kept separate from the imported score
so the import is never overwritten and every correction can be undone.

Dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .ir import Note, Score
from .limits import MAX_BAR_NUMBER
from .timeline import Timeline

MAX_EDITS = 5_000
_EDITABLE = {"pitch", "onset", "duration", "staff", "velocity"}


class SelectionError(ValueError):
    """The selection does not make sense for this score."""


@dataclass(frozen=True)
class NoteEdit:
    """One correction to one note. `op` is "delete", "update" or "add"."""

    op: str
    note_id: str | None = None
    pitch: int | None = None
    onset: float | None = None
    duration: float | None = None
    staff: int | None = None
    velocity: int | None = None


@dataclass(frozen=True)
class SourceSelection:
    # Index into Score.tracks of the part that carries the tune. None: let the
    # melody finder decide.
    melody_track: int | None = None
    # Parts to leave out entirely (a click track, a doubled pad).
    ignored_tracks: tuple[int, ...] = ()
    # Individual notes marked as melody, for a tune that moves between parts.
    melody_note_ids: tuple[str, ...] = ()
    # Whole-piece transposition in semitones, e.g. into an easier key.
    transpose: int = 0
    # For recordings: the tempo the user hears, when the estimate was wrong or
    # absent. Re-bars the piece; does not move any note in time.
    tempo_bpm: float | None = None
    meter: tuple[int, int] | None = None
    edits: tuple[NoteEdit, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "melody_track": self.melody_track,
            "ignored_tracks": list(self.ignored_tracks),
            "melody_note_ids": list(self.melody_note_ids),
            "transpose": self.transpose,
            "tempo_bpm": self.tempo_bpm,
            "meter": list(self.meter) if self.meter else None,
            "edits": [
                {k: v for k, v in vars(edit).items() if v is not None} for edit in self.edits
            ],
        }

    @classmethod
    def from_dict(cls, data) -> "SourceSelection":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise SelectionError("selection must be an object")
        known = {"melody_track", "ignored_tracks", "melody_note_ids", "transpose", "tempo_bpm",
                 "meter", "edits"}
        if unknown := set(data) - known:
            raise SelectionError(f"unknown selection keys: {sorted(unknown)}")

        def integer(value, name, low, high):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise SelectionError(f"{name} must be a whole number from {low} to {high}")
            return value

        def number(value, name, low, high):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise SelectionError(f"{name} must be a number from {low} to {high}")
            return float(value)

        melody_track = data.get("melody_track")
        if melody_track is not None:
            melody_track = integer(melody_track, "melody_track", 0, 1024)
        ignored = data.get("ignored_tracks") or []
        if not isinstance(ignored, list) or len(ignored) > 1024:
            raise SelectionError("ignored_tracks must be a list")
        ignored = tuple(sorted({integer(t, "ignored_tracks", 0, 1024) for t in ignored}))
        ids = data.get("melody_note_ids") or []
        if not isinstance(ids, list) or len(ids) > 100_000 or not all(isinstance(i, str) and len(i) <= 64 for i in ids):
            raise SelectionError("melody_note_ids must be a list of note ids")
        tempo = data.get("tempo_bpm")
        if tempo is not None:
            tempo = number(tempo, "tempo_bpm", 20, 400)
        meter = data.get("meter")
        if meter is not None:
            if not isinstance(meter, (list, tuple)) or len(meter) != 2:
                raise SelectionError("meter must be [numerator, denominator]")
            meter = (integer(meter[0], "meter numerator", 1, 32), integer(meter[1], "meter denominator", 1, 32))
            if meter[1] not in (1, 2, 4, 8, 16, 32):
                raise SelectionError("meter denominator must be a power of two")
        raw_edits = data.get("edits") or []
        if not isinstance(raw_edits, list) or len(raw_edits) > MAX_EDITS:
            raise SelectionError(f"at most {MAX_EDITS} edits are allowed")
        edits = []
        for i, raw in enumerate(raw_edits):
            if not isinstance(raw, dict) or raw.get("op") not in ("delete", "update", "add"):
                raise SelectionError(f"edit {i}: op must be delete, update or add")
            if unknown := set(raw) - {"op", "note_id", *_EDITABLE}:
                raise SelectionError(f"edit {i}: unknown keys {sorted(unknown)}")
            note_id = raw.get("note_id")
            if raw["op"] != "add" and (not isinstance(note_id, str) or not 0 < len(note_id) <= 64):
                raise SelectionError(f"edit {i}: note_id is required")
            edits.append(
                NoteEdit(
                    op=raw["op"], note_id=note_id,
                    pitch=None if raw.get("pitch") is None else integer(raw["pitch"], f"edit {i} pitch", 0, 127),
                    onset=None if raw.get("onset") is None else number(raw["onset"], f"edit {i} onset", 0, 21_600),
                    duration=None if raw.get("duration") is None else number(raw["duration"], f"edit {i} duration", 0.01, 600),
                    staff=None if raw.get("staff") is None else integer(raw["staff"], f"edit {i} staff", 1, 2),
                    velocity=None if raw.get("velocity") is None else integer(raw["velocity"], f"edit {i} velocity", 1, 127),
                )
            )
        return cls(
            melody_track=melody_track, ignored_tracks=ignored, melody_note_ids=tuple(ids),
            transpose=integer(data.get("transpose", 0), "transpose", -24, 24),
            tempo_bpm=tempo, meter=meter, edits=tuple(edits),
        )


def apply_selection(score: Score, selection: SourceSelection | None) -> tuple[Score, list[str]]:
    """The score as the user corrected it, plus notes on what was changed.

    The input is never mutated. Every note keeps its id, so findings and
    highlights still point at the right place after corrections.
    """
    if selection is None or selection == SourceSelection():
        return score, []
    changes: list[str] = []
    track_indexes = {t.index for t in score.tracks}
    if selection.melody_track is not None and track_indexes and selection.melody_track not in track_indexes:
        raise SelectionError(f"there is no track {selection.melody_track} in this piece")
    unknown_tracks = [t for t in selection.ignored_tracks if track_indexes and t not in track_indexes]
    if unknown_tracks:
        raise SelectionError(f"there is no track {unknown_tracks[0]} in this piece")

    notes = list(score.notes)

    # --- note-level corrections -------------------------------------------
    if selection.edits:
        by_id = {n.id: i for i, n in enumerate(notes) if n.id}
        deleted: set[int] = set()
        added = updated = 0
        for edit in selection.edits:
            if edit.op == "add":
                if edit.pitch is None or edit.onset is None or edit.duration is None:
                    raise SelectionError("an added note needs pitch, onset and duration")
                notes.append(Note(pitch=edit.pitch, onset=edit.onset, duration=edit.duration,
                                  staff=edit.staff, velocity=edit.velocity, id=f"u{added}"))
                added += 1
                continue
            index = by_id.get(edit.note_id)
            if index is None:
                raise SelectionError(f"there is no note '{edit.note_id}' in this piece")
            if edit.op == "delete":
                deleted.add(index)
                continue
            fields = {k: getattr(edit, k) for k in _EDITABLE if getattr(edit, k) is not None}
            moved = "onset" in fields or "duration" in fields
            notes[index] = replace(notes[index], **fields, **({"beat": None, "beats": None} if moved else {}))
            updated += 1
        notes = [n for i, n in enumerate(notes) if i not in deleted]
        parts = [f"{len(deleted)} removed" if deleted else "", f"{updated} changed" if updated else "",
                 f"{added} added" if added else ""]
        changes.append("Notes corrected by hand: " + ", ".join(p for p in parts if p) + ".")

    # --- parts ---------------------------------------------------------------
    if selection.ignored_tracks:
        before = len(notes)
        ignored = set(selection.ignored_tracks)
        notes = [n for n in notes if n.track not in ignored]
        names = [t.name or f"track {t.index + 1}" for t in score.tracks if t.index in ignored]
        changes.append(f"Left out {', '.join(names) or 'selected parts'} ({before - len(notes)} notes).")
    if not notes:
        raise SelectionError("these choices leave no notes to arrange")

    # --- timing for recordings ---------------------------------------------------
    timeline = score.timeline
    tempo_bpm = score.tempo_bpm
    if selection.tempo_bpm is not None or selection.meter is not None:
        bpm = selection.tempo_bpm or score.tempo_bpm or 120.0
        num, den = selection.meter or (timeline.meter_at_bar(1) if timeline else (4, 4))
        timeline = Timeline.constant(bpm, num, den)
        tempo_bpm = bpm
        changes.append(f"Barred at {bpm:.0f} bpm in {num}/{den} as set by hand.")
        rebarred = []
        for n in notes:
            beat = timeline.beat_at(n.onset)
            bar = min(timeline.bar_at(beat), MAX_BAR_NUMBER)
            rebarred.append(replace(n, beat=beat, beats=max(timeline.beat_at(n.offset) - beat, 1e-3), bar=bar))
        notes = rebarred
    elif timeline is not None:
        # Hand-placed notes need a bar and a beat like everything else.
        notes = [
            n if n.beat is not None and n.bar is not None else replace(
                n, beat=timeline.beat_at(n.onset),
                beats=max(timeline.beat_at(n.offset) - timeline.beat_at(n.onset), 1e-3),
                bar=min(timeline.bar_at(timeline.beat_at(n.onset)), MAX_BAR_NUMBER),
            )
            for n in notes
        ]

    # --- key --------------------------------------------------------------------------
    if selection.transpose:
        shift = selection.transpose
        if any(not 0 <= n.pitch + shift <= 127 for n in notes):
            raise SelectionError("that transposition moves notes off the keyboard")
        notes = [replace(n, pitch=n.pitch + shift, spelling=None if shift % 12 else n.spelling) for n in notes]
        changes.append(f"Transposed {'up' if shift > 0 else 'down'} {abs(shift)} semitone(s).")

    # --- melody -----------------------------------------------------------------------------
    marked = set(selection.melody_note_ids)
    if selection.melody_track is not None or marked:
        flagged = 0
        out = []
        for n in notes:
            is_melody = (selection.melody_track is not None and n.track == selection.melody_track) or (
                n.id in marked
            )
            flagged += is_melody
            out.append(replace(n, role="melody" if is_melody else (None if n.role == "melody" else n.role)))
        if not flagged:
            raise SelectionError("the chosen melody has no notes")
        notes = out
        if selection.melody_track is not None:
            name = next((t.name for t in score.tracks if t.index == selection.melody_track), "") or (
                f"track {selection.melody_track + 1}"
            )
            changes.append(f"Melody taken from {name}.")
        else:
            changes.append(f"Melody set by hand ({flagged} notes).")

    return (
        Score(
            notes=notes, tempo_bpm=tempo_bpm, title=score.title, timeline=timeline,
            pedals=list(score.pedals), tracks=list(score.tracks), composer=score.composer,
            source_format=score.source_format, warnings=list(score.warnings),
        ),
        changes,
    )
