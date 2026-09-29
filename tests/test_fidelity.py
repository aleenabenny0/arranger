"""Fidelity scoring, and the cheap outputs it must not reward.

Each test in the "abuse" section is an arrangement that is easy to produce,
trivially playable, and not the piece. If any of them clears the acceptance
floor, the repair loop will eventually find it.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.agent import FIDELITY_FLOOR  # noqa: E402
from arranger.fidelity import WEIGHTS, Fidelity, measure  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import ArrangementPlan, BassMode, LHPattern, Section  # noqa: E402
from arranger.render import render  # noqa: E402
from arranger.timeline import MeterChange, TempoChange, Timeline  # noqa: E402

BARS = 16
TUNE = [72, 74, 76, 77, 79, 77, 76, 74]
ROOTS = [48, 53, 55, 48]          # I IV V I


def source() -> Score:
    """Sixteen bars: a stepwise tune over broken triads, four attacks a bar."""
    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 4, 4)])
    notes = []
    for bar in range(BARS):
        root = ROOTS[bar % 4]
        for k in range(4):
            beat = bar * 4 + k
            notes.append(Note(TUNE[(bar * 4 + k) % len(TUNE)] + (bar % 3), beat * 0.5, 0.5,
                              bar=bar + 1, beat=float(beat), beats=1.0, velocity=90))
            notes.append(Note(root + (0, 7, 4, 7)[k], beat * 0.5, 0.5, bar=bar + 1,
                              beat=float(beat), beats=1.0, velocity=60))
    return Score(notes=notes, tempo_bpm=120, timeline=timeline, title="fixture")


SOURCE = source()


def arranged(pattern=LHPattern.BLOCK, voices=3, **kwargs) -> Score:
    return render(ArrangementPlan(sections=[Section(1, BARS, pattern, lh_voices=voices, **kwargs)]), SOURCE)


def right(score_: Score) -> list[Note]:
    return [n for n in score_.notes if n.staff == 1]


def left(score_: Score) -> list[Note]:
    return [n for n in score_.notes if n.staff == 2]


def with_notes(base: Score, notes: list[Note]) -> Score:
    return Score(notes=notes, tempo_bpm=base.tempo_bpm, timeline=base.timeline)


# --- honest arrangements clear the floor ------------------------------------


@pytest.mark.parametrize(
    "pattern, voices",
    [(LHPattern.BLOCK, 3), (LHPattern.BLOCK, 2), (LHPattern.ALBERTI, 3), (LHPattern.ARPEGGIO, 3),
     (LHPattern.PEDAL_TONE, 1), (LHPattern.STRIDE, 3)],
)
def test_real_arrangements_pass(pattern, voices):
    fidelity = measure(SOURCE, arranged(pattern, voices))
    assert fidelity.melodic_recall == 1.0 and fidelity.rhythm == 1.0 and fidelity.contour == 1.0
    assert fidelity.score() >= FIDELITY_FLOOR, fidelity.summary()


def test_the_source_against_itself_is_perfect_on_the_melody():
    fidelity = measure(SOURCE, SOURCE)
    assert fidelity.melodic_recall == 1.0 and fidelity.rhythm == 1.0


def test_a_simpler_left_hand_scores_lower_but_not_as_a_failure():
    busy = measure(SOURCE, arranged(LHPattern.ALBERTI, 3))
    plain = measure(SOURCE, arranged(LHPattern.PEDAL_TONE, 1))
    assert plain.accompaniment < busy.accompaniment
    assert plain.harmonic_coverage < busy.harmonic_coverage
    assert plain.score() >= FIDELITY_FLOOR


# --- abuse: cheap outputs that must fail ------------------------------------------


def test_no_left_hand_at_all_fails():
    fidelity = measure(SOURCE, arranged(voices=0))
    assert fidelity.accompaniment == 0.0 and fidelity.bass == 0.0
    assert fidelity.harmonic_coverage <= 0.4, "the melody alone must not state the harmony"
    assert fidelity.score() < FIDELITY_FLOOR - 0.2


def test_deleting_the_left_hand_from_a_third_of_the_bars_fails():
    # The first agent run's exploit, reproduced: 87 of 239 bars.
    plan = ArrangementPlan(sections=[Section(1, 10), Section(11, BARS, lh_voices=0)])
    fidelity = measure(SOURCE, render(plan, SOURCE))
    assert fidelity.score() < FIDELITY_FLOOR


def test_one_held_note_for_the_whole_piece_fails():
    base = arranged(voices=0)
    drone = with_notes(base, [*base.notes, Note(36, 0.0, SOURCE.duration(), staff=2, bar=1)])
    fidelity = measure(SOURCE, drone)
    assert fidelity.accompaniment < 0.1
    assert fidelity.score() < FIDELITY_FLOOR - 0.2


def test_a_left_hand_doubling_the_tune_cannot_cover_for_a_missing_right_hand():
    good = arranged()
    swapped = with_notes(good, [replace(n, staff=2, pitch=n.pitch - 12) for n in right(good)] + left(good))
    fidelity = measure(SOURCE, swapped)
    assert fidelity.melodic_recall == 0.0
    assert fidelity.score() < FIDELITY_FLOOR - 0.2


def test_a_transposed_tune_is_not_the_tune():
    good = arranged()
    tritone = with_notes(good, [replace(n, pitch=n.pitch + 6) for n in right(good)] + left(good))
    assert measure(SOURCE, tritone).melodic_recall == 0.0
    # Near miss: an octave is fine, it is the same tune.
    octave = with_notes(good, [replace(n, pitch=n.pitch - 12) for n in right(good)] + left(good))
    assert measure(SOURCE, octave).melodic_recall == 1.0


def test_right_notes_at_the_wrong_time_are_not_the_tune():
    good = arranged()
    late = with_notes(good, [replace(n, onset=n.onset + 0.2) for n in right(good)] + left(good))
    fidelity = measure(SOURCE, late)
    assert fidelity.melodic_recall == 0.0 and fidelity.rhythm == 0.0
    # Near miss: a performer's 20ms is still the same moment.
    human = with_notes(good, [replace(n, onset=n.onset + 0.02) for n in right(good)] + left(good))
    assert measure(SOURCE, human).melodic_recall == 1.0


def test_padding_the_right_hand_with_extra_notes_costs_rhythm():
    good = arranged()
    extra = [Note(84, n.onset + 0.25, 0.1, staff=1, bar=n.bar) for n in right(good)]
    fidelity = measure(SOURCE, with_notes(good, [*good.notes, *extra]))
    assert fidelity.melodic_recall == 1.0, "every real note is still there"
    assert fidelity.rhythm == pytest.approx(2 / 3, abs=0.02), "but half the attacks are invented"


def test_dropping_half_the_tune_costs_melody_and_rhythm():
    good = arranged()
    thinned = with_notes(good, right(good)[::2] + left(good))
    fidelity = measure(SOURCE, thinned)
    assert fidelity.melodic_recall == pytest.approx(0.5, abs=0.02)
    assert fidelity.score() < FIDELITY_FLOOR


def test_folding_that_inverts_the_line_costs_contour():
    good = arranged()
    flipped = [replace(n, pitch=n.pitch - 12 if i % 2 else n.pitch) for i, n in enumerate(right(good))]
    fidelity = measure(SOURCE, with_notes(good, flipped + left(good)))
    assert fidelity.melodic_recall == 1.0, "pitch classes all survive"
    assert fidelity.contour < 0.6
    assert measure(SOURCE, good).contour == 1.0


def test_wrong_chords_cost_harmony_and_bass():
    good = arranged()
    wrong = with_notes(good, right(good) + [replace(n, pitch=n.pitch + 1) for n in left(good)])
    fidelity = measure(SOURCE, wrong)
    assert fidelity.harmonic_coverage < 0.5 and fidelity.bass < 0.2
    assert fidelity.accompaniment > 0.7, "there is a left hand; it is just wrong"
    assert fidelity.score() < FIDELITY_FLOOR


def test_keeping_the_real_bass_is_rewarded_over_the_root():
    # Source in first inversion: E in the bass under C major.
    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 4, 4)])
    notes = []
    for bar in range(4):
        t = bar * 2.0
        notes += [Note(p, t, 2.0, bar=bar + 1, beat=bar * 4.0, beats=4.0) for p in (52, 60, 67, 76)]
    inverted = Score(notes=notes, timeline=timeline)

    def bass_score(mode):
        plan = ArrangementPlan(sections=[Section(1, 4, bass=mode)])
        return measure(inverted, render(plan, inverted)).bass

    assert bass_score(BassMode.SOURCE) == 1.0
    assert bass_score(BassMode.ROOT) == pytest.approx(0.7)


# --- the number itself ----------------------------------------------------------------


def test_weights_sum_to_one_and_the_scale_is_anchored():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
    assert Fidelity(1.0, 1.0, 1.0).score() == pytest.approx(1.0)
    assert Fidelity(0.0, 0.0, 0.0, 0.0, 0.0, 0.0).score() == 0.0
    # Code written against the three-field version still means the same thing.
    assert Fidelity(FIDELITY_FLOOR, FIDELITY_FLOOR, FIDELITY_FLOOR).score() >= FIDELITY_FLOOR


def test_empty_inputs_do_not_crash():
    assert measure(Score(notes=[]), SOURCE).score() == 0.0
    assert measure(SOURCE, Score(notes=[])).score() == 0.0


def test_weakest_names_what_to_fix():
    name, value = measure(SOURCE, arranged(voices=0)).weakest()
    assert name in ("accompaniment", "harmonic_coverage") and value < 0.5


def test_scoring_a_long_piece_is_fast():
    import time

    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 4, 4)])
    notes = []
    for bar in range(800):
        for k in range(4):
            beat = bar * 4 + k
            notes.append(Note(72 + (beat % 7), beat * 0.5, 0.5, bar=bar + 1, beat=float(beat), beats=1.0))
            notes.append(Note(48 + (0, 7, 4, 7)[k], beat * 0.5, 0.5, bar=bar + 1, beat=float(beat), beats=1.0))
    long_source = Score(notes=notes, timeline=timeline)
    out = render(ArrangementPlan(sections=[Section(1, 800)]), long_source)
    started = time.perf_counter()
    measure(long_source, out)
    assert time.perf_counter() - started < 3.0
