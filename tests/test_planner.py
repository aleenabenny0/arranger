"""Deterministic planner tests.

Runs under pytest, or standalone: `python tests/test_planner.py`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.fidelity import measure  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import ArrangementPlan, LHPattern, Section  # noqa: E402
from arranger.planner import (  # noqa: E402
    analyze_score,
    candidate_ranking_rows,
    create_initial_plan,
    detect_regions,
    rank_regions_by_difficulty,
    score_region_difficulty,
)
from arranger.profile import PRESETS  # noqa: E402
from arranger.render import last_bar, render  # noqa: E402
from arranger.verify import verify  # noqa: E402


def source_with_regions() -> Score:
    notes = []
    for bar in range(1, 7):
        t = (bar - 1) * 1.2
        root = 60 if bar <= 3 else 67
        melody = 72 + (bar % 3) * 2
        for pitch in (root, root + 4, root + 7, melody):
            notes.append(Note(pitch=pitch, onset=t, duration=0.8, bar=bar))
        if bar >= 4:
            notes.append(Note(pitch=melody + 3, onset=t + 0.3, duration=0.25, bar=bar))
            notes.append(Note(pitch=melody + 5, onset=t + 0.6, duration=0.25, bar=bar))
    return Score(notes=notes, tempo_bpm=132, title="regions")


def wide_melody_source() -> Score:
    return Score(
        notes=[
            Note(pitch=48, onset=0.0, duration=0.8, bar=1),
            Note(pitch=52, onset=0.0, duration=0.8, bar=1),
            Note(pitch=55, onset=0.0, duration=0.8, bar=1),
            *[
                Note(pitch=60 + 4 * i, onset=i * 0.35, duration=0.25, bar=1)
                for i in range(8)
            ],
        ],
        tempo_bpm=140,
        title="wide melody",
    )


def mixed_texture_source() -> Score:
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
            for pitch in (
                root - 12,
                root,
                root + 4,
                root + 7,
                root + 12,
                melody - 5,
                melody - 2,
                melody,
                melody + 3,
                melody + 7,
            ):
                notes.append(Note(pitch=pitch, onset=t, duration=0.8, bar=bar))
    return Score(notes=notes, tempo_bpm=96, title="mixed texture")


def plan_cost(source: Score, profile) -> float:
    plan = create_initial_plan(source, profile)
    arranged = render(plan, source)
    verdict = verify(arranged, profile)
    fidelity = measure(source, arranged)
    return len(verdict.hard) + max(0.0, 0.88 - fidelity.score()) * 60


def test_analyze_score_returns_one_feature_per_bar():
    features = analyze_score(source_with_regions())
    assert [f.bar for f in features] == [1, 2, 3, 4, 5, 6]
    assert all(f.chord_root is not None for f in features)
    assert features[-1].note_count > features[0].note_count


def test_initial_plan_covers_every_bar_and_validates():
    source = source_with_regions()
    plan = create_initial_plan(source, PRESETS["intermediate"])
    assert not plan.validate()
    assert plan.sections[0].start_bar == 1
    assert plan.sections[-1].end_bar == last_bar(source)
    assert len(plan.sections) <= 10


def test_region_detection_splits_chord_and_texture_changes():
    regions = detect_regions(source_with_regions(), PRESETS["intermediate"])
    assert len(regions) >= 2
    assert any(region.start_bar == 4 for region in regions)


def test_region_difficulty_scores_actual_playing_demand():
    source = mixed_texture_source()
    profile = PRESETS["advanced"]
    regions = detect_regions(source, profile)
    easy = score_region_difficulty(regions[0], profile, source.tempo_bpm)
    hard = score_region_difficulty(regions[-1], profile, source.tempo_bpm)

    assert hard.score > easy.score
    assert hard.rank in {"challenging", "demanding", "extreme"}
    assert "thick simultaneities" in hard.reasons


def test_regions_can_be_ranked_by_difficulty():
    ranked = rank_regions_by_difficulty(mixed_texture_source(), PRESETS["advanced"])
    assert ranked[0][0].start_bar == 3
    assert ranked[0][1].score >= ranked[-1][1].score


def test_per_section_scoring_can_choose_different_patterns():
    plan = create_initial_plan(mixed_texture_source(), PRESETS["advanced"])
    decisions = {
        (section.lh_pattern, section.lh_voices, section.melody_fold_window)
        for section in plan.sections
    }
    assert len(plan.sections) >= 2
    assert len(decisions) >= 2
    assert any(section.lh_pattern == LHPattern.WALKING for section in plan.sections)
    assert any("extreme" in section.label for section in plan.sections)
    assert any("climax reduction" in section.label for section in plan.sections)


def test_candidate_rankings_are_ml_ready():
    rows = candidate_ranking_rows(mixed_texture_source(), PRESETS["advanced"])
    assert rows
    assert any(row.chosen for row in rows)
    assert all(row.pattern and row.difficulty_rank for row in rows)
    assert all(0 <= row.profile_fit <= 1 for row in rows)


def test_wide_melody_gets_octave_folding():
    plan = create_initial_plan(wide_melody_source(), PRESETS["beginner"])
    assert any(section.melody_fold_window for section in plan.sections)


def test_beginner_fast_dense_music_uses_safe_left_hand():
    plan = create_initial_plan(source_with_regions(), PRESETS["beginner"])
    assert all(
        section.lh_pattern in {LHPattern.PEDAL_TONE, LHPattern.BLOCK}
        for section in plan.sections
    )
    assert max(section.lh_voices for section in plan.sections) <= 2


def test_initial_plan_is_no_worse_than_basic_block_plan():
    source = source_with_regions()
    profile = PRESETS["beginner"]
    deterministic = plan_cost(source, profile)
    basic = ArrangementPlan(sections=[Section(1, last_bar(source), LHPattern.BLOCK)])
    arranged = render(basic, source)
    verdict = verify(arranged, profile)
    fidelity = measure(source, arranged)
    basic_cost = len(verdict.hard) + max(0.0, 0.88 - fidelity.score()) * 60
    assert deterministic <= basic_cost


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


# --- skill ceilings, tempo gates, bounded work ---------------------------------------

from dataclasses import replace  # noqa: E402

from arranger import planner  # noqa: E402


def _region(bars: int = 4, density: int = 1) -> planner.Region:
    """A region described directly by its bar features: `density` notes per onset."""
    features = tuple(
        planner.BarFeatures(
            bar=bar, note_count=4 * density, onset_count=4, max_simultaneous=density, min_onset_gap=0.5,
            chord_root=(0, 5, 7, 0)[(bar - 1) % 4], chord_quality="maj", melody_low=64, melody_high=71, melody_leap=3,
        )
        for bar in range(1, bars + 1)
    )
    return planner.Region(1, bars, features)


def _calm_region(bars: int = 4) -> planner.Region:
    return _region(bars, density=1)


def _patterns(region, profile, tempo):
    return {c.pattern for c in planner._region_candidates(region, profile, tempo)}


def test_no_candidate_is_ever_above_the_skill_ceiling():
    from arranger.plan import patterns_for_skill

    region = _calm_region()
    for level in range(1, 11):
        profile = replace(PRESETS["advanced"], skill_level=level)
        for tempo in (50, 72, 100, 132, 176):
            offered = _patterns(region, profile, tempo)
            assert offered <= set(patterns_for_skill(level)), (level, tempo, offered)
            assert offered, "there is always at least the safest figure"


def test_broken_tenths_are_offered_slow_and_not_at_speed():
    profile = replace(PRESETS["advanced"], skill_level=8)
    region = _calm_region()
    assert LHPattern.BROKEN_TENTH in _patterns(region, profile, 72)
    # Near miss: exactly on the gate is still allowed; just over it is not.
    assert LHPattern.BROKEN_TENTH in _patterns(region, profile, planner.BROKEN_TENTH_MAX_BPM)
    assert LHPattern.BROKEN_TENTH not in _patterns(region, profile, planner.BROKEN_TENTH_MAX_BPM + 1)
    # One level below the ceiling never sees it, however slow the piece.
    assert LHPattern.BROKEN_TENTH not in _patterns(region, replace(profile, skill_level=6), 60)


def test_stride_needs_a_hand_that_can_make_the_leap_in_one_beat():
    region = _calm_region()
    quick = replace(PRESETS["advanced"], skill_level=8, max_leap_rate=120.0, leap_slack=5)
    # 120 semitones a second covers 24 semitones in a beat up to a very fast tempo.
    assert LHPattern.STRIDE in _patterns(region, quick, 120)
    slow_hand = replace(quick, max_leap_rate=30.0)
    # 30 a second plus 5 of slack reaches 24 at 94 bpm and below, and not above.
    assert planner.stride_is_reachable(slow_hand, 94)
    assert not planner.stride_is_reachable(slow_hand, 96)
    assert LHPattern.STRIDE in _patterns(region, slow_hand, 90)
    assert LHPattern.STRIDE not in _patterns(region, slow_hand, 132)
    # A dense region never gets stride, whatever the hand can do.
    dense = _region(4, density=5)
    assert LHPattern.STRIDE not in _patterns(dense, quick, 100)


def test_a_stride_or_tenth_plan_is_only_kept_when_the_verifier_accepts_it():
    from arranger.verify import verify as check

    profile = replace(PRESETS["advanced"], skill_level=9)
    source = mixed_texture_source()
    plan = create_initial_plan(source, profile)
    assert plan.validate_for_source(last_bar(source), skill_level=profile.skill_level) == []
    arranged = render(plan, source)
    # Whatever figures won, they won by not adding hard findings.
    block = ArrangementPlan(title="block", target_skill=profile.skill_level,
                            sections=[Section(1, last_bar(source), LHPattern.BLOCK, lh_voices=2)])
    baseline = render(block, source)
    assert len(check(arranged, profile).hard) <= len(check(baseline, profile).hard)


def test_a_planner_told_to_stop_still_returns_a_complete_plan():
    source = mixed_texture_source()
    profile = PRESETS["intermediate"]
    asked = []

    def stop():
        asked.append(1)
        return True

    plan = create_initial_plan(source, profile, stop=stop)
    assert asked, "the planner must poll the stop signal"
    assert plan.validate_for_source(last_bar(source), skill_level=profile.skill_level) == []
    covered = sorted(bar for s in plan.sections for bar in range(s.start_bar, s.end_bar + 1))
    assert covered == list(range(1, last_bar(source) + 1))


def test_stopping_late_gives_the_same_plan_as_not_stopping():
    # Near miss for the stop signal: one that never fires must change nothing.
    source = mixed_texture_source()
    profile = PRESETS["intermediate"]
    assert create_initial_plan(source, profile, stop=lambda: False).to_json() == create_initial_plan(source, profile).to_json()

