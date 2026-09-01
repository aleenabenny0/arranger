"""Architecture boundary tests.

These are intentionally small. They do not test musical correctness; the
existing verifier, renderer, and agent tests do that. This file pins the
public surfaces that future CLIs, APIs, and workers should call.
"""

import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.score_json import load_score_json  # noqa: E402
from arranger.application import (  # noqa: E402
    arrange_score,
    baseline_for,
    fidelity_for,
    render_plan,
    verify_score,
)
from arranger.agent import ScriptedModel  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section  # noqa: E402
from arranger.profile import PRESETS  # noqa: E402


def make_source() -> Score:
    notes = []
    for bar, root in ((1, 60), (2, 67)):
        t = (bar - 1) * 2.0
        for pitch in (root, root + 4, root + 7, root + 12):
            notes.append(Note(pitch=pitch, onset=t, duration=1.0, bar=bar))
    return Score(notes=notes, title="architecture")


def test_application_use_cases_cover_core_flow():
    source = make_source()
    profile = PRESETS["intermediate"]
    plan = ArrangementPlan(sections=[Section(1, 2, LHPattern.PEDAL_TONE, lh_voices=1)])

    arranged = render_plan(plan, source)
    verdict = verify_score(arranged, profile)
    fidelity = fidelity_for(source, arranged)
    baseline = baseline_for(source, profile)

    assert arranged.notes
    assert verdict.title == arranged.title
    assert 0 <= fidelity.score() <= 1
    assert baseline[0] >= 0


def test_arrange_score_accepts_a_model_port():
    source = make_source()
    profile = PRESETS["advanced"]
    plan = asdict(
        ArrangementPlan(sections=[Section(1, 2, LHPattern.PEDAL_TONE, lh_voices=1)])
    )

    result = arrange_score(
        source,
        profile,
        ScriptedModel([plan]),
        max_attempts=1,
        verbose=False,
    )

    assert len(result.attempts) == 1
    assert result.best_plan is not None


def test_score_json_adapter_loads_debug_format():
    loaded = load_score_json(Path("tests") / "fixtures" / "too_hard.json")

    assert loaded.title == "over-ambitious chorale"
    assert loaded.tempo_bpm == 76
    assert len(loaded.notes) == 10


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
