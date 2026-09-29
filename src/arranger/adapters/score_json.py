"""JSON adapter for the internal Score IR.

Two jobs. It is the debugging and test interchange format, intentionally
simple so failures can be reproduced without a MIDI or MusicXML parser in the
loop. It is also how the API persists scores, so `score_from_dict` is a trust
boundary: it validates types and ranges and enforces size limits, because a
saved record or a request body can say anything.

The original minimal shape (title, tempo_bpm, notes with pitch/onset/duration)
still loads. Optional fields are omitted on output when unset, which keeps
small scores small.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..ir import Note, PedalSpan, Score, TrackInfo
from ..limits import MAX_BAR_NUMBER, MAX_SCORE_NOTES, LimitExceeded, MalformedFile
from ..timeline import Timeline, TimelineError

SCHEMA_VERSION = 2
_MAX_SECONDS = 6 * 60 * 60.0
_MAX_NOTE_SECONDS = 600.0
_ROLES = {"melody", "bass", "harmony", "accompaniment", "countermelody"}
_STEPS = {"A", "B", "C", "D", "E", "F", "G"}


def _bad(message: str) -> MalformedFile:
    return MalformedFile("The score data is not valid.", detail=message)


def _number(value, name: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < low or value > high:
        raise _bad(f"{name}={value} outside {low}..{high}")
    return value


def _integer(value, name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{name} must be an integer")
    if value < low or value > high:
        raise _bad(f"{name}={value} outside {low}..{high}")
    return value


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise _bad(f"{name} must be a string")
    return "".join(ch for ch in value if ch.isprintable())[:limit]


def note_to_dict(n: Note) -> dict:
    out: dict = {"pitch": n.pitch, "onset": n.onset, "duration": n.duration}
    if n.staff is not None:
        out["staff"] = n.staff
    if n.bar is not None:
        out["bar"] = n.bar
    if n.voice != 1:
        out["voice"] = n.voice
    for key in ("id", "velocity", "beat", "beats", "track", "source_id", "role",
                "dynamic", "confidence"):
        value = getattr(n, key)
        if value is not None:
            out[key] = value
    if n.spelling is not None:
        out["spelling"] = [n.spelling[0], n.spelling[1]]
    if n.articulations:
        out["articulations"] = list(n.articulations)
    if n.rolled:
        out["rolled"] = True
    return out


def note_from_dict(raw, index: int = 0) -> Note:
    if not isinstance(raw, dict):
        raise _bad(f"note {index} must be an object")
    where = f"note {index}"
    try:
        pitch = _integer(raw["pitch"], f"{where}.pitch", low=0, high=127)
        onset = _number(raw["onset"], f"{where}.onset", low=0.0, high=_MAX_SECONDS)
        duration = _number(raw["duration"], f"{where}.duration", low=1e-6, high=_MAX_NOTE_SECONDS)
    except KeyError as exc:
        raise _bad(f"{where} is missing {exc.args[0]}") from None

    staff = raw.get("staff")
    if staff is not None:
        staff = _integer(staff, f"{where}.staff", low=1, high=4)
    bar = raw.get("bar")
    if bar is not None:
        bar = _integer(bar, f"{where}.bar", low=1, high=MAX_BAR_NUMBER)
    voice = _integer(raw.get("voice", 1), f"{where}.voice", low=1, high=16)

    note_id = raw.get("id")
    if note_id is not None:
        note_id = _text(note_id, f"{where}.id", 64)
    source_id = raw.get("source_id")
    if source_id is not None:
        source_id = _text(source_id, f"{where}.source_id", 64)
    velocity = raw.get("velocity")
    if velocity is not None:
        velocity = _integer(velocity, f"{where}.velocity", low=1, high=127)
    beat = raw.get("beat")
    if beat is not None:
        beat = _number(beat, f"{where}.beat", low=0.0, high=1e7)
    beats = raw.get("beats")
    if beats is not None:
        beats = _number(beats, f"{where}.beats", low=1e-6, high=1e5)
    track = raw.get("track")
    if track is not None:
        track = _integer(track, f"{where}.track", low=0, high=1024)
    role = raw.get("role")
    if role is not None and role not in _ROLES:
        raise _bad(f"{where}.role must be one of {sorted(_ROLES)}")
    spelling = raw.get("spelling")
    if spelling is not None:
        if (
            not isinstance(spelling, (list, tuple)) or len(spelling) != 2
            or spelling[0] not in _STEPS
        ):
            raise _bad(f"{where}.spelling must be [step, alter]")
        spelling = (spelling[0], _integer(spelling[1], f"{where}.spelling", low=-2, high=2))
    articulations = raw.get("articulations", ())
    if not isinstance(articulations, (list, tuple)) or len(articulations) > 8:
        raise _bad(f"{where}.articulations must be a short list")
    articulations = tuple(_text(a, f"{where}.articulations", 24) for a in articulations)
    dynamic = raw.get("dynamic")
    if dynamic is not None:
        dynamic = _text(dynamic, f"{where}.dynamic", 8)
    confidence = raw.get("confidence")
    if confidence is not None:
        confidence = _number(confidence, f"{where}.confidence", low=0.0, high=1.0)
    rolled = raw.get("rolled", False)
    if not isinstance(rolled, bool):
        raise _bad(f"{where}.rolled must be a boolean")

    return Note(
        pitch=pitch, onset=onset, duration=duration, staff=staff, bar=bar, voice=voice,
        id=note_id, velocity=velocity, beat=beat, beats=beats, track=track,
        source_id=source_id, role=role, spelling=spelling, articulations=articulations,
        dynamic=dynamic, rolled=rolled, confidence=confidence,
    )


def score_to_dict(score: Score) -> dict:
    out: dict = {
        "schema_version": SCHEMA_VERSION,
        "title": score.title,
        "tempo_bpm": score.tempo_bpm,
        "notes": [note_to_dict(n) for n in score.notes],
    }
    if score.composer:
        out["composer"] = score.composer
    if score.source_format:
        out["source_format"] = score.source_format
    if score.timeline is not None:
        out["timeline"] = score.timeline.to_dict()
    if score.pedals:
        out["pedals"] = [[p.start, p.end] for p in score.pedals]
    if score.tracks:
        out["tracks"] = [
            {
                "index": t.index, "name": t.name, "program": t.program,
                "channel": t.channel, "is_percussion": t.is_percussion,
                "note_count": t.note_count, "lowest_pitch": t.lowest_pitch,
                "highest_pitch": t.highest_pitch,
            }
            for t in score.tracks
        ]
    if score.warnings:
        out["warnings"] = list(score.warnings)
    return out


def score_from_dict(
    data, *, default_title: str = "untitled", max_notes: int = MAX_SCORE_NOTES
) -> Score:
    """Build a Score from untrusted parsed JSON. Raises `ScoreImportError`s only."""
    if not isinstance(data, dict):
        raise _bad("score must be an object")
    raw_notes = data.get("notes")
    if not isinstance(raw_notes, list):
        raise _bad("score.notes must be a list")
    if len(raw_notes) > max_notes:
        raise LimitExceeded(
            "This score has more notes than this service will process.",
            detail=f"{len(raw_notes)} > {max_notes}",
        )
    notes = [note_from_dict(raw, i) for i, raw in enumerate(raw_notes)]

    timeline = None
    if data.get("timeline") is not None:
        try:
            timeline = Timeline.from_dict(data["timeline"])
        except TimelineError as exc:
            raise _bad(str(exc)) from None

    pedals = []
    raw_pedals = data.get("pedals", [])
    if not isinstance(raw_pedals, list) or len(raw_pedals) > 20_000:
        raise _bad("score.pedals must be a bounded list")
    for i, span in enumerate(raw_pedals):
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            raise _bad(f"pedal {i} must be [start, end]")
        start = _number(span[0], f"pedal {i}.start", low=0.0, high=_MAX_SECONDS)
        end = _number(span[1], f"pedal {i}.end", low=0.0, high=_MAX_SECONDS)
        if end > start:
            pedals.append(PedalSpan(start, end))

    tracks = []
    raw_tracks = data.get("tracks", [])
    if not isinstance(raw_tracks, list) or len(raw_tracks) > 1024:
        raise _bad("score.tracks must be a bounded list")
    for i, raw in enumerate(raw_tracks):
        if not isinstance(raw, dict):
            raise _bad(f"track {i} must be an object")

        def opt_int(key: str, low: int, high: int, raw=raw, i=i):
            value = raw.get(key)
            return None if value is None else _integer(value, f"track {i}.{key}", low=low, high=high)

        tracks.append(
            TrackInfo(
                index=_integer(raw.get("index", i), f"track {i}.index", low=0, high=1024),
                name=_text(raw.get("name", ""), f"track {i}.name", 200),
                program=opt_int("program", 0, 127),
                channel=opt_int("channel", 0, 15),
                is_percussion=bool(raw.get("is_percussion", False)),
                note_count=_integer(raw.get("note_count", 0), f"track {i}.note_count", low=0, high=10**7),
                lowest_pitch=opt_int("lowest_pitch", 0, 127),
                highest_pitch=opt_int("highest_pitch", 0, 127),
            )
        )

    raw_warnings = data.get("warnings", [])
    if not isinstance(raw_warnings, list) or len(raw_warnings) > 200:
        raise _bad("score.warnings must be a bounded list")

    source_format = data.get("source_format")
    if source_format is not None:
        source_format = _text(source_format, "source_format", 16)

    return Score(
        notes=notes,
        tempo_bpm=_number(data.get("tempo_bpm", 100.0), "tempo_bpm", low=5.0, high=1000.0),
        title=_text(data.get("title", default_title), "title", 200) or default_title,
        timeline=timeline,
        pedals=pedals,
        tracks=tracks,
        composer=_text(data.get("composer", ""), "composer", 200),
        source_format=source_format,
        warnings=[_text(w, "warning", 400) for w in raw_warnings],
    )


def load_score_json(path: str | Path) -> Score:
    """Load a Score from the project's JSON format."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedFile("This is not valid JSON.", detail=str(exc)) from None
    return score_from_dict(data, default_title=path.stem)


def dump_score_json(score: Score, path: str | Path) -> None:
    """Write a Score in the project's JSON format."""
    Path(path).write_text(json.dumps(score_to_dict(score), indent=2) + "\n", encoding="utf-8")
