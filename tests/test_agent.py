"""Agent loop tests.

Runs under pytest, or standalone: `python tests/test_agent.py`

Every test here uses ScriptedModel — no API key, no network, no cost. That
separation is deliberate: loop bugs and model-quality problems are different
problems, and mixing them means every debugging session costs money and
returns different results each run.

What these pin down is the loop's *contract*: bounded attempts, best-so-far
tracking, malformed output handled as feedback rather than a crash, and
escalation when the budget runs out.
"""

import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.agent import (  # noqa: E402
    FIDELITY_FLOOR, Attempt, ScriptedModel, TruncatedResponse, arrange,
    brute_force_baseline, cost, describe_score, describe_verdict, _parse_plan,
)
from arranger.fidelity import Fidelity, measure  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section, simple_plan  # noqa: E402
from arranger.profile import PlayerProfile, PRESETS  # noqa: E402
from arranger.render import render, last_bar  # noqa: E402
from arranger.verify import verify  # noqa: E402

PROFILE = PRESETS["intermediate"]


def make_source(bars: int = 4) -> Score:
    """A simple I-V piece with melody on top and triads underneath."""
    notes = []
    for bar in range(1, bars + 1):
        t = (bar - 1) * 2.0
        root = 60 if bar % 2 else 67
        for pitch, onset, dur in (
            (root, t, 1.9), (root + 4, t, 1.9), (root + 7, t, 1.9),
            (root + 12, t, 0.9), (root + 14, t + 1.0, 0.9),
        ):
            notes.append(Note(pitch=pitch, onset=onset, duration=dur, bar=bar))
    return Score(notes=notes, title="test piece")


SOURCE = make_source()
END = last_bar(SOURCE)


def plan_dict(pattern: LHPattern, voices: int = 3) -> dict:
    return asdict(ArrangementPlan(
        title="t", sections=[Section(1, END, pattern, lh_voices=voices)]
    ))


# --- parsing model output ------------------------------------------------

def test_markdown_fences_do_not_waste_an_attempt():
    # Models wrap JSON in fences despite instructions. A formatting slip is
    # not a planning mistake and must not cost a retry.
    raw = '```json\n{"sections": [{"start_bar": 1, "end_bar": 4}]}\n```'
    assert _parse_plan(raw).sections[0].end_bar == 4


def test_prose_around_the_json_is_tolerated():
    raw = 'Here is my plan:\n{"sections": [{"start_bar": 1, "end_bar": 8}]}\nHope that helps.'
    assert _parse_plan(raw).sections[0].end_bar == 8


def test_truncation_is_reported_as_truncation():
    # A cut-off response fails as a JSON syntax error, which sends the model
    # hunting for a missing comma that was never the problem. It must be told
    # it ran out of room. Cost the first live run an attempt.
    class Truncating:
        input_tokens = output_tokens = 0
        calls = 0

        def __call__(self, messages):
            Truncating.calls += 1
            raise TruncatedResponse("response hit the token limit")

    result = arrange(SOURCE, PROFILE, Truncating(), max_attempts=2, verbose=False)
    assert all("token limit" in (a.error or "") for a in result.attempts)
    assert result.escalated


def test_response_with_no_json_raises():
    try:
        _parse_plan("I cannot arrange this piece.")
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a response containing no plan")


# --- describing the problem ---------------------------------------------

def test_summary_mentions_the_player_limits():
    text = describe_score(SOURCE, PROFILE)
    assert str(PROFILE.max_span) in text and "skill level" in text


