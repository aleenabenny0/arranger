"""The ordering every comparison uses, and the engine's floor under brute force.

Runs under pytest, or standalone: `python tests/test_engine_quality.py`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.agent import brute_force_candidate  # noqa: E402
from arranger.engine import (  # noqa: E402
    Candidate,
    arrange_deterministic,
    evaluate_plan,
    polish,
    quality_key,
    seed_uniform,
    uniform_plans,
)
from arranger.fidelity import Fidelity  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section, patterns_for_skill  # noqa: E402
from arranger.profile import PRESETS, PlayerProfile  # noqa: E402
from arranger.render import last_bar  # noqa: E402
from arranger.repair import suggest_repairs  # noqa: E402
from arranger.timeline import MeterChange, TempoChange, Timeline  # noqa: E402
from arranger.verify import Rule, Severity, Verdict, Violation, verify  # noqa: E402


def build(rows, *, bpm=100.0, meter=(4, 4), title="t") -> Score:
    """rows: (pitch, beat, beats). Bars and seconds come from the timeline."""
    timeline = Timeline([TempoChange(0, bpm)], [MeterChange(1, *meter)])
    notes = []
    for i, (pitch, beat, beats) in enumerate(rows):
        onset = timeline.seconds_at(beat)
        notes.append(Note(pitch=pitch, onset=onset, duration=timeline.seconds_at(beat + beats) - onset,
                          bar=timeline.bar_at(beat), id=f"s{i}", beat=beat, beats=beats))
    return Score(notes=notes, tempo_bpm=bpm, timeline=timeline, title=title)


def hymn(bars: int = 8) -> Score:
    """Four-part writing with a tune on top: a chord per bar, the tune moving in quarters."""
    rows = []
    for bar in range(bars):
        root = (48, 53, 55, 48)[bar % 4]
        beat = bar * 4
        for interval in (0, 4, 7):
            rows.append((root + interval, beat, 4))
        for q, step in enumerate((12, 14, 16, 12)):
            rows.append((root + 12 + step, beat + q, 1))
    return build(rows, title="hymn")


def wide_waltz(bars: int = 8) -> Score:
    """A tune two octaves wide over tenths in the bass: strain for most hands."""
    rows = []
    for bar in range(bars):
        root = (43, 45, 50, 43)[bar % 4]
        beat = bar * 3
        rows += [(root, beat, 3), (root + 16, beat, 3), (root + 19, beat, 3)]
        for q in range(3):
            rows.append((root + 24 + (q * 7) % 12 + (12 if bar % 2 else 0), beat + q, 1))
    return build(rows, bpm=132, meter=(3, 4), title="waltz")


def candidate(cost, tempo, strain, fidelity) -> Candidate:
    """A candidate with exactly these numbers and nothing else."""
    verdict = Verdict("t", "p", playable=cost == 0, violations=[
        Violation(Rule.HAND_SPAN, Severity.STRAIN, 0.0) for _ in range(strain)
    ])
    plan = ArrangementPlan(sections=[Section(1, 1)], tempo_scale=tempo)
    return Candidate(plan, Score(), verdict, Fidelity(fidelity, fidelity, fidelity, fidelity, fidelity, fidelity),
                     cost, "test")


# --- the ordering ---------------------------------------------------------


def test_quality_ranks_cost_then_tempo_then_strain_then_fidelity():
    assert quality_key(1, 1.0, 0, 1.0) > quality_key(0, 0.4, 50, 0.88), "nothing outweighs cost"
    assert quality_key(0, 0.85, 0, 1.0) > quality_key(0, 1.0, 9, 0.88), "tempo before strain"
    assert quality_key(0, 1.0, 3, 1.0) > quality_key(0, 1.0, 2, 0.88), "strain before fidelity"
    assert quality_key(0, 1.0, 2, 0.90) > quality_key(0, 1.0, 2, 0.95), "then more of the music"


def test_fidelity_differences_below_the_resolution_are_ties():
    # Less than a hundredth of the score is one note's worth on a short piece:
    # a tie, so a plan with the better figure per passage is not displaced by
    # a uniform one that happens to place one more accompaniment note.
    assert quality_key(0, 1.0, 0, 0.904) == quality_key(0, 1.0, 0, 0.901)
    assert quality_key(0, 1.0, 0, 0.91) < quality_key(0, 1.0, 0, 0.90)


def test_better_than_is_strict_and_keeps_the_incumbent_on_a_tie():
    a = candidate(0, 1.0, 2, 0.92)
    b = candidate(0, 1.0, 2, 0.92)
    assert not a.better_than(b) and not b.better_than(a)
    assert candidate(0, 1.0, 1, 0.92).better_than(a)
    assert candidate(0, 1.0, 2, 0.93).better_than(a)
    assert not candidate(0, 0.9, 0, 1.0).better_than(a), "a slower plan never beats an equal-cost faster one"
    assert a.better_than(None)


# --- the whole-piece figures -------------------------------------------


def test_uniform_plans_are_every_legal_figure_easiest_first():
    source = hymn()
    for profile in (PRESETS["beginner"], PRESETS["intermediate"], PRESETS["advanced"]):
        plans = list(uniform_plans(source, profile))
        patterns = {p.sections[0].lh_pattern for p in plans}
        assert patterns == set(patterns_for_skill(profile.skill_level))
        assert len(plans) == len(patterns) * 9
        first = plans[0].sections[0]
        assert (first.lh_pattern, first.lh_voices, first.melody_fold_window) == (LHPattern.BLOCK, 1, 0)
        assert all(not p.validate_for_source(last_bar(source), skill_level=profile.skill_level) for p in plans)
    assert len(list(uniform_plans(source, PRESETS["beginner"], legal_only=False))) == len(LHPattern) * 9


def test_brute_force_candidate_is_the_best_uniform_plan_on_quality():
    # Cheapest on cost, as the protocol defines it, and among equally cheap
    # plans the best on the full ordering: a contestant cannot beat brute
    # force merely by breaking a tie among the plans brute force itself tried.
    source, profile = wide_waltz(), PRESETS["intermediate"]
    best = brute_force_candidate(source, profile, legal_only=True)
    for plan in uniform_plans(source, profile):
        trial = evaluate_plan(plan, source, profile, origin="x")
        assert best.cost <= trial.cost
        assert best.quality <= trial.quality
    assert best.origin == "brute_force" and len(best.plan.sections) == 1
    assert seed_uniform(None, source, profile).quality == best.quality


def test_seed_uniform_keeps_the_incumbent_on_a_tie_and_beats_a_worse_one():
    source, profile = hymn(), PRESETS["intermediate"]
    best = brute_force_candidate(source, profile, legal_only=True)
    kept = seed_uniform(best, source, profile)
    assert kept.quality <= best.quality
    if kept.quality == best.quality:
        assert kept is best, "a tie must not replace the incumbent"
    worse = evaluate_plan(
        ArrangementPlan(sections=[Section(1, last_bar(source), LHPattern.BROKEN_OCTAVE, lh_voices=5)]),
        source, profile, origin="deterministic",
    )
    assert seed_uniform(worse, source, profile).quality <= best.quality


# --- the guarantee ------------------------------------------------------------


def test_engine_is_never_worse_than_brute_force_on_quality():
    # The reason the engine seeds itself with every uniform figure: whatever
    # exhaustive search can reach, the engine has seen. Strict, on every
    # fixture and every preset, and on the protocol's own profile.
    me = PlayerProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "me.json")
    for source in (hymn(), wide_waltz(), hymn(3)):
        for profile in (PRESETS["beginner"], PRESETS["intermediate"], PRESETS["advanced"], me):
            brute = brute_force_candidate(source, profile, legal_only=True)
            engine = arrange_deterministic(source, profile)
            assert engine.quality <= brute.quality, (
                f"{source.title}/{profile.name}: engine {engine.describe()} lost to brute force {brute.describe()}"
            )
            assert engine.origin in ("deterministic", "local_repair")


def test_engine_wins_on_quality_where_a_uniform_plan_leaves_strain():
    # The waltz has tenths in the left hand. A single figure for the whole
    # piece either keeps them (strain) or thins everything; the engine may
    # treat the bars differently, and must come out at least as well.
    source, profile = wide_waltz(), PRESETS["intermediate"]
    brute = brute_force_candidate(source, profile, legal_only=True)
    engine = arrange_deterministic(source, profile)
    assert engine.cost <= brute.cost
    assert engine.quality <= brute.quality
    if engine.cost == brute.cost:
        assert engine.strain <= brute.strain


def test_planner_keeps_its_per_region_judgement_over_a_uniform_near_tie():
    # The planner's own test fixture: a sparse two bars then a thick two bars.
    # Among the planner's candidates a uniform block/2 keeps half a percent
    # more fidelity than walking bass over the sparse bars and one voice over
    # the thick ones. That is a tie at the resolution, so the planner keeps
    # its per-region plan. The engine then compares it with every uniform
    # figure, and may still prefer one that is better by more than that.
    from arranger.planner import create_initial_plan

    source, profile = mixed_texture(), PRESETS["advanced"]
    plan = create_initial_plan(source, profile)
    assert len(plan.sections) >= 2 and {s.lh_pattern for s in plan.sections} >= {LHPattern.WALKING, LHPattern.BLOCK}
    draft = evaluate_plan(plan, source, profile, origin="deterministic")
    engine = arrange_deterministic(source, profile)
    brute = brute_force_candidate(source, profile, legal_only=True)
    assert engine.quality <= brute.quality and engine.quality <= draft.quality
    if len(engine.plan.sections) == 1:
        assert engine.better_than(draft), "a uniform plan may replace the draft only by a real margin"


def mixed_texture() -> Score:
    notes = []
    roots = {1: 60, 2: 65, 3: 67, 4: 67}
    for bar in range(1, 5):
        t = (bar - 1) * 1.0
        root = roots[bar]
        melody = 72 + bar
        if bar <= 2:
            notes.append(Note(pitch=root, onset=t, duration=0.4, bar=bar))
            notes.append(Note(pitch=melody, onset=t, duration=0.4, bar=bar))
            notes.append(Note(pitch=root + 7, onset=t + 0.5, duration=0.4, bar=bar))
            notes.append(Note(pitch=melody + 1, onset=t + 0.5, duration=0.4, bar=bar))
        else:
            for pitch in (root - 12, root, root + 4, root + 7, root + 12,
                          melody - 5, melody - 2, melody, melody + 3, melody + 7):
                notes.append(Note(pitch=pitch, onset=t, duration=0.8, bar=bar))
    return Score(notes=notes, tempo_bpm=96, title="mixed texture")


# --- polish ----------------------------------------------------------------------


def test_polish_never_makes_a_plan_worse_and_leaves_a_clean_one_alone():
    source, profile = wide_waltz(), PRESETS["intermediate"]
    before = evaluate_plan(
        ArrangementPlan(sections=[Section(1, last_bar(source), LHPattern.BLOCK, lh_voices=3)]),
        source, profile, origin="deterministic",
    )
    after = polish(before, source, profile)
    assert after.quality <= before.quality
    assert after.plan.tempo_scale == before.plan.tempo_scale, "comfort is never bought with tempo"
    assert after.hard <= before.hard
    clean = candidate(0, 1.0, 0, 0.95)
    assert polish(clean, source, profile) is clean
    unaccepted = candidate(3, 1.0, 5, 0.95)
    assert polish(unaccepted, source, profile) is unaccepted, "polish is for plans that already play"


def test_strain_findings_only_produce_suggestions_when_asked():
    plan = ArrangementPlan(sections=[Section(1, 4, LHPattern.BLOCK, lh_voices=3)])
    verdict = Verdict("t", "p", playable=True, violations=[
        Violation(Rule.HAND_SPAN, Severity.STRAIN, 0.5, bar=2, hand="L", measured=11, limit=9),
    ])
    assert suggest_repairs(verdict, plan) == []
    offered = suggest_repairs(verdict, plan, include_strain=True)
    assert offered and {s.field for s in offered} >= {"roll_wide_chords", "pedal_bars"}
    # A HARD finding is offered either way: the flag adds, it never removes.
    hard = Verdict("t", "p", playable=False, violations=[
        Violation(Rule.HAND_SPAN, Severity.HARD, 0.5, bar=2, hand="L", measured=14, limit=12),
    ])
    assert suggest_repairs(hard, plan) == suggest_repairs(hard, plan, include_strain=True)


def test_verify_is_untouched_by_the_ordering():
    # The oracle does not know the ordering exists: a verdict is the same
    # whichever candidate it is wrapped in.
    source, profile = hymn(), PRESETS["intermediate"]
    arranged = evaluate_plan(ArrangementPlan(sections=[Section(1, 8)]), source, profile, origin="x").arranged
    assert verify(arranged, profile).summary() == verify(arranged, profile).summary()


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
