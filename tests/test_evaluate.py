"""The evaluation harness: brute force, the engine and the model on the same ordering.

Every test uses the scripted model or no model at all: nothing here touches
the network, and the corpus is not needed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.agent import RunResult, ScriptedModel  # noqa: E402
from arranger.evaluate import (  # noqa: E402
    Contestant,
    PieceResult,
    evaluate,
    main,
    measure_piece,
    outcome,
    protocol_verdict,
    results_to_json,
    scorecard,
)
from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section  # noqa: E402
from arranger.profile import PRESETS  # noqa: E402
from arranger.render import last_bar  # noqa: E402

PROFILE = PRESETS["intermediate"]


def make_source(bars: int = 6) -> Score:
    notes = []
    for bar in range(1, bars + 1):
        t = (bar - 1) * 2.0
        root = 60 if bar % 2 else 65
        for pitch, onset, dur in (
            (root - 12, t, 1.9), (root - 8, t, 1.9), (root - 5, t, 1.9),
            (root + 12, t, 0.9), (root + 14, t + 1.0, 0.9),
        ):
            notes.append(Note(pitch=pitch, onset=onset, duration=dur, bar=bar))
    return Score(notes=notes, tempo_bpm=96, title="eval fixture")


def side(cost, tempo=1.0, strain=0, fidelity=0.9) -> Contestant:
    return Contestant(cost, int(cost), strain, fidelity, tempo, 1, cost == 0, 0.0, "x")


def run(cost: float, accepted: bool = True) -> RunResult:
    return RunResult(title="t", baseline_hard=0, best_hard=int(cost), best_plan={"sections": [{}]},
                     playable=accepted, best_cost=cost, accepted=accepted, best_strain=0,
                     best_fidelity={"melodic_recall": 1, "harmonic_coverage": 1, "accompaniment": 1,
                                    "rhythm": 1, "contour": 1, "bass": 1})


def piece(name: str, baseline_cost: float, run_costs: list[float]) -> PieceResult:
    result = PieceResult(name=name, notes=1, bars=1, source_hard=0, baseline=side(baseline_cost))
    result.runs = [run(c) for c in run_costs]
    result.model_contestants = [Contestant.from_run(r) for r in result.runs]
    return result


# --- outcomes -------------------------------------------------------------------


def test_outcome_on_cost_and_on_quality():
    baseline = side(0, strain=4, fidelity=0.90)
    assert outcome(side(1), baseline) == ("loss", "loss")
    assert outcome(side(0, strain=4, fidelity=0.90), baseline) == ("tie", "tie")
    assert outcome(side(0, strain=2, fidelity=0.90), baseline) == ("tie", "win")
    assert outcome(side(0, strain=4, fidelity=0.95), baseline) == ("tie", "win")
    assert outcome(side(0, tempo=0.85, strain=0, fidelity=1.0), baseline) == ("tie", "loss")
    assert outcome(side(0), side(2)) == ("win", "win")


def test_contestant_from_run_reads_the_run_log():
    r = run(0)
    r.best_plan = {"tempo_scale": 0.7, "sections": [{"lh_pattern": "block", "lh_voices": 2, "melody_fold_window": 12}]}
    c = Contestant.from_run(r)
    assert c.plan == "block/v2/f12 @70%" and c.tempo_scale == 0.7 and c.fidelity == 1.0 and c.accepted


# --- the protocol verdict ---------------------------------------------------------


def test_verdict_needs_three_qualifying_pieces():
    verdict, notes = protocol_verdict([piece("a", 2.0, [1, 1, 1]), piece("b", 0.0, [0, 0, 0])])
    assert verdict == "INSUFFICIENT DATA" and "1 qualifying" in notes[0]


def test_verdict_passes_on_a_strict_majority_of_pieces_each_won_by_a_strict_majority():
    results = [
        piece("a", 2.0, [1, 1, 1, 2, 2]),   # 3/5 wins
        piece("b", 3.0, [2, 2, 1, 3, 3]),   # 3/5 wins
        piece("c", 1.0, [1, 1, 1, 0, 0]),   # 2/5: not a win
    ]
    verdict, notes = protocol_verdict(results)
    assert verdict == "PASS" and "2 of 3" in notes[-1]
    results[1] = piece("b", 3.0, [3, 3, 3, 2, 2])      # 2/5 now
    verdict, notes = protocol_verdict(results)
    assert verdict == "FAIL"
    assert any("loses to brute force" in line for line in notes), "a typical loss gets its own line"


def test_ties_never_count_as_wins():
    result = piece("a", 2.0, [2, 2, 2])
    assert result.beat_baseline() == 0 and not result.model_wins_piece()


# --- measuring a piece ----------------------------------------------------------------


def test_measure_piece_runs_brute_force_and_the_engine_without_a_model():
    source = make_source()
    result = measure_piece(source, PROFILE, name="fixture")
    assert result.engine is not None and result.runs == []
    assert result.baseline.sections == 1 and "/" in result.baseline.plan
    on_cost, on_quality = result.engine_outcome()
    assert on_cost in ("win", "tie") and on_quality in ("win", "tie"), "the engine never loses"
    assert result.baseline.seconds >= 0 and result.engine.seconds >= 0


def test_measure_piece_with_a_scripted_model_records_every_run():
    source = make_source()
    plan = ArrangementPlan(sections=[Section(1, last_bar(source), LHPattern.PEDAL_TONE, lh_voices=1)])
    factory = lambda: ScriptedModel([json.loads(plan.to_json())])  # noqa: E731
    result = measure_piece(source, PROFILE, model_factory=factory, repeats=2, attempts=2)
    assert len(result.runs) == 2 and len(result.model_contestants) == 2
    assert all(c in ("win", "tie", "loss") for c, _ in result.model_outcomes())


def test_scorecard_reports_both_comparisons_and_the_verdict():
    source = make_source()
    plan = ArrangementPlan(sections=[Section(1, last_bar(source), LHPattern.BLOCK, lh_voices=1)])
    factory = lambda: ScriptedModel([json.loads(plan.to_json())])  # noqa: E731
    results = [measure_piece(source, PROFILE, name="fixture", model_factory=factory, repeats=1, attempts=1)]
    text = scorecard(results, 1, 1, True, profile_name=PROFILE.name)
    assert "## Deterministic engine vs brute force" in text
    assert "| fixture |" in text and "on quality" in text
    assert "## Model loop vs brute force" in text and "Protocol verdict: INSUFFICIENT DATA" in text
    payload = results_to_json(results)
    assert payload["pieces"][0]["engine_outcome"][1] in ("win", "tie")
    json.dumps(payload)


def test_evaluate_reads_files_and_survives_an_unreadable_one(tmp_path):
    good = tmp_path / "good.mid"
    good.write_bytes(write_midi(make_source()))
    bad = tmp_path / "bad.mid"
    bad.write_bytes(b"not midi at all")
    results = evaluate([good, bad], PROFILE, repeats=1, log=None)
    assert results[0].error is None and results[0].engine is not None
    assert results[1].error and results[1].engine is None
    text = scorecard(results, 1, 4, True)
    assert "error:" in text


def test_main_writes_the_scorecard_and_json_without_touching_the_network(tmp_path):
    path = tmp_path / "piece.mid"
    path.write_bytes(write_midi(make_source()))
    out, numbers = tmp_path / "SCORECARD.md", tmp_path / "numbers.json"
    code = main([str(path), "--profile", str(Path("profiles") / "me.json"),
                 "--out", str(out), "--json", str(numbers)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("# Scorecard")
    data = json.loads(numbers.read_text(encoding="utf-8"))
    assert data["pieces"][0]["runs"] == [], "no model was named, so no model ran"
    assert data["pieces"][0]["engine"]["cost"] >= 0


def test_main_without_files_or_corpus_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 2