def test_summary_warns_when_melody_exceeds_one_hand():
    # A real ascending line, not two isolated notes two octaves apart — the
    # melody floor correctly treats the latter's lower note as accompaniment,
    # so that fixture tested nothing.
    rising = Score(notes=[
        Note(pitch=60 + 3 * i, onset=i * 0.5, duration=0.4, bar=1 + i // 4)
        for i in range(9)
    ], title="rising")
    assert "octave displacement" in describe_score(rising, PROFILE)


def test_feedback_names_the_section_to_edit():
    # The model edits sections. Feedback that only gives timestamps is not
    # actionable, however precise it is.
    plan = ArrangementPlan(sections=[Section(1, END, LHPattern.BROKEN_OCTAVE)])
    verdict = verify(render(plan, SOURCE), PRESETS["beginner"])
    if verdict.hard:
        assert "sections" in describe_verdict(verdict, plan)


def test_feedback_on_a_clean_run_says_so():
    plan = simple_plan(END, LHPattern.PEDAL_TONE)
    verdict = verify(render(plan, SOURCE), PRESETS["advanced"])
    if verdict.playable:
        assert "PLAYABLE" in describe_verdict(verdict, plan)


# --- the loop ------------------------------------------------------------

def test_loop_stops_as_soon_as_it_succeeds():
    model = ScriptedModel([plan_dict(LHPattern.PEDAL_TONE, 1)])
    result = arrange(SOURCE, PRESETS["advanced"], model, verbose=False)
    if result.playable:
        assert len(result.attempts) == 1, "kept going after succeeding"


def test_loop_respects_its_budget():
    # A model that only ever returns something unplayable must not loop forever.
    model = ScriptedModel([plan_dict(LHPattern.BROKEN_OCTAVE, 5)])
    result = arrange(SOURCE, PRESETS["beginner"], model, max_attempts=3, verbose=False)
    assert len(result.attempts) <= 3
    assert model.calls <= 3


def test_a_worse_later_attempt_does_not_overwrite_a_better_one():
    # The failure this guards against: the loop appears to make progress, then
    # hands back a regression because it returned the most recent plan.
    model = ScriptedModel([
        plan_dict(LHPattern.PEDAL_TONE, 1),      # good
        plan_dict(LHPattern.BROKEN_OCTAVE, 5),   # much worse
        plan_dict(LHPattern.BROKEN_OCTAVE, 5),
    ])
    result = arrange(SOURCE, PRESETS["beginner"], model, max_attempts=3, verbose=False)
    first = result.attempts[0].hard
    assert result.best_hard is not None and first is not None
    assert result.best_hard <= first, "a worse attempt replaced a better one"


def test_malformed_output_is_feedback_not_a_crash():
    class Broken:
        calls = 0
        input_tokens = output_tokens = 0

        def __call__(self, messages):
            Broken.calls += 1
            return "sorry, I can't do that"

    result = arrange(SOURCE, PROFILE, Broken(), max_attempts=2, verbose=False)
    assert len(result.attempts) == 2
    assert all(a.error for a in result.attempts)
    assert result.escalated


def test_invalid_plans_are_rejected_and_reported():
    # Overlapping sections: the renderer must refuse, and the loop must
    # record why rather than silently rendering one of them.
    bad = asdict(ArrangementPlan(sections=[Section(1, 4), Section(3, 6)]))
    result = arrange(SOURCE, PROFILE, ScriptedModel([bad]), max_attempts=1, verbose=False)
    assert result.attempts[0].error and "cover bar" in result.attempts[0].error


def test_escalation_is_recorded_when_the_budget_runs_out():
    model = ScriptedModel([plan_dict(LHPattern.BROKEN_OCTAVE, 5)])
    result = arrange(SOURCE, PRESETS["beginner"], model, max_attempts=2, verbose=False)
    if not result.playable:
        assert result.escalated


def test_run_log_records_every_attempt():
    # Uses plans that are always rejected, so the budget is guaranteed to be
    # spent. Picking a "hard" pattern instead is unreliable: broken octaves at
    # two seconds per bar are playable even for a beginner, which is how the
    # first version of this test ended up asserting on a single attempt.
    bad = asdict(ArrangementPlan(sections=[Section(1, 4), Section(3, 6)]))
    result = arrange(SOURCE, PROFILE, ScriptedModel([bad]), max_attempts=2,
                     verbose=False)
    import json
    log = json.loads(result.to_json())
    assert len(log["attempts"]) == 2
    assert log["baseline_hard"] == len(verify(SOURCE, PROFILE).hard)
    assert all(a["error"] for a in log["attempts"])


# --- the baseline the agent must beat -----------------------------------

def test_brute_force_returns_a_real_plan():
    c, hard, pattern, voices, fold = brute_force_baseline(SOURCE, PROFILE)
    assert c >= 0 and hard >= 0 and pattern in {str(p) for p in LHPattern}
    assert 1 <= voices <= 3 and fold in (0, 12, 16)


# --- the objective -------------------------------------------------------

def test_deleting_the_accompaniment_costs_more_than_it_saves():
    # The whole point of the fidelity floor. An empty arrangement is perfectly
    # playable; the objective must not reward that.
    gutted = render(ArrangementPlan(sections=[Section(1, END, lh_voices=0)]), SOURCE)
    complete = render(ArrangementPlan(sections=[Section(1, END, lh_voices=2)]), SOURCE)
    g_hard = len(verify(gutted, PROFILE).hard)
    c_hard = len(verify(complete, PROFILE).hard)
    g_cost = cost(g_hard, measure(SOURCE, gutted))
    c_cost = cost(c_hard, measure(SOURCE, complete))
    assert g_hard <= c_hard, "fixture invalid: gutting did not reduce violations"
    assert g_cost > c_cost, "the objective still rewards deleting music"


def test_full_fidelity_costs_only_its_violations():
    perfect = Fidelity(1.0, 1.0, 1.0)
    assert cost(3, perfect) == 3


def test_fidelity_above_the_floor_earns_nothing_extra():
    # The goal is a complete arrangement that plays, not the most faithful one
    # imaginable. Rewarding surplus fidelity would trade playability for it.
    at_floor = cost(2, Fidelity(FIDELITY_FLOOR, FIDELITY_FLOOR, FIDELITY_FLOOR))
    assert cost(2, Fidelity(1.0, 1.0, 1.0)) == at_floor


def test_melody_survives_octave_folding_in_the_score():
    # Fidelity compares pitch classes, not absolute pitch — otherwise folding,
    # the encouraged fix for wide melodies, would be punished as note loss.
    folded = render(
        ArrangementPlan(sections=[Section(1, END, melody_fold_window=12)]), SOURCE
    )
    assert measure(SOURCE, folded).melodic_recall > 0.9


def test_brute_force_is_at_least_as_good_as_any_single_choice():
    best = brute_force_baseline(SOURCE, PROFILE)[0]
    for pattern in LHPattern:
        plan = ArrangementPlan(sections=[Section(1, END, pattern, lh_voices=2)])
        assert best <= len(verify(render(plan, SOURCE), PROFILE).hard)


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
