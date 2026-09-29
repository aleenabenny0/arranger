"""Product workflows: import, inspect, arrange, revise, export.

These are the use cases behind the API's project routes. They compose domain
modules and adapters and return plain data; they know nothing about HTTP,
databases or job queues, so a CLI or a worker can call them just the same.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..adapters.midi_writer import write_midi
from ..adapters.musicxml_writer import write_musicxml
from ..adapters.score_json import score_from_dict
from ..agent import RepairBudget, RunResult, arrange
from ..analysis import _track_priors, extract_melody
from ..engine import ALGORITHM_VERSION, Candidate, evaluate_plan
from ..explain import Difficulty, TransformationReport, estimate_difficulty, explain
from ..io import read_midi_bytes, sniff_midi
from ..ir import Score
from ..limits import DEFAULT_LIMITS, ImportLimits, MalformedFile, UnsupportedFormat
from ..notation import estimate_key
from ..plan import ArrangementPlan
from ..profile import PlayerProfile
from ..selection import SourceSelection, apply_selection
from ..verify import Severity, SolverStatus, Verdict, verify

AUDIO_MAGIC = (b"RIFF", b"fLaC", b"OggS", b"ID3", b"FORM", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


@dataclass
class ImportResult:
    score: Score
    kind: str                       # "midi" | "musicxml" | "json"
    warnings: list[str] = field(default_factory=list)


def sniff_kind(data: bytes, filename: str | None = None) -> str:
    """What this upload is, judged by its content. The filename is only a tiebreak."""
    from ..adapters.musicxml_reader import sniff_musicxml

    if sniff_midi(data):
        return "midi"
    if sniff_musicxml(data):
        return "musicxml"
    head = data[:12]
    if any(head.startswith(magic) for magic in AUDIO_MAGIC):
        return "audio"
    stripped = data[:64].lstrip()
    if stripped.startswith(b"{"):
        return "json"
    name = (filename or "").lower()
    if name.endswith((".mp3", ".wav", ".flac", ".ogg", ".aiff", ".aif")):
        return "audio"
    return "unknown"


def import_bytes(
    data: bytes, filename: str | None = None, limits: ImportLimits = DEFAULT_LIMITS
) -> ImportResult:
    """Parse an uploaded symbolic score. Audio is a separate, slower workflow."""
    from ..adapters.musicxml_reader import read_musicxml_bytes

    kind = sniff_kind(data, filename)
    if kind == "midi":
        score = read_midi_bytes(data, filename=filename, limits=limits)
    elif kind == "musicxml":
        score = read_musicxml_bytes(data, filename=filename, limits=limits)
    elif kind == "json":
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MalformedFile("This is not valid JSON.", detail=str(exc)) from None
        score = score_from_dict(payload, max_notes=limits.max_notes)
        score.source_format = score.source_format or "json"
    elif kind == "audio":
        raise UnsupportedFormat(
            "This is an audio recording. Upload it through audio transcription instead.",
            detail="audio sent to the symbolic importer",
        )
    else:
        raise UnsupportedFormat(
            "This file is not MIDI, MusicXML or an audio recording this service can read.",
            detail=f"unrecognised content, filename={filename!r}",
        )
    return ImportResult(score, kind, list(score.warnings))


# --- looking at a source before arranging it ----------------------------------


def findings_summary(verdict: Verdict) -> dict:
    """What a verdict may honestly claim, in words a musician can act on."""
    hard = verdict.hard
    unproven = [v for v in hard if str(v.certainty) == "unproven"]
    if not hard:
        status, headline = "passes", "Passes modeled constraints"
        detail = (
            "Every note is within this profile's reach, finger count and movement speed as "
            "this model understands them. It does not check fingering in detail or guarantee "
            "that the piece is comfortable."
        )
    elif verdict.solver_status == SolverStatus.UNKNOWN or len(unproven) == len(hard):
        status, headline = "unresolved", "Could not find a way to play some passages"
        detail = (
            "The search for a division between the hands was cut short, so these passages may "
            "still be playable. Treat them as places to look at, not as proof."
        )
    else:
        status, headline = "findings", f"{len(hard)} passage(s) exceed this profile's limits"
        detail = (
            "Within this model, no division of the notes between the hands avoids these. "
            "The model has no finger substitution and no thumb on two keys, so a skilled "
            "player may still manage some of them."
        )
    by_rule: dict[str, int] = {}
    for v in verdict.violations:
        key = f"{v.rule}:{v.severity}"
        by_rule[key] = by_rule.get(key, 0) + 1
    return {
        "status": status, "headline": headline, "detail": detail,
        "hard": len(hard), "strain": len(verdict.strain), "unproven": len(unproven),
        "solver_status": str(verdict.solver_status), "by_rule": by_rule,
    }


def verdict_to_dict(verdict: Verdict, limit: int = 400) -> dict:
    ordered = sorted(verdict.violations, key=lambda v: (v.severity != Severity.HARD, v.time))
    return {
        "playable": verdict.playable,
        "summary": findings_summary(verdict),
        "violations": [v.to_dict() for v in ordered[:limit]],
        "violations_truncated": max(0, len(ordered) - limit),
        "hands": verdict.hands,
    }


def inspect_source(score: Score, profile: PlayerProfile | None = None) -> dict:
    """Everything the source screen needs: parts, key, meter, the likely tune, how hard it is."""
    priors = _track_priors(score)
    counts: dict[int, int] = {}
    for n in score.notes:
        if n.track is not None:
            counts[n.track] = counts.get(n.track, 0) + 1
    melody = extract_melody(score)
    melody_tracks: dict[int, int] = {}
    ids = {n.id: n.track for n in score.notes if n.id}
    for n in melody:
        track = ids.get(n.source_id)
        if track is not None:
            melody_tracks[track] = melody_tracks.get(track, 0) + 1
    suggested = max(melody_tracks, key=melody_tracks.get) if melody_tracks else None

    timeline = score.timeline
    key = timeline.key_at(0.0) if timeline else None
    estimated_key = key is None
    fifths, mode = (key.fifths, key.mode) if key else estimate_key(score.notes)
    last_bar = score.last_bar()
    out = {
        "title": score.title,
        "composer": score.composer,
        "source_format": score.source_format,
        "note_count": len(score.notes),
        "bar_count": last_bar,
        "duration_seconds": round(score.duration(), 3),
        "tempo_bpm": round(score.tempo_bpm, 2),
        "tempo_changes": len(timeline.tempos) - 1 if timeline else 0,
        "meter": list(timeline.meter_at_bar(min(2, last_bar))) if timeline else None,
        "meter_changes": len(timeline.meters) - 1 if timeline else 0,
        "pickup_beats": timeline.pickup_beats if timeline else 0.0,
        "key": {"fifths": fifths, "mode": mode, "estimated": estimated_key},
        "pedal_marks": len(score.pedals),
        "tracks": [
            {
                "index": t.index, "name": t.name, "program": t.program, "is_percussion": t.is_percussion,
                "note_count": counts.get(t.index, 0), "lowest_pitch": t.lowest_pitch,
                "highest_pitch": t.highest_pitch,
                "melody_likelihood": round(priors.get(t.index, 0.0), 3) if priors else None,
                "melody_share": round(melody_tracks.get(t.index, 0) / max(1, len(melody)), 3),
            }
            for t in score.tracks
        ],
        "suggested_melody_track": suggested,
        "melody_note_count": len(melody),
        "melody_note_ids": [n.source_id for n in melody if n.source_id][:20_000],
        "warnings": list(score.warnings),
        "difficulty": estimate_difficulty(score).to_dict(),
        "confidence": _confidence(score),
    }
    if profile is not None:
        # Imported music: the staff is where the engraver put a note, not
        # which hand plays it, and most files carry no pedal data.
        literal = verify(score, profile, staff_is_binding=False, time_budget=2.0)
        pedalled = verify(score, profile, staff_is_binding=False, assume_pedal=True, time_budget=2.0)
        out["as_written"] = {
            "without_pedal": findings_summary(literal),
            "with_pedal_each_bar": findings_summary(pedalled),
            "note": "How the original sits under this profile's hands, before any arranging.",
        }
    return out


def _confidence(score: Score) -> dict | None:
    """Import confidence. Only transcribed audio has any; symbolic files are exact."""
    values = [n.confidence for n in score.notes if n.confidence is not None]
    if not values:
        return None
    low = [n.id for n in score.notes if n.confidence is not None and n.confidence < 0.5 and n.id]
    return {
        "mean": round(sum(values) / len(values), 3),
        "low_confidence_notes": len(low),
        "low_confidence_note_ids": low[:5_000],
        "note": "From the transcription model. It ranks notes by how sure the model was; it is "
                "not a probability that the note is right. Check the low ones by ear.",
    }


# --- arranging -------------------------------------------------------------------------


@dataclass
class ArrangementBundle:
    """One finished arrangement and everything needed to show, save and reproduce it."""

    plan: ArrangementPlan
    arranged: Score
    verdict: Verdict
    fidelity: dict
    report: TransformationReport
    difficulty: Difficulty
    origin: str
    accepted: bool
    stop_reason: str
    algorithm_version: str = ALGORITHM_VERSION
    model: str | None = None
    cost_usd: float = 0.0
    attempts: list[dict] = field(default_factory=list)
    selection_changes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "accepted": self.accepted,
            "origin": self.origin,
            "stop_reason": self.stop_reason,
            "algorithm_version": self.algorithm_version,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "findings": findings_summary(self.verdict),
            "fidelity": self.fidelity,
            "difficulty": self.difficulty.to_dict(),
            "tempo_scale": self.plan.tempo_scale,
            "tempo_bpm": round(self.arranged.tempo_bpm, 2),
        }


def _bundle(candidate: Candidate, source: Score, run: RunResult | None, changes: list[str]) -> ArrangementBundle:
    fidelity = asdict(candidate.fidelity)
    fidelity["score"] = round(candidate.fidelity.score(), 4)
    return ArrangementBundle(
        plan=candidate.plan,
        arranged=candidate.arranged,
        verdict=candidate.verdict,
        fidelity=fidelity,
        report=explain(candidate.plan, source, candidate.arranged, changes),
        difficulty=estimate_difficulty(candidate.arranged),
        origin=candidate.origin,
        accepted=candidate.accepted,
        stop_reason=run.stop_reason if run else "user_plan",
        model=run.model if run and candidate.origin == "model" else None,
        cost_usd=run.cost_usd if run else 0.0,
        attempts=[asdict(a) for a in run.attempts] if run else [],
        selection_changes=changes,
    )


def arrange_source(
    score: Score,
    profile: PlayerProfile,
    selection: SourceSelection | None = None,
    *,
    model=None,
    budget: RepairBudget | None = None,
    progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ArrangementBundle:
    """Arrange a source for a player. With `model=None` this is fully deterministic."""
    prepared, changes = apply_selection(score, selection)
    run = arrange(
        prepared, profile, model, verbose=False, budget=budget, progress=progress,
        should_cancel=should_cancel,
    )
    plan = ArrangementPlan.from_dict(run.best_plan)
    candidate = evaluate_plan(plan, prepared, profile, origin=run.best_origin or "deterministic")
    return _bundle(candidate, prepared, run, changes)


def revise_arrangement(
    score: Score, profile: PlayerProfile, plan: ArrangementPlan, selection: SourceSelection | None = None
) -> ArrangementBundle:
    """Evaluate a plan the user edited. Raises ValueError with the reasons if it does not fit."""
    prepared, changes = apply_selection(score, selection)
    if problems := plan.validate_for_source(prepared.last_bar()):
        raise ValueError("; ".join(problems))
    candidate = evaluate_plan(plan, prepared, profile, origin="user")
    return _bundle(candidate, prepared, None, changes)


# --- exports ------------------------------------------------------------------------------


def export_midi(score: Score) -> bytes:
    return write_midi(score)


def export_musicxml(score: Score) -> tuple[bytes, list[str]]:
    return write_musicxml(score)


def export_pdf(
    score: Score, *, paper: str = "a4", should_cancel: Callable[[], bool] | None = None,
    engraver=None,
) -> tuple[bytes, list[str]]:
    from ..adapters.lilypond import LilyPondEngraver

    return (engraver or LilyPondEngraver()).engrave_score(score, paper=paper, should_cancel=should_cancel)


def playback_events(score: Score, limit: int = 60_000) -> dict:
    """Compact note events for the browser player: [pitch, onset, duration, velocity, staff, id]."""
    notes = score.notes[:limit]
    return {
        "duration": round(score.duration(), 4),
        "tempo_bpm": round(score.tempo_bpm, 3),
        "truncated": len(score.notes) > limit,
        "notes": [
            [n.pitch, round(n.onset, 4), round(n.duration, 4), n.velocity or 72, n.staff or 0, n.id, n.bar or 0]
            for n in notes
        ],
        "pedals": [[round(p.start, 4), round(p.end, 4)] for p in score.pedals],
        "bars": _bar_times(score),
    }


def _bar_times(score: Score) -> list[list[float]]:
    timeline = score.timeline
    last = score.last_bar()
    if timeline is None:
        starts: dict[int, float] = {}
        for n in score.notes:
            if n.bar is not None:
                starts[n.bar] = min(starts.get(n.bar, n.onset), n.onset)
        return [[bar, round(t, 4)] for bar, t in sorted(starts.items())]
    return [[bar, round(timeline.bar_seconds(bar)[0], 4)] for bar in range(1, min(last, 4000) + 1)]
