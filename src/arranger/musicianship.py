"""Musician-style analysis used by planning, repair, and explanations."""

from __future__ import annotations

from dataclasses import dataclass, asdict

from .ir import Note, Score
from .profile import PlayerProfile
from .render import TEMPLATES, detect_chords, extract_melody


@dataclass(frozen=True)
class NoteImportance:
    note_index: int
    bar: int | None
    pitch: int
    onset: float
    layer: str
    score: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ArrangingGuidance:
    title: str
    summary: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


GUIDANCE_LIBRARY: tuple[ArrangingGuidance, ...] = (
    ArrangingGuidance(
        "Protect the melody first",
        "Keep the singable top line clear before adding harmony, bass, or texture.",
        ("melody", "priority", "clarity"),
    ),
    ArrangingGuidance(
        "Keep bass and chord identity",
        "Preserve bass roots and chord thirds before fifths, doublings, and color tones.",
        ("bass", "harmony", "reduction"),
    ),
    ArrangingGuidance(
        "Thin dense inner voices",
        "When a texture is crowded, remove doubled notes, fifths, and buried inner parts first.",
        ("texture", "density", "playability"),
    ),
    ArrangingGuidance(
        "Avoid muddy low chords",
        "Low-register harmony should usually be roots, shells, or open spacing, not thick blocks.",
        ("bass", "register", "voicing"),
    ),
    ArrangingGuidance(
        "Match texture to section energy",
        "Use lighter textures for lower-energy sections and save fuller patterns for peaks.",
        ("section", "energy", "texture"),
    ),
    ArrangingGuidance(
        "Repair by changing roles",
        "If a passage remains unplayable, switch accompaniment role before deleting melody.",
        ("repair", "playability", "roles"),
    ),
)


def _chord_role(note: Note, chord: tuple[int, str] | None) -> tuple[str, float, str]:
    if chord is None:
        return "inner_voice", 0.35, "non-chord or passing material"
    root, quality = chord
    pc = note.pitch % 12
    template = TEMPLATES[quality]
    third = (root + template[1]) % 12 if len(template) > 1 else root
    fifth = (root + 7) % 12
    seventh = (root + template[3]) % 12 if len(template) > 3 else None
    if pc == root:
        return "bass" if note.pitch < 60 else "harmony", 0.86, "chord root"
    if pc == third:
        return "harmony", 0.78, "chord third defines quality"
    if seventh is not None and pc == seventh:
        return "harmony", 0.72, "chord seventh carries tension"
    if pc == fifth:
        return "doubling", 0.42, "fifth is usually optional"
    return "color", 0.50, "color or passing tone"


def analyze_note_importance(source: Score) -> list[NoteImportance]:
    """Rank source notes by how strongly a human reduction would protect them."""
    melody = extract_melody(source)
    chords = detect_chords(source, melody)
    melody_keys = {(round(note.onset, 3), note.pitch, note.bar) for note in melody}
    lowest_by_bar: dict[int, int] = {}
    for note in source.notes:
        if note.bar is not None:
            lowest_by_bar[note.bar] = min(note.pitch, lowest_by_bar.get(note.bar, note.pitch))

    importances: list[NoteImportance] = []
    for index, note in enumerate(source.notes):
        reasons: list[str] = []
        if (round(note.onset, 3), note.pitch, note.bar) in melody_keys:
            layer = "melody"
            score = 1.0
            reasons.append("main melody")
        elif note.bar is not None and note.pitch == lowest_by_bar.get(note.bar):
            layer = "bass"
            score = 0.9
            reasons.append("lowest bass anchor")
        else:
            layer, score, reason = _chord_role(note, chords.get(note.bar or 0))
            reasons.append(reason)

        same_pitch_class = [
            other
            for other in source.notes
            if other is not note
            and other.bar == note.bar
            and other.pitch % 12 == note.pitch % 12
        ]
        if same_pitch_class and layer not in {"melody", "bass"}:
            score -= 0.18
            reasons.append("doubled pitch class")
            if layer == "harmony":
                layer = "doubling"
        if note.pitch < 48 and layer not in {"melody", "bass"}:
            score -= 0.12
            reasons.append("low-register crowding")

        importances.append(
            NoteImportance(
                note_index=index,
                bar=note.bar,
                pitch=note.pitch,
                onset=note.onset,
                layer=layer,
                score=round(max(0.0, min(score, 1.0)), 2),
                reasons=tuple(reasons),
            )
        )
    return importances


def reduction_priority(source: Score) -> list[NoteImportance]:
    """Return least-essential notes first, which is the order to thin texture."""
    return sorted(
        analyze_note_importance(source),
        key=lambda item: (item.score, item.layer != "doubling", item.bar or 0, item.onset),
    )


def guidance_for_context(query: str, limit: int = 4) -> list[ArrangingGuidance]:
    """Tiny local retrieval layer for arranging guidance without external services."""
    words = {word.strip(".,:;!?()").lower() for word in query.split()}
    ranked = sorted(
        GUIDANCE_LIBRARY,
        key=lambda item: (
            -len(words & set(item.tags)),
            item.title,
        ),
    )
    return ranked[:limit]


def summarize_importance(source: Score, limit: int = 8) -> str:
    removable = reduction_priority(source)[:limit]
    lines = ["Lowest-priority notes to thin first:"]
    for note in removable:
        reasons = ", ".join(note.reasons)
        where = f"bar {note.bar}" if note.bar is not None else f"t={note.onset:.1f}s"
        lines.append(
            f"  {where}: pitch {note.pitch}, {note.layer}, "
            f"importance {note.score:.2f} ({reasons})"
        )
    return "\n".join(lines)


def profile_fit_weight(profile: PlayerProfile) -> float:
    """A simple scalar ML features can use to represent player capacity."""
    return round(
        (
            profile.skill_level / 10
            + profile.max_span / 18
            + min(profile.max_leap_rate, 160) / 160
        )
        / 3,
        3,
    )
