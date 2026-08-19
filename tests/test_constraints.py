"""Constraint tests.

Runs under pytest, or standalone: `python tests/test_constraints.py`

The negative tests (the ones asserting *no* violation) matter more than the
positive ones. A verifier that flags everything is useless in a way that is
hard to notice, because the agent will dutifully "fix" music that was fine.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.ir import Score  # noqa: E402
from arranger.profile import PlayerProfile, PRESETS  # noqa: E402
from arranger.verify import verify  # noqa: E402
from arranger.verify.verdict import Rule, Severity  # noqa: E402

STD = PRESETS["intermediate"]  # max_span 12, comfortable 9, 5 fingers


def rules(verdict, severity=Severity.HARD):
    return {str(v.rule) for v in verdict.violations if v.severity == severity}


# --- range ---------------------------------------------------------------

def test_note_below_keyboard_is_hard():
    score = Score.from_tuples([(12, 0.0, 1.0)])  # C0, below A0
    assert Rule.RANGE in rules(verify(score, STD))


def test_notes_inside_range_are_clean():
    score = Score.from_tuples([(60, 0.0, 1.0), (64, 0.0, 1.0), (67, 0.0, 1.0)])
    assert verify(score, STD).playable


# --- hand span -----------------------------------------------------------

def test_thirteenth_in_one_hand_is_hard():
    # C3 + A4 = 21 semitones. Staff 2 forces both into the left hand; without
    # that hint the solver would sensibly give one note to each hand, which is
    # what a pianist would do and is *not* a violation. See the test below.
    score = Score.from_tuples([(48, 0.0, 1.0, 2), (69, 0.0, 1.0, 2)])
    v = verify(score, PlayerProfile(name="t", max_span=12, max_leap_rate=1e6))
    assert not v.playable
    assert Rule.HAND_SPAN in rules(v)


def test_octave_is_fine():
    score = Score.from_tuples([(60, 0.0, 1.0), (72, 0.0, 1.0)])
    assert verify(score, STD).playable


def test_tenth_is_strain_not_hard_for_standard_hands():
    # 11 semitones: past comfortable (9), inside max (12). Forced to one hand.
    score = Score.from_tuples([(60, 0.0, 1.0, 1), (71, 0.0, 1.0, 1)])
    v = verify(score, STD)
    assert v.playable
    assert Rule.HAND_SPAN in rules(v, Severity.STRAIN)


def test_wide_spread_splits_across_two_hands():
    # C3 and C5 are 24 apart but trivially one per hand.
    score = Score.from_tuples([(48, 0.0, 1.0), (72, 0.0, 1.0)])
    assert verify(score, STD).playable


def test_explicit_staff_is_respected_even_when_it_is_wrong():
    # Both notes forced into the left hand: 21 semitones. The verifier must
    # report this rather than silently rescuing the arranger's bad choice.
    score = Score.from_tuples([(48, 0.0, 1.0, 2), (69, 0.0, 1.0, 2)])
    assert Rule.HAND_SPAN in rules(verify(score, STD))


# --- polyphony -----------------------------------------------------------

def test_six_notes_in_one_hand_is_hard():
    score = Score.from_tuples([(60 + i, 0.0, 1.0, 1) for i in range(6)])
    assert Rule.HAND_POLYPHONY in rules(verify(score, STD))


def test_eleven_simultaneous_notes_exceeds_ten_fingers():
    score = Score.from_tuples([(48 + 2 * i, 0.0, 1.0) for i in range(11)])
    assert Rule.TOTAL_POLYPHONY in rules(verify(score, STD))


# --- legato boundary -----------------------------------------------------

def test_note_ending_as_next_begins_is_not_simultaneous():
    # The half-open-interval rule. Without it, every scale reports violations.
    score = Score.from_tuples([(48, 0.0, 1.0), (69, 1.0, 1.0)])
    v = verify(score, PlayerProfile(name="t", max_leap_rate=1e6))
    assert Rule.HAND_SPAN not in rules(v)


# --- leaps ---------------------------------------------------------------

def test_two_octave_leap_in_20ms_is_infeasible():
    # Same hand (staff 2), so there is no free hand to rescue the leap.
    score = Score.from_tuples([(36, 0.0, 0.02, 2), (60, 0.02, 0.5, 2)])
    v = verify(score, PlayerProfile(name="t", max_leap_rate=70.0, leap_slack=5))
    assert Rule.LEAP_INFEASIBLE in rules(v)


def test_lone_line_stays_in_one_hand_and_its_leaps_are_checked():
    # REVERSED in M2. This test previously asserted the opposite: that a fast
    # wide leap should be excused because the other hand is idle and could
    # take it. That rule let a single melodic line drift into whichever hand
    # was momentarily nearer, and Fur Elise showed the cost — 58 phantom leap
    # violations from a melody being relabelled back and forth.
    #
    # Charging a price to wake a resting hand fixed that, at the cost of no
    # longer excusing genuine two-hand rescues in isolated fragments. That
    # trade is worth it: a per-instant solver cannot tell whether the other
    # hand is free over the whole phrase, only at this moment. The proper
    # answer is the global solver in M3.
    score = Score.from_tuples([(36, 0.0, 0.02), (60, 0.02, 0.5)])
    v = verify(score, PlayerProfile(name="t", max_leap_rate=70.0, leap_slack=5))
    assert Rule.LEAP_INFEASIBLE in rules(v)


def test_resting_hand_gets_credit_for_the_time_it_rested():
    # The M2 timing fix. The left hand plays a low note, rests while the right
    # hand plays three fast notes, then moves. It had the whole rest to move,
    # not just the 20ms since the most recent right-hand note.
    score = Score.from_tuples([
        (36, 0.00, 0.10, 2),
        (72, 0.10, 0.02, 1), (74, 0.12, 0.02, 1), (76, 0.14, 0.02, 1),
        (48, 0.60, 0.20, 2),
    ])
    v = verify(score, PlayerProfile(name="t", max_leap_rate=50.0, leap_slack=5))
    assert Rule.LEAP_INFEASIBLE not in rules(v)


def test_same_leap_with_a_full_second_is_fine():
    score = Score.from_tuples([(36, 0.0, 1.0, 2), (60, 1.0, 1.0, 2)])
    v = verify(score, PlayerProfile(name="t", max_leap_rate=70.0, leap_slack=5))
    assert Rule.LEAP_INFEASIBLE not in rules(v)


def test_stepwise_melody_never_trips_the_leap_rule():
    score = Score.from_tuples([(60 + i, i * 0.15, 0.15) for i in range(8)])
    assert verify(score, STD).playable


# --- profiles ------------------------------------------------------------

def test_beginner_profile_rejects_what_advanced_accepts():
    # Five-note voicing spanning two octaves. Advanced hands split it 2+3
    # comfortably; a beginner (3 fingers, 9-semitone span) cannot.
    score = Score.from_tuples(
        [(48, 0.0, 0.5), (60, 0.0, 0.5), (64, 0.0, 0.5), (67, 0.0, 0.5), (72, 0.0, 0.5)]
    )
    assert verify(score, PRESETS["advanced"]).playable
    assert not verify(score, PRESETS["beginner"]).playable


def test_violations_carry_actionable_numbers():
    score = Score.from_tuples([(48, 0.0, 1.0, 2), (69, 0.0, 1.0, 2)])
    v = next(x for x in verify(score, STD).violations if x.rule == Rule.HAND_SPAN)
    assert v.measured == 21 and v.limit == 12
    assert v.pitches == [48, 69] and v.hand == "L"
    assert v.message  # the repair agent needs prose too, not just numbers


def test_nonsense_profiles_are_rejected():
    assert PlayerProfile(name="x", comfortable_span=20, max_span=12).validate()
    assert not PlayerProfile(name="x").validate()


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
