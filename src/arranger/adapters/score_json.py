"""JSON adapter for the internal Score IR.

This is the debugging and test interchange format. It is intentionally simple
and stable so failures can be reproduced without a MIDI or MusicXML parser in
the loop.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..ir import Note, Score


def load_score_json(path: str | Path) -> Score:
    """Load a Score from the project's JSON debugging format."""
    path = Path(path)
    data = json.loads(path.read_text())
    notes = [
        Note(
            pitch=n["pitch"],
            onset=float(n["onset"]),
            duration=float(n["duration"]),
            staff=n.get("staff"),
            bar=n.get("bar"),
            voice=n.get("voice", 1),
        )
        for n in data["notes"]
    ]
    return Score(
        notes=notes,
        tempo_bpm=float(data.get("tempo_bpm", 100.0)),
        title=data.get("title", path.stem),
    )


def dump_score_json(score: Score, path: str | Path) -> None:
    """Write a Score in the project's JSON debugging format."""
    data = {
        "title": score.title,
        "tempo_bpm": score.tempo_bpm,
        "notes": [
            {
                "pitch": n.pitch,
                "onset": n.onset,
                "duration": n.duration,
                "staff": n.staff,
                "bar": n.bar,
                "voice": n.voice,
            }
            for n in score.notes
        ],
    }
    Path(path).write_text(json.dumps(data, indent=2) + "\n")
