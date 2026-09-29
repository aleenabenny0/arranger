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
import time

import pytest
import json
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.agent import (  # noqa: E402
    FIDELITY_FLOOR, ScriptedModel, TruncatedResponse, arrange,
    brute_force_baseline, cost, describe_score, describe_verdict, _parse_plan,
)
from arranger.fidelity import Fidelity, measure  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.agent import (  # noqa: E402
    ModelRefused, ProviderError, RepairBudget, _fingerprint,
)
from arranger.engine import Cancelled  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section, simple_plan  # noqa: E402
from arranger.profile import PRESETS, PlayerProfile  # noqa: E402
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
    assert len(result.attempts) == 2
    assert all("token limit" in (a.error or "") for a in result.attempts)
    # The model never delivered a plan. That used to mean escalation; now the
    # deterministic draft stands, so a useless model costs nothing but money.
    assert result.best_origin in ("deterministic", "local_repair")
    assert result.accepted and not result.escalated


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


def test_first_prompt_includes_deterministic_draft():
    class CapturingModel:
        input_tokens = output_tokens = 0
        messages = None

        def __call__(self, messages):
            CapturingModel.messages = messages
            return json.dumps(plan_dict(LHPattern.PEDAL_TONE, 1))

    arrange(SOURCE, PRESETS["advanced"], CapturingModel(), max_attempts=1, verbose=False)
    assert CapturingModel.messages is not None
    first = CapturingModel.messages[0]["content"]
    assert "deterministic draft" in first
    assert "ARRANGING GUIDANCE RETRIEVED FOR THIS SCORE" in first
    assert "Lowest-priority notes to thin first" in first
    assert '"sections"' in first


def test_retry_feedback_includes_deterministic_repair_suggestions():
    source = Score(
        notes=[
            Note(48, 0.0, 1.0, bar=1),
            Note(52, 0.0, 1.0, bar=1),
            Note(55, 0.0, 1.0, bar=1),
            Note(58, 0.0, 1.0, bar=1),
            Note(72, 0.0, 1.0, bar=1),
        ],
        title="thick chord",
    )
    bad = asdict(
        ArrangementPlan(
            title="bad",
            sections=[Section(1, 1, LHPattern.BLOCK, lh_voices=4)],
        )
    )
    good = asdict(
        ArrangementPlan(
            title="good",
            sections=[Section(1, 1, LHPattern.PEDAL_TONE, lh_voices=1)],
        )
    )

    class CapturingBadThenGood:
        input_tokens = output_tokens = 0
        messages = None
        calls = 0

        def __call__(self, messages):
            CapturingBadThenGood.messages = messages
            CapturingBadThenGood.calls += 1
            if CapturingBadThenGood.calls == 1:
                return json.dumps(bad)
            return json.dumps(good)

    arrange(
        source,
        PRESETS["beginner"],
        CapturingBadThenGood(),
        max_attempts=2,
        verbose=False,
    )
    assert CapturingBadThenGood.messages is not None
    retry = CapturingBadThenGood.messages[-1]["content"]
    assert "DETERMINISTIC REPAIR SUGGESTIONS" in retry
    assert "lh_pattern" in retry or "lh_voices" in retry


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
    assert result.best_origin in ("deterministic", "local_repair")
    assert result.stop_reason == "attempts_exhausted"


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


# --- the deterministic draft is candidate zero ---------------------------

IMPOSSIBLE = PlayerProfile(
    name="impossible", max_span=2, comfortable_span=1, max_notes_per_hand=1,
    max_leap_rate=0.5, leap_slack=0, lowest_pitch=70, highest_pitch=72,
)


