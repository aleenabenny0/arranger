"""Product workflows: corrections to the source, arranging, explaining, exporting."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.adapters.musicxml_writer import write_mxl, write_musicxml  # noqa: E402
from arranger.adapters.score_json import score_to_dict  # noqa: E402
from arranger.application import workflows as wf  # noqa: E402
from arranger.explain import estimate_difficulty, explain  # noqa: E402
from arranger.io import read_midi_bytes  # noqa: E402
from arranger.ir import Note, Score, TrackInfo  # noqa: E402
from arranger.limits import ImportLimits, LimitExceeded, MalformedFile, UnsupportedFormat  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section  # noqa: E402
from arranger.profile import PRESETS  # noqa: E402
from arranger.render import render  # noqa: E402
from arranger.selection import SelectionError, SourceSelection, apply_selection  # noqa: E402
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402

PROFILE = PRESETS["intermediate"]


def piece(bars: int = 8, *, with_pad: bool = False) -> Score:
    """A tune (track 0) over broken triads (track 1), optionally under a high pad (track 2)."""
    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 4, 4)], [KeyChange(0, 0)])
    notes = []
    roots = [48, 53, 55, 48]
    for bar in range(bars):
        root = roots[bar % 4]
        for k in range(4):
            beat = bar * 4 + k
            notes.append(Note(72 + (k + bar) % 5, beat * 0.5, 0.5, bar=bar + 1, beat=float(beat), beats=1.0,
                              track=0, velocity=96, id=f"m{beat}"))
            notes.append(Note(root + (0, 7, 4, 7)[k], beat * 0.5, 0.5, bar=bar + 1, beat=float(beat),
                              beats=1.0, track=1, velocity=60, id=f"a{beat}"))
        if with_pad:
            notes.append(Note(91, bar * 2.0, 2.0, bar=bar + 1, beat=bar * 4.0, beats=4.0, track=2, id=f"p{bar}"))
    tracks = [TrackInfo(0, "Lead vocal"), TrackInfo(1, "Piano left"), TrackInfo(2, "Strings pad")]
    return Score(notes=notes, tempo_bpm=120, title="Fixture", timeline=timeline,
                 tracks=tracks[: 3 if with_pad else 2], source_format="midi")


# --- import ---------------------------------------------------------------------


def test_import_recognises_files_by_content_not_by_name():
    midi = write_midi(piece())
    xml, _ = write_musicxml(piece())
    mxl, _ = write_mxl(piece())
    as_json = json.dumps(score_to_dict(piece())).encode()
    assert wf.import_bytes(midi, "lies.pdf").kind == "midi"
    assert wf.import_bytes(xml, "lies.mid").kind == "musicxml"
    assert wf.import_bytes(mxl, "no-extension").kind == "musicxml"
    assert wf.import_bytes(as_json, None).kind == "json"
    assert len(wf.import_bytes(midi, "x.mid").score.notes) == len(piece().notes)


@pytest.mark.parametrize(
    "data, filename, error",
    [(b"RIFF\x00\x00\x00\x00WAVEfmt ", "song.wav", UnsupportedFormat),
     (b"%PDF-1.7 not music", "score.pdf", UnsupportedFormat),
     (b"", "empty.mid", UnsupportedFormat),
     (b"{ not json", "score.json", MalformedFile),
     (b'{"notes": "nope"}', "score.json", MalformedFile)],
    ids=["audio", "pdf", "empty", "bad-json", "wrong-shape"],
)
def test_import_refuses_what_it_cannot_read(data, filename, error):
    with pytest.raises(error) as info:
        wf.import_bytes(data, filename)
    assert info.value.public and "Traceback" not in info.value.public


def test_import_limits_apply_to_every_format():
    midi = write_midi(piece())
    with pytest.raises(LimitExceeded):
        wf.import_bytes(midi, "x.mid", ImportLimits(max_notes=10))
    with pytest.raises(LimitExceeded):
        wf.import_bytes(json.dumps(score_to_dict(piece())).encode(), None, ImportLimits(max_notes=10))


# --- inspection --------------------------------------------------------------------


def test_inspection_reports_parts_key_meter_and_the_likely_tune():
    info = wf.inspect_source(piece(with_pad=True), PROFILE)
    assert info["bar_count"] == 8 and info["meter"] == [4, 4] and info["key"]["estimated"] is False
    assert [t["name"] for t in info["tracks"]] == ["Lead vocal", "Piano left", "Strings pad"]
    assert info["suggested_melody_track"] == 0, "the vocal, not the pad above it"
    assert info["tracks"][0]["melody_share"] == 1.0
    assert info["confidence"] is None, "symbolic files have no transcription confidence"
    assert set(info["as_written"]) == {"without_pedal", "with_pedal_each_bar", "note"}
    assert 1 <= info["difficulty"]["level"] <= 10


def test_transcribed_notes_carry_confidence_into_the_inspection():
    score = piece()
    score.notes[0] = Note(60, 0.0, 0.5, bar=1, id="low", confidence=0.2)
    score.notes[1] = Note(62, 0.0, 0.5, bar=1, id="high", confidence=0.9)
    confidence = wf.inspect_source(score)["confidence"]
    assert confidence["low_confidence_note_ids"] == ["low"] and "not a probability" in confidence["note"]


# --- corrections ----------------------------------------------------------------------


def test_an_empty_selection_changes_nothing():
    score = piece()
    same, changes = apply_selection(score, SourceSelection())
    assert same is score and changes == []


def test_choosing_the_melody_track_overrules_the_heuristic():
    score = piece(with_pad=True)
    chosen, changes = apply_selection(score, SourceSelection(melody_track=2))
    assert {n.track for n in chosen.notes if n.role == "melody"} == {2}
    assert changes == ["Melody taken from Strings pad."]
    arranged = render(ArrangementPlan(sections=[Section(1, 8)]), chosen)
    assert {n.pitch for n in arranged.notes if n.staff == 1} == {91}


def test_ignoring_a_track_removes_it_and_says_how_much():
    chosen, changes = apply_selection(piece(with_pad=True), SourceSelection(ignored_tracks=(2,)))
    assert not [n for n in chosen.notes if n.track == 2]
    assert changes == ["Left out Strings pad (8 notes)."]


def test_transposing_keeps_ids_and_drops_stale_spelling():
    score = piece()
    score.notes[0] = Note(61, 0.0, 0.5, bar=1, id="x", track=0, spelling=("D", -1), beat=0.0, beats=1.0)
    up, changes = apply_selection(score, SourceSelection(transpose=2))
    moved = next(n for n in up.notes if n.id == "x")
    assert moved.pitch == 63 and moved.spelling is None and "Transposed up 2" in changes[0]
    octave, _ = apply_selection(score, SourceSelection(transpose=12))
    assert next(n for n in octave.notes if n.id == "x").spelling == ("D", -1)


def test_note_edits_delete_update_and_add():
    raw = {"edits": [{"op": "delete", "note_id": "m0"}, {"op": "update", "note_id": "m1", "pitch": 80},
                     {"op": "add", "pitch": 84, "onset": 1.0, "duration": 0.5, "staff": 1}]}
    edited, changes = apply_selection(piece(), SourceSelection.from_dict(raw))
    ids = {n.id: n for n in edited.notes}
    assert "m0" not in ids and ids["m1"].pitch == 80 and ids["u0"].pitch == 84
    assert ids["u0"].bar == 1 and ids["u0"].beat == 2.0, "a hand-placed note still gets its bar and beat"
    assert changes == ["Notes corrected by hand: 1 removed, 1 changed, 1 added."]


def test_setting_the_tempo_rebars_without_moving_a_note():
    score = piece()
    score.timeline = None
    fixed, changes = apply_selection(score, SourceSelection(tempo_bpm=60, meter=(3, 4)))
    assert [n.onset for n in fixed.notes] == [n.onset for n in score.notes]
    assert fixed.timeline.meter_at_bar(1) == (3, 4) and fixed.tempo_bpm == 60
    # 16 seconds at 60 bpm is 16 beats; the last note starts on beat 15.5, in bar 6 of 3/4.
    assert max(n.bar for n in fixed.notes) == 6
    assert "60 bpm in 3/4" in changes[0]


@pytest.mark.parametrize(
    "raw",
    [{"melody_track": 9}, {"ignored_tracks": [0, 1]}, {"transpose": 99}, {"tempo_bpm": 5},
     {"meter": [4, 3]}, {"edits": [{"op": "explode"}]}, {"edits": [{"op": "delete", "note_id": "zzz"}]},
     {"edits": [{"op": "add", "pitch": 60}]}, {"surprise": 1}, {"transpose": 60}, "not an object",
     {"melody_note_ids": ["zzz"]}, {"edits": [{"op": "update", "note_id": "m0", "pitch": 999}]}],
)
def test_nonsense_selections_are_refused_with_a_reason(raw):
    with pytest.raises(SelectionError) as info:
        apply_selection(piece(), SourceSelection.from_dict(raw))
    assert str(info.value)


def test_a_selection_survives_json():
    raw = {"melody_track": 0, "ignored_tracks": [1], "transpose": -2, "tempo_bpm": 90.0, "meter": [6, 8],
           "melody_note_ids": ["m1"], "edits": [{"op": "update", "note_id": "m1", "pitch": 70}]}
    selection = SourceSelection.from_dict(raw)
    assert SourceSelection.from_dict(json.loads(json.dumps(selection.to_dict()))) == selection


# --- arranging ----------------------------------------------------------------------------


def test_arranging_without_a_model_gives_a_complete_bundle():
    stages: list[tuple[float, str]] = []
    bundle = wf.arrange_source(piece(), PROFILE, progress=lambda f, s: stages.append((f, s)))
    assert bundle.accepted and bundle.origin in ("deterministic", "local_repair")
    assert bundle.model is None and bundle.cost_usd == 0 and bundle.algorithm_version
    summary = bundle.summary()
    assert summary["findings"]["headline"] == "Passes modeled constraints"
    assert summary["fidelity"]["score"] >= 0.88 and "rhythm" in summary["fidelity"]
    assert stages and stages[-1][0] == 1.0
    assert all(n.id for n in bundle.arranged.notes)


def test_the_selection_shapes_the_arrangement_and_is_reported():
    bundle = wf.arrange_source(piece(with_pad=True), PROFILE, SourceSelection(ignored_tracks=(2,), transpose=-2))
    assert bundle.selection_changes[0].startswith("Left out Strings pad")
    assert bundle.report.sentences[:2] == bundle.selection_changes
    assert max(n.pitch for n in bundle.arranged.notes) < 91


def test_a_user_edited_plan_is_evaluated_not_trusted():
    good = ArrangementPlan(sections=[Section(1, 8, LHPattern.ALBERTI)])
    bundle = wf.revise_arrangement(piece(), PROFILE, good)
    assert bundle.origin == "user" and bundle.stop_reason == "user_plan"
    assert bundle.report.patterns == {"an Alberti figure": 8}
    with pytest.raises(ValueError, match="not covered"):
        wf.revise_arrangement(piece(), PROFILE, ArrangementPlan(sections=[Section(1, 3)]))


def test_findings_language_never_overclaims():
    clean = wf.findings_summary(wf.arrange_source(piece(), PROFILE).verdict)
    assert clean["status"] == "passes" and "guarantee" in clean["detail"]
    from arranger.verify import verify

    forced = verify(Score.from_tuples([(36, 0.0, 1.0, 2), (72, 0.0, 1.0, 2)]), PROFILE)
    proven = wf.findings_summary(forced)
    assert proven["status"] == "findings" and "Within this model" in proven["detail"]
    for v in forced.violations:
        v.certainty = type(v.certainty)("unproven")
    unproven = wf.findings_summary(forced)
    assert unproven["status"] == "unresolved" and "not as proof" in unproven["detail"]


# --- explanation and difficulty ---------------------------------------------------------------


def test_the_explanation_counts_what_really_happened():
    source = piece()
    plan = ArrangementPlan(sections=[Section(1, 4, LHPattern.PEDAL_TONE, lh_voices=1), Section(5, 8, lh_voices=0)],
                           reductions=[], pedal_bars=[1], tempo_scale=0.7)
    arranged = render(plan, source)
    report = explain(plan, source, arranged)
    text = " ".join(report.sentences)
    assert report.melody_notes == report.melody_kept == 32
    assert report.dropped["inner chord tones"] > 0
    assert "a single held bass note (4 bars)" in text and "1 section(s) are melody only" in text
    assert "70% of the original (84 instead of 120" in text
    assert report.pedal_bars == 1 and report.bass_bars_lost == 4


def test_the_explanation_ignores_what_the_plan_claims():
    from arranger.plan import Reduction, ReductionKind

    source = piece()
    honest = ArrangementPlan(sections=[Section(1, 8)])
    lying = ArrangementPlan(sections=[Section(1, 8)], reductions=[
        Reduction(ReductionKind.COUNTERMELODY, 1, 8, "I removed a countermelody that never existed")])
    assert explain(honest, source, render(honest, source)).sentences == \
        explain(lying, source, render(lying, source)).sentences


def test_octave_moves_are_reported_with_their_bars():
    source = piece()
    plan = ArrangementPlan(sections=[Section(1, 8, melody_shift=-12)])
    report = explain(plan, source, render(plan, source))
    assert report.melody_moved_by_octave == 32 and "8 bars (1 to 8)" in " ".join(report.sentences)


def test_difficulty_ranks_pieces_sensibly_and_is_not_a_finding():
    easy = Score.from_tuples([(60 + i % 5, i * 1.0, 1.0, 1) for i in range(16)])
    busy = Score.from_tuples(
        [(60 + (i * 7) % 24, i * 0.1, 0.1, 1) for i in range(200)]
        + [(36 + (i * 5) % 12, i * 0.15, 0.15, 2) for i in range(130)]
        + [(48 + j * 4, i * 0.45, 0.4, 2) for i in range(40) for j in range(4)]
    )
    a, b = estimate_difficulty(easy), estimate_difficulty(busy)
    assert a.level < 3 and a.label == "beginner"
    assert b.level > a.level + 3
    assert "not a graded-exam level" in b.note
    assert estimate_difficulty(Score(notes=[])).level == 1.0


# --- exports -------------------------------------------------------------------------------------


def test_exports_are_real_files_with_the_arrangement_in_them():
    bundle = wf.arrange_source(piece(), PROFILE)
    midi = wf.export_midi(bundle.arranged)
    back = read_midi_bytes(midi)
    assert sorted((n.pitch, round(n.beat, 3)) for n in back.notes) == \
        sorted((n.pitch, round(n.beat, 3)) for n in bundle.arranged.notes)
    xml, _ = wf.export_musicxml(bundle.arranged)
    again = wf.import_bytes(xml, "again.musicxml").score
    assert sorted(n.pitch for n in again.notes) == sorted(n.pitch for n in bundle.arranged.notes)
    assert {n.staff for n in again.notes} == {1, 2}


def test_playback_events_are_compact_and_complete():
    bundle = wf.arrange_source(piece(), PROFILE)
    events = wf.playback_events(bundle.arranged)
    assert len(events["notes"]) == len(bundle.arranged.notes) and not events["truncated"]
    pitch, onset, duration, velocity, staff, note_id, bar = events["notes"][0]
    assert 0 <= pitch <= 127 and duration > 0 and staff in (1, 2) and note_id and bar == 1
    assert events["bars"][0] == [1, 0.0] and len(events["bars"]) == 8
    assert wf.playback_events(bundle.arranged, limit=5)["truncated"]


def test_a_slowed_arrangement_exports_at_the_slower_tempo():
    plan = ArrangementPlan(sections=[Section(1, 8)], tempo_scale=0.5)
    bundle = wf.revise_arrangement(piece(), PROFILE, plan)
    assert bundle.arranged.tempo_bpm == 60
    assert read_midi_bytes(wf.export_midi(bundle.arranged)).tempo_bpm == pytest.approx(60)
    assert bundle.fidelity["melodic_recall"] == 1.0, "slower is not the same as late"
