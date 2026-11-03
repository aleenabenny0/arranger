"""The phrase-based hand solver.

Follows the house rule for `verify/`: every rule has a violating case and a
near miss that must stay clean. False positives are the expensive failure.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.ir import Note, PedalSpan, Score  # noqa: E402
from arranger.profile import (  # noqa: E402
    PRESETS,
    Calibration,
    PlayerProfile,
    profile_from_calibration,
)
from arranger.verify import (  # noqa: E402
    Certainty,
    Rule,
    Severity,
    SolverStatus,
    solve_hands,
    verify,
)
from arranger.verify.constraints import with_assumed_pedal  # noqa: E402
from arranger.verify.solver import chord_fits_fingers  # noqa: E402

STD = PRESETS["intermediate"]
FAST = PlayerProfile(name="fast", max_leap_rate=1e6)   # takes leaps out of the picture


def found(verdict, rule, severity=Severity.HARD):
    return [v for v in verdict.violations if v.rule == rule and v.severity == severity]


def score(rows, pedals=()):
    """rows: (pitch, onset, duration[, staff[, kwargs]])"""
    notes = []
    for i, row in enumerate(rows):
        staff = row[3] if len(row) > 3 else None
        kwargs = row[4] if len(row) > 4 else {}
        notes.append(Note(row[0], float(row[1]), float(row[2]), staff=staff, bar=1, id=f"n{i}", **kwargs))
    return Score(notes=notes, pedals=[PedalSpan(*p) for p in pedals])


# --- looking ahead -----------------------------------------------------------


def test_the_whole_phrase_is_considered_not_one_instant():
    # Left hand plays a low note; 300ms later a note arrives 18 semitones up.
    # The greedy assigner compares distances only: 18 to move the left hand,
    # 24 to wake the right, so it keeps the left, and the left cannot make it
    # (budget is 5 + 40 * 0.3 = 17). The phrase search sees that the right hand
    # is free, takes the note there, and is then well placed for the chord.
    slow = PlayerProfile(name="t", max_leap_rate=40.0)
    rows = [(48, 0.0, 0.1), (66, 0.3, 0.1), (84, 1.0, 0.5), (88, 1.0, 0.5), (91, 1.0, 0.5)]

    from arranger.verify.hands import assign_hands

    greedy, _ = assign_hands([Note(66, 0.3, 0.1)], slow, prev=(48.0, None))
    assert greedy == {0: "L"}, "the per-instant choice this test exists to beat"

    solution = solve_hands(score(rows), slow)
    assert solution.status == SolverStatus.FEASIBLE
    assert [solution.hands[i] for i in range(5)] == ["L", "R", "R", "R", "R"]


def test_a_held_note_keeps_its_hand():
    # Left hand holds a bass note. While it sounds, a note right next to it
    # arrives: it may join the left hand, but the bass cannot hop to the right.
    rows = [(40, 0.0, 2.0), (76, 0.0, 0.4), (43, 0.5, 0.4), (79, 1.0, 0.4)]
    solution = solve_hands(score(rows), STD)
    assert solution.hands[0] == "L" and solution.hands[2] == "L"
    assert solution.hands[1] == "R" and solution.hands[3] == "R"
    assert solution.status == SolverStatus.FEASIBLE


# --- crossing ------------------------------------------------------------------


def test_crossing_is_allowed_and_flagged_as_strain():
    rows = [(60, 0.0, 1.0, 1), (72, 0.0, 1.0, 2)]      # left hand written above the right
    verdict = verify(score(rows), STD)
    assert verdict.playable
    assert found(verdict, Rule.HAND_CROSSING, Severity.STRAIN)


def test_side_by_side_hands_are_not_a_crossing():
    verdict = verify(score([(60, 0.0, 1.0, 2), (72, 0.0, 1.0, 1)]), STD)
    assert not found(verdict, Rule.HAND_CROSSING, Severity.STRAIN)


# --- per-hand limits and fingers ---------------------------------------------------


def test_each_hand_has_its_own_reach():
    small_left = PlayerProfile(name="t", left_max_span=9, left_comfortable_span=7, max_leap_rate=1e6)
    wide_left = score([(48, 0.0, 1.0, 2), (59, 0.0, 1.0, 2)])       # 11 semitones
    wide_right = score([(60, 0.0, 1.0, 1), (71, 0.0, 1.0, 1)])
    hard = found(verify(wide_left, small_left), Rule.HAND_SPAN)
    assert hard and hard[0].limit == 9 and hard[0].hand == "L"
    assert verify(wide_right, small_left).playable, "the right hand still reaches an octave"


def test_fewer_fingers_means_fewer_notes():
    three = PlayerProfile(name="t", right_fingers=(1, 2, 3), max_leap_rate=1e6)
    four_notes = score([(60 + 2 * i, 0.0, 1.0, 1) for i in range(4)])
    hard = found(verify(four_notes, three), Rule.HAND_POLYPHONY)
    assert hard and hard[0].limit == 3
    assert verify(score([(60 + 2 * i, 0.0, 1.0, 1) for i in range(3)]), three).playable
    assert verify(four_notes, STD).playable


def test_a_chord_inside_the_span_can_still_not_fit_the_fingers():
    # C, C#, B, C: an octave wide, but the two inner notes are ten semitones
    # apart and must be taken by neighbouring fingers.
    awkward = [60, 61, 71, 72]
    assert not chord_fits_fingers(awkward, (1, 2, 3, 4, 5), 12, "R")
    assert chord_fits_fingers([60, 64, 67, 72], (1, 2, 3, 4, 5), 12, "R")
    verdict = verify(score([(p, 0.0, 1.0, 1) for p in awkward]), FAST)
    assert verdict.playable, "new rules are STRAIN until checked against the corpus"
    assert found(verdict, Rule.FINGER_STRETCH, Severity.STRAIN)
    assert not found(verify(score([(p, 0.0, 1.0, 1) for p in (60, 64, 67, 72)]), FAST),
                     Rule.FINGER_STRETCH, Severity.STRAIN)


def test_finger_model_is_mirrored_for_the_left_hand():
    # Thumb on top in the left hand: a wide gap is fine at the top (thumb to
    # index), not at the bottom (ring to little finger).
    assert chord_fits_fingers([48, 50, 52, 60], (1, 2, 3, 4, 5), 12, "L")
    assert not chord_fits_fingers([48, 56, 58, 60], (1, 2, 3, 4, 5), 12, "L")


# --- pedal ------------------------------------------------------------------------


BASS_THEN_CHORD = [(36, 0.0, 2.0, 2), (55, 1.0, 1.0, 2), (59, 1.0, 1.0, 2), (62, 1.0, 1.0, 2)]


def test_a_held_bass_blocks_the_hand_without_pedal():
    hard = found(verify(score(BASS_THEN_CHORD), STD), Rule.HAND_SPAN)
    assert hard and hard[0].measured == 26 and hard[0].note_ids == ["n0", "n1", "n2", "n3"]


def test_the_pedal_lets_the_hand_leave_a_held_bass():
    assert verify(score(BASS_THEN_CHORD, pedals=[(0.0, 2.0)]), STD).playable


def test_pedal_pressed_too_late_to_catch_the_note_does_not_help():
    # Pedal goes down at the very instant of the chord: the bass was never caught.
    verdict = verify(score(BASS_THEN_CHORD, pedals=[(1.0, 2.0)]), STD)
    assert found(verdict, Rule.HAND_SPAN)


def test_pedalled_notes_are_not_fingers():
    arpeggio = [(36 + 4 * i, i * 0.1, 3.0) for i in range(12)]
    assert found(verify(score(arpeggio), FAST), Rule.TOTAL_POLYPHONY)
    assert not found(verify(score(arpeggio, pedals=[(0.0, 3.0)]), FAST), Rule.TOTAL_POLYPHONY)


def test_assumed_pedal_is_explicit_and_keeps_real_marks():
    literal = verify(score(BASS_THEN_CHORD), STD)
    assumed = verify(score(BASS_THEN_CHORD), STD, assume_pedal=True)
    assert not literal.playable and assumed.playable
    marked = with_assumed_pedal(score(BASS_THEN_CHORD, pedals=[(0.2, 0.4)]))
    assert PedalSpan(0.2, 0.4) in marked.pedals and len(marked.pedals) == 2


# --- rolled chords ----------------------------------------------------------------------


def rolled_chord(span, stagger=0.03, rolled=True):
    pitches = [48, 48 + span // 2, 48 + span]
    return score([(p, i * stagger, 1.0, 2, {"rolled": rolled}) for i, p in enumerate(pitches)])


def test_rolling_buys_reach():
    assert found(verify(rolled_chord(16, rolled=False), FAST), Rule.HAND_SPAN), "a tenth as a block"
    assert verify(rolled_chord(16), FAST).playable, "the same tenth, rolled"


def test_rolling_is_not_unlimited():
    hard = found(verify(rolled_chord(19), FAST), Rule.HAND_SPAN)
    assert hard and hard[0].limit == 17 and "rolled" in hard[0].message
    pedalled = rolled_chord(19)
    pedalled.pedals = [PedalSpan(0.0, 1.0)]
    assert verify(pedalled, FAST).playable, "with pedal the hand can travel during the roll"


# --- strain rules: repetition and legato ---------------------------------------------------


def test_fast_repetition_is_strain():
    quick = score([(60, i * 0.08, 0.06, 1) for i in range(4)])       # 12.5 per second
    steady = score([(60, i * 0.2, 0.15, 1) for i in range(4)])       # 5 per second
    assert found(verify(quick, STD), Rule.FAST_REPETITION, Severity.STRAIN)
    assert verify(quick, STD).playable
    assert not found(verify(steady, STD), Rule.FAST_REPETITION, Severity.STRAIN)


def test_a_leap_out_of_a_long_note_breaks_the_line_unless_pedalled():
    rows = [(48, 0.0, 1.0, 2), (72, 1.0, 0.5, 2)]    # two octaves, no gap between the notes
    slow = PlayerProfile(name="t", max_leap_rate=40.0)
    verdict = verify(score(rows), slow)
    assert verdict.playable
    broken = found(verdict, Rule.LEGATO_BREAK, Severity.STRAIN)
    assert broken and broken[0].measured == pytest.approx(475, abs=1)
    assert not found(verify(score(rows, pedals=[(0.0, 1.5)]), slow), Rule.LEGATO_BREAK, Severity.STRAIN)
    gap = [(48, 0.0, 0.4, 2), (72, 1.0, 0.5, 2)]     # near miss: the note ends early anyway
    assert not found(verify(score(gap), slow), Rule.LEGATO_BREAK, Severity.STRAIN)


# --- what a result means -------------------------------------------------------------------


def test_feasible_infeasible_and_unknown_are_different_claims():
    fine = verify(score([(60, 0.0, 1.0), (64, 0.0, 1.0)]), STD)
    assert fine.solver_status == SolverStatus.FEASIBLE

    forced = verify(score([(48, 0.0, 1.0, 2), (69, 0.0, 1.0, 2)]), STD)
    assert forced.solver_status == SolverStatus.INFEASIBLE
    assert all(v.certainty == Certainty.PROVEN for v in forced.hard)


def test_a_search_that_was_cut_short_never_claims_proof():
    # Eleven free notes in one instant exceed ten fingers however they are split,
    # but with the search narrowed to one state and branches discarded, the
    # solver cannot know that and must say so.
    rows = [(60 + (i * 7) % 24, i * 0.05, 0.04) for i in range(30)]
    rows += [(40 + 3 * i, 2.0, 1.0) for i in range(7)]
    narrowed = solve_hands(score(rows), PRESETS["beginner"], beam_width=1)
    assert narrowed.narrowed
    if narrowed.status != SolverStatus.FEASIBLE:
        hard = [v for v in narrowed.violations if v.severity == Severity.HARD]
        assert narrowed.status == SolverStatus.UNKNOWN
        assert hard and all(v.certainty == Certainty.UNPROVEN for v in hard)

    timed = solve_hands(score(rows), PRESETS["beginner"], time_budget=0.0)
    assert timed.timed_out
    assert timed.status in (SolverStatus.FEASIBLE, SolverStatus.UNKNOWN)


def test_pruning_does_not_forfeit_a_proof_it_can_still_make():
    # Every branch that gets dropped is already worse than the answer, so the
    # result is still proven even though the search was narrowed.
    rows = [(48, 0.0, 1.0, 2), (69, 0.0, 1.0, 2)] + [(60 + i % 5, 1.0 + i * 0.1, 0.1) for i in range(30)]
    solution = solve_hands(score(rows), STD, beam_width=2)
    assert solution.status == SolverStatus.INFEASIBLE


def test_range_failure_is_proven_whatever_the_search_did():
    verdict = verify(score([(12, 0.0, 1.0)]), STD)
    assert not verdict.playable and verdict.solver_status == SolverStatus.INFEASIBLE


# --- phrases ----------------------------------------------------------------------------------


def test_a_long_silence_starts_a_new_phrase():
    rows = [(36, 0.0, 0.5, 2), (96, 5.0, 0.5, 2)]     # five octaves, but five seconds to get there
    solution = solve_hands(score(rows), STD)
    assert solution.phrases == 2 and solution.status == SolverStatus.FEASIBLE


def test_a_short_silence_does_not():
    rows = [(36, 0.0, 0.05, 2), (96, 0.1, 0.5, 2)]
    assert [v.rule for v in solve_hands(score(rows), STD).violations if v.severity == Severity.HARD] == [
        Rule.LEAP_INFEASIBLE
    ]


# --- imported music ------------------------------------------------------------------------------


def test_staff_is_a_hint_for_imported_music_and_binding_for_ours():
    # Upper-staff chord that only the left hand can take while the right plays the tune.
    rows = [(55, 0.0, 1.0, 1), (59, 0.0, 1.0, 1), (62, 0.0, 1.0, 1), (86, 0.0, 1.0, 1)]
    assert not verify(score(rows), FAST).playable
    relaxed = verify(score(rows), FAST, staff_is_binding=False)
    assert relaxed.playable
    assert relaxed.hands == {"n0": "L", "n1": "L", "n2": "L", "n3": "R"}


def test_staff_hint_is_followed_when_nothing_is_gained_by_ignoring_it():
    rows = [(60, 0.0, 1.0, 2), (64, 0.0, 1.0, 2), (72, 0.0, 1.0, 1)]
    assert verify(score(rows), STD, staff_is_binding=False).hands == {"n0": "L", "n1": "L", "n2": "R"}


# --- profile calibration ------------------------------------------------------------------------------


def test_calibration_turns_measurements_into_a_profile():
    profile = profile_from_calibration(Calibration(
        base_preset="intermediate", name="Me", left_max_white_keys=8, right_max_white_keys=9,
        left_comfortable_white_keys=7, right_comfortable_white_keys=8,
        two_octave_leap_seconds=0.5, repeated_notes_per_second=6, right_fingers=(1, 2, 3, 5),
    ))
    assert (profile.left_max_span, profile.right_max_span, profile.max_span) == (12, 14, 12)
    assert profile.span_limit("R") == 14 and profile.comfortable_limit("L") == 11
    assert profile.max_leap_rate == 38.0 and profile.note_limit("R") == 4
    assert PlayerProfile.from_dict(profile.to_dict()) == profile


@pytest.mark.parametrize(
    "bad",
    [{"left_max_white_keys": 40}, {"two_octave_leap_seconds": 0.0}, {"base_preset": "virtuoso"},
     {"right_fingers": (1, 1, 2)}, {"skill_level": 99}],
)
def test_impossible_calibration_answers_are_refused(bad):
    with pytest.raises(ValueError):
        profile_from_calibration(Calibration(**bad))


def test_profile_loading_is_strict():
    for bad in ({"max_spam": 12}, {"max_span": "12"}, {"max_span": True}, {"left_fingers": [0, 9]}, []):
        with pytest.raises(ValueError):
            PlayerProfile.from_dict(bad)


# --- cost ------------------------------------------------------------------------------------------------


def test_a_long_arrangement_verifies_quickly():
    rows = []
    for i in range(3000):
        rows.append((72 + (i % 7), i * 0.25, 0.25, 1))
        if i % 4 == 0:
            rows += [(48, i * 0.25, 1.0, 2), (55, i * 0.25, 1.0, 2)]
    started = time.perf_counter()
    verdict = verify(score(rows), STD)
    assert time.perf_counter() - started < 3.0
    assert verdict.playable