class Recording:
    """A model that returns scripted replies and records what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.input_tokens = self.output_tokens = 0

    def __call__(self, messages):
        self.seen.append(list(messages))
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def test_no_model_means_a_deterministic_arrangement_not_an_error():
    result = arrange(SOURCE, PROFILE, None, verbose=False)
    assert result.accepted and result.best_plan and not result.attempts
    assert result.best_origin in ("deterministic", "local_repair")
    assert result.draft is not None and result.draft.number == 0
    assert result.cost_usd == 0 and result.algorithm_version


def test_a_model_cannot_make_the_result_worse_than_the_draft():
    alone = arrange(SOURCE, PRESETS["beginner"], None, verbose=False)
    model = Recording([plan_dict(LHPattern.BROKEN_OCTAVE, 5)])
    helped = arrange(SOURCE, PRESETS["beginner"], model, max_attempts=3, verbose=False)
    assert helped.best_cost <= alone.best_cost
    if helped.best_origin != "model":
        assert helped.best_plan == alone.best_plan


def test_an_equally_good_model_plan_does_not_displace_the_draft():
    alone = arrange(SOURCE, PROFILE, None, verbose=False)
    echo = Recording([alone.best_plan])
    result = arrange(SOURCE, PROFILE, echo, max_attempts=1, verbose=False)
    assert result.best_origin != "model"


def test_a_hosted_run_does_not_pay_to_improve_an_acceptable_draft():
    model = Recording([plan_dict(LHPattern.BLOCK)])
    result = arrange(SOURCE, PROFILE, model, verbose=False,
                     budget=RepairBudget(skip_model_when_draft_accepted=True))
    assert model.calls == 0 and result.stop_reason == "draft_accepted" and result.accepted


def test_the_model_is_told_what_it_has_to_beat():
    model = Recording([plan_dict(LHPattern.BLOCK)])
    arrange(SOURCE, PROFILE, model, max_attempts=1, verbose=False)
    assert "will be discarded in its favour" in model.seen[0][0]["content"]


def test_escalation_means_nothing_acceptable_was_found_by_anyone():
    model = Recording([plan_dict(LHPattern.PEDAL_TONE, 1)])
    result = arrange(SOURCE, IMPOSSIBLE, model, max_attempts=2, verbose=False)
    assert result.escalated and not result.accepted
    assert result.best_plan is not None, "even a failed run hands back its best effort"


# --- every way the loop stops ----------------------------------------------


def test_stops_when_the_same_plan_keeps_coming_back():
    model = Recording([plan_dict(LHPattern.BROKEN_OCTAVE, 5)])
    result = arrange(SOURCE, IMPOSSIBLE, model, verbose=False,
                     budget=RepairBudget(max_attempts=8, patience=99))
    assert result.stop_reason == "repeated_plan"
    assert model.calls == 3, "one original, one tolerated repeat, one too many"
    assert "renders identically" in model.seen[2][-1]["content"]


def test_relabelling_a_plan_does_not_make_it_new():
    first = plan_dict(LHPattern.BROKEN_OCTAVE, 5)
    second = json.loads(json.dumps(first))
    second["title"] = "completely different, honest"
    second["notes"] = "new reasoning"
    second["sections"][0]["label"] = "renamed"
    assert _fingerprint(ArrangementPlan.from_dict(first)) == _fingerprint(ArrangementPlan.from_dict(second))
    second["sections"][0]["lh_voices"] = 2
    assert _fingerprint(ArrangementPlan.from_dict(first)) != _fingerprint(ArrangementPlan.from_dict(second))


def test_stops_when_valid_attempts_stop_improving():
    plans = [plan_dict(LHPattern.BROKEN_OCTAVE, v) for v in (5, 4, 3, 2)]
    model = Recording(plans)
    result = arrange(SOURCE, IMPOSSIBLE, model, verbose=False,
                     budget=RepairBudget(max_attempts=8, patience=2, max_repeated_plans=99))
    assert result.stop_reason == "no_improvement" and model.calls == 2


def test_stops_on_the_time_budget():
    class Slow(Recording):
        def __call__(self, messages):
            time.sleep(0.3)
            return super().__call__(messages)

    model = Slow([plan_dict(LHPattern.BROKEN_OCTAVE, v) for v in (5, 4, 3, 2, 1)])
    result = arrange(SOURCE, IMPOSSIBLE, model, verbose=False,
                     budget=RepairBudget(max_attempts=50, max_seconds=1.6, patience=99, max_repeated_plans=99))
    assert result.stop_reason == "time_budget" and model.calls < 10


def test_stops_on_the_cost_budget():
    class Pricey(Recording):
        cost_usd = 0.0

        def __call__(self, messages, **options):
            Pricey.cost_usd += 0.4
            self.options = options
            return super().__call__(messages)

    Pricey.cost_usd = 0.0
    model = Pricey([plan_dict(LHPattern.BROKEN_OCTAVE, v) for v in (5, 4, 3, 2)])
    result = arrange(SOURCE, IMPOSSIBLE, model, verbose=False,
                     budget=RepairBudget(max_attempts=9, max_cost_usd=1.0, patience=99, max_repeated_plans=99))
    assert result.stop_reason == "cost_budget" and model.calls == 3
    assert result.cost_usd == pytest.approx(1.2)
    assert model.options["max_tokens"] >= 1024 and model.options["timeout"] > 0


def test_provider_failures_end_the_run_and_keep_the_draft():
    fatal = Recording([ProviderError("bad key", retryable=False)])
    result = arrange(SOURCE, PROFILE, fatal, max_attempts=4, verbose=False)
    assert result.stop_reason == "provider_failure" and fatal.calls == 1
    assert result.accepted and result.best_origin != "model"

    flaky = Recording([ProviderError("503", retryable=True)])
    result = arrange(SOURCE, PROFILE, flaky, max_attempts=6, verbose=False)
    assert result.stop_reason == "provider_failure" and flaky.calls == 2
    assert all("ProviderError" in a.error for a in result.attempts)


def test_a_transient_provider_failure_is_not_fed_back_as_a_bad_plan():
    model = Recording([ProviderError("503", retryable=True), plan_dict(LHPattern.BLOCK)])
    arrange(SOURCE, IMPOSSIBLE, model, max_attempts=2, verbose=False)
    assert len(model.seen[1]) == 1, "the model never answered, so there is nothing to correct"


def test_a_refusal_ends_the_run():
    model = Recording([ModelRefused("declined")])
    result = arrange(SOURCE, PROFILE, model, max_attempts=4, verbose=False)
    assert result.stop_reason == "model_refused" and model.calls == 1


def test_an_oversized_response_is_rejected_before_parsing():
    huge = "{" + " " * 5000 + "}"
    model = Recording([huge])
    result = arrange(SOURCE, PROFILE, model, verbose=False,
                     budget=RepairBudget(max_attempts=1, max_response_chars=1000))
    assert "the limit is 1000" in result.attempts[0].error


def test_a_plan_that_does_not_fit_the_piece_is_rejected_with_the_reason():
    short = asdict(ArrangementPlan(sections=[Section(1, 2)]))
    result = arrange(SOURCE, PROFILE, Recording([short]), max_attempts=1, verbose=False)
    assert "not covered" in result.attempts[0].error


def test_cancellation_stops_promptly():
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(Cancelled):
        arrange(SOURCE, IMPOSSIBLE, Recording([plan_dict(LHPattern.BLOCK)]), verbose=False, should_cancel=cancel)


def test_progress_is_reported_and_ends_at_one():
    seen: list[float] = []
    arrange(SOURCE, PROFILE, None, verbose=False, progress=lambda f, _stage: seen.append(f))
    assert seen == sorted(seen) and seen[-1] == 1.0 and 0 < seen[0] < 1


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
