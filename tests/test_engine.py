"""Analysis and rendering behaviour added on top of the original renderer.

`tests/test_render.py` pins the original contracts (determinism, melody never
dropped, invalid plans refused). This file pins what the engine learned since:
finding a tune that is not simply on top, hearing chord changes inside a bar,
keeping the real bass, following the meter, and moving the hand less.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.analysis import (  # noqa: E402
    bass_line,
    detect_chord_segments,
    extract_melody,
    extract_melody_skyline,
)
from arranger.ir import Note, Score, TrackInfo  # noqa: E402
from arranger.plan import (  # noqa: E402
    ArrangementPlan,
    BassMode,
    HarmonicRhythm,
    LHPattern,
    Section,
    Voicing,
    patterns_for_skill,
)
from arranger.render import render  # noqa: E402
from arranger.timeline import MeterChange, TempoChange, Timeline  # noqa: E402


def build(rows, *, meter=(4, 4), bpm=120.0, pickup=0.0, tracks=()) -> Score:
    """rows: (pitch, beat, beats[, kwargs]). Bars and seconds come from the timeline."""
    timeline = Timeline([TempoChange(0, bpm)], [MeterChange(1, *meter)], pickup_beats=pickup)
    notes = []
    for i, row in enumerate(rows):
        pitch, beat, beats = row[:3]
        kwargs = row[3] if len(row) > 3 else {}
        onset = timeline.seconds_at(beat)
        notes.append(Note(pitch=pitch, onset=onset, duration=timeline.seconds_at(beat + beats) - onset,
                          bar=timeline.bar_at(beat), id=f"s{i}", beat=beat, beats=beats, **kwargs))
    return Score(notes=notes, tempo_bpm=bpm, timeline=timeline, tracks=list(tracks), title="t")


def chord(root, beat, beats, quality=(0, 4, 7), **kwargs):
    return [(root + o, beat, beats, kwargs) for o in quality]


def left(score):
    return sorted((n for n in score.notes if n.staff == 2), key=lambda n: (n.onset, n.pitch))


def one_section(bars, pattern=LHPattern.BLOCK, **kwargs):
    return ArrangementPlan(sections=[Section(1, bars, pattern, **kwargs)])


# --- melody -----------------------------------------------------------------


def test_melody_rest_does_not_pull_in_the_accompaniment():
    # Tune in bar 1, silent in bar 2 while the left hand keeps arpeggiating.
    rows = [(76, 0, 1), (77, 1, 1), (79, 2, 2)]
    rows += [(48 + o, b, 0.5) for b in range(8) for o in (0,)] + [(55, b + 0.5, 0.5) for b in range(8)]
    rows += [(76, 8, 2)]
    melody = extract_melody(build(rows))
    assert [n.pitch for n in melody] == [76, 77, 79, 76]


def test_held_melody_note_is_one_note_not_one_per_inner_attack():
    rows = [(79, 0, 4)] + [(60 + (i % 3) * 4, i * 0.5, 0.5) for i in range(8)]
    melody = extract_melody(build(rows))
    assert [n.pitch for n in melody] == [79]
    # The skyline gets this right too, but only by merging afterwards; the
    # path never considers the inner attacks in the first place.
    assert [n.pitch for n in extract_melody_skyline(build(rows))] == [79]


def test_track_information_finds_a_tune_that_is_not_on_top():
    # A high sustained pad sits above a moving vocal line.
    tracks = [TrackInfo(0, "Strings pad"), TrackInfo(1, "Lead vocal")]
    rows = [(84, b, 4, {"track": 0}) for b in (0, 4)]
    tune = [67, 69, 71, 72, 74, 72, 71, 69]
    rows += [(p, i, 1, {"track": 1}) for i, p in enumerate(tune)]
    melody = extract_melody(build(rows, tracks=tracks))
    chosen = [n.pitch for n in melody if n.pitch < 80]
    assert chosen == tune, "the vocal line should be followed under the pad"


def test_user_marked_melody_overrules_every_heuristic():
    rows = [(84, 0, 4), (60, 0, 1, {"role": "melody"}), (62, 1, 1, {"role": "melody"}),
            (64, 2, 2, {"role": "melody"})]
    melody = extract_melody(build(rows))
    assert [n.pitch for n in melody] == [60, 62, 64]
    assert all(n.staff == 1 and n.role == "melody" for n in melody)


def test_melody_keeps_a_link_to_its_source_note():
    melody = extract_melody(build([(72, 0, 1), (74, 1, 1)]))
    assert [n.source_id for n in melody] == ["s0", "s1"]


# --- harmony ------------------------------------------------------------------


def test_chord_change_inside_a_bar_is_detected():
    rows = chord(48, 0, 2) + chord(55, 2, 2) + [(72, 0, 2), (74, 2, 2)]   # C then G in one bar
    segments = detect_chord_segments(build(rows), extract_melody(build(rows)))[1]
    assert [(s.root, s.quality) for s in segments] == [(0, "maj"), (7, "maj")]
    assert [(s.start_beat, s.end_beat) for s in segments] == [(0.0, 2.0), (2.0, 4.0)]


def test_a_passing_tone_is_not_a_chord_change():
    # C major all bar; the tune touches D and F on the way. Near miss for the rule above.
    rows = chord(48, 0, 4) + [(72, 0, 1), (74, 1, 1), (76, 2, 1), (77, 3, 1)]
    source = build(rows)
    segments = detect_chord_segments(source, extract_melody(source))[1]
    assert len(segments) == 1 and segments[0].root == 0


def test_bass_line_reports_what_is_really_at_the_bottom():
    rows = [(52, 0, 4), (60, 0, 4), (67, 0, 4), (72, 0, 4)]      # C major over E: first inversion
    source = build(rows)
    assert bass_line(source)[1] == 52
    segment = detect_chord_segments(source, extract_melody(source))[1][0]
    assert segment.root == 0 or segment.bass_pc == 4
    assert segment.bass_pc == 4


# --- rendering: meter -----------------------------------------------------------


@pytest.mark.parametrize(
    "meter, pattern, expected_attacks",
    [((4, 4), LHPattern.ALBERTI, 4), ((3, 4), LHPattern.ALBERTI, 3), ((6, 8), LHPattern.ARPEGGIO, 6),
     ((2, 4), LHPattern.BROKEN_OCTAVE, 4), ((5, 4), LHPattern.ARPEGGIO, 5)],
)
def test_left_hand_figures_follow_the_meter(meter, pattern, expected_attacks):
    bar_beats = meter[0] * 4 / meter[1]
    rows = chord(48, 0, bar_beats) + [(72, 0, bar_beats)]
    out = render(one_section(1, pattern), build(rows, meter=meter))
    attacks = sorted({round(n.beat, 6) for n in left(out)})
    assert len(attacks) == expected_attacks
    step = bar_beats / expected_attacks
    assert attacks == pytest.approx([i * step for i in range(expected_attacks)])


def test_waltz_stride_puts_the_bass_on_one_and_chords_after():
    rows = chord(48, 0, 3) + [(72, 0, 3)]
    out = render(one_section(1, LHPattern.STRIDE), build(rows, meter=(3, 4)))
    by_beat: dict[float, list[int]] = {}
    for n in left(out):
        by_beat.setdefault(n.beat, []).append(n.pitch)
    assert by_beat[0.0] == [48]
    assert by_beat[1.0] == by_beat[2.0] and len(by_beat[1.0]) == 2
    assert min(by_beat[1.0]) > 48


def test_broken_tenth_reaches_a_tenth_in_sequence_never_at_once():
    rows = chord(48, 0, 4) + [(84, 0, 4)]
    out = render(one_section(1, LHPattern.BROKEN_TENTH), build(rows))
    pitches = [n.pitch for n in left(out)]
    assert max(pitches) - min(pitches) == 16
    assert len({n.onset for n in left(out)}) == len(left(out)), "one note at a time"


def test_pickup_bar_gets_only_its_own_pulses():
    rows = [(67, 0, 1), (55, 0, 1)] + chord(48, 1, 3) + [(72, 1, 3)]
    out = render(one_section(2, LHPattern.ALBERTI), build(rows, meter=(3, 4), pickup=1.0))
    assert [n.beat for n in left(out) if n.bar == 1] == [0.0]
    assert [n.beat for n in left(out) if n.bar == 2] == [1.0, 2.0, 3.0]


def test_without_a_timeline_the_bar_is_still_cut_in_four():
    source = Score(notes=[Note(p, 0.0, 2.0, bar=1) for p in (48, 52, 55, 72)])
    out = render(one_section(1, LHPattern.ALBERTI), source)
    assert [round(n.onset, 3) for n in left(out)] == [0.0, 0.5, 1.0, 1.5]


# --- rendering: silence, bass, voicing ---------------------------------------------


def test_left_hand_is_silent_where_the_source_is_silent():
    rows = chord(48, 0, 4) + [(72, 0, 4)] + chord(48, 8, 4) + [(72, 8, 4)]   # bar 2 is empty
    out = render(one_section(3), build(rows))
    assert sorted({n.bar for n in left(out)}) == [1, 3]


def test_final_short_chord_is_not_padded_to_the_barline():
    rows = chord(48, 0, 1) + [(72, 0, 1)]
    out = render(one_section(1), build(rows))
    assert max(n.offset for n in left(out)) == pytest.approx(0.5)


def test_source_bass_keeps_a_first_inversion():
    rows = [(52, 0, 4), (60, 0, 4), (67, 0, 4), (76, 0, 4)]
    source = build(rows)
    root_bass = render(one_section(1, bass=BassMode.ROOT), source)
    real_bass = render(one_section(1, bass=BassMode.SOURCE), source)
    assert min(n.pitch for n in left(root_bass)) % 12 == 0
    assert min(n.pitch for n in left(real_bass)) % 12 == 4
    assert {n.pitch % 12 for n in left(real_bass)} <= {0, 4, 7}, "still the same chord"


def test_smooth_voicing_moves_the_hand_less_than_root_position():
    # I - IV - V - I, one chord per bar.
    roots = [48, 53, 55, 48]
    rows = []
    for i, r in enumerate(roots):
        rows += chord(r, i * 4, 4) + [(72 + i, i * 4, 4)]
    source = build(rows)

    def travel(score):
        by_bar: dict[int, list[int]] = {}
        for n in left(score):
            by_bar.setdefault(n.bar, []).append(n.pitch)
        centres = [sum(v) / len(v) for _, v in sorted(by_bar.items())]
        return sum(abs(b - a) for a, b in zip(centres, centres[1:], strict=False))

    plain = render(one_section(4, voicing=Voicing.ROOT), source)
    smooth = render(one_section(4, voicing=Voicing.SMOOTH), source)
    assert travel(smooth) < travel(plain) * 0.6
    for bar, root in enumerate(roots, start=1):
        pcs = {n.pitch % 12 for n in left(smooth) if n.bar == bar}
        assert pcs == {(root + o) % 12 for o in (0, 4, 7)}, "inversions keep every chord tone"
    assert min(n.pitch for n in left(smooth) if n.bar == 1) == 48, "first chord states the root"


def test_harmonic_rhythm_is_a_plan_decision():
    rows = chord(48, 0, 2) + chord(55, 2, 2) + [(72, 0, 2), (74, 2, 2)]
    source = build(rows)
    per_bar = render(one_section(1, harmonic_rhythm=HarmonicRhythm.BAR), source)
    detected = render(one_section(1, harmonic_rhythm=HarmonicRhythm.DETECTED), source)
    assert len({n.onset for n in left(per_bar)}) == 1
    assert sorted({n.beat for n in left(detected)}) == [0.0, 2.0]
    assert {n.pitch % 12 for n in left(detected) if n.beat == 2.0} == {7, 11, 2}


# --- rendering: what the output carries ------------------------------------------------


def test_rolled_chords_are_staggered_and_marked():
    rows = chord(48, 0, 4) + [(72, 0, 4)]
    out = render(one_section(1, roll_wide_chords=True), build(rows))
    notes = left(out)
    assert all(n.rolled for n in notes)
    assert [round(n.onset, 3) for n in notes] == [0.0, 0.03, 0.06]


def test_output_notes_have_ids_provenance_roles_and_dynamics():
    rows = chord(48, 0, 4) + [(72, 0, 2, {"velocity": 100}), (74, 2, 2, {"velocity": 100})]
    out = render(one_section(1), build(rows))
    ids = [n.id for n in out.notes]
    assert all(ids) and len(ids) == len(set(ids))
    right = [n for n in out.notes if n.staff == 1]
    assert [n.source_id for n in right] == ["s3", "s4"] and all(n.role == "melody" for n in right)
    assert {n.role for n in left(out)} == {"bass", "accompaniment"}
    assert all(n.velocity == 80 for n in left(out)), "left hand sits under the melody"
    assert all(n.beat is not None and n.beats for n in out.notes)


def test_pedal_bars_become_pedal_spans():
    rows = chord(48, 0, 4) + [(72, 0, 4)] + chord(48, 4, 4) + [(72, 4, 4)]
    plan = one_section(2)
    plan.pedal_bars = [2]
    out = render(plan, build(rows))
    assert [(p.start, p.end) for p in out.pedals] == [(2.0, 4.0)]


def test_octave_moves_keep_the_spelling_and_other_shifts_drop_it():
    rows = [(73, 0, 4, {"spelling": ("D", -1)}), (49, 0, 4)]
    source = build(rows)
    octave = render(ArrangementPlan(sections=[Section(1, 1, melody_shift=-12)]), source)
    third = render(ArrangementPlan(sections=[Section(1, 1, melody_shift=-3)]), source)
    assert next(n for n in octave.notes if n.staff == 1).spelling == ("D", -1)
    assert next(n for n in third.notes if n.staff == 1).spelling is None


# --- plan schema -------------------------------------------------------------------------


def test_skill_ceilings_match_the_left_hand_patterns_skill():
    assert set(patterns_for_skill(2)) == {LHPattern.BLOCK, LHPattern.PEDAL_TONE}
    assert LHPattern.ALBERTI in patterns_for_skill(4) and LHPattern.STRIDE not in patterns_for_skill(6)
    assert {LHPattern.STRIDE, LHPattern.BROKEN_TENTH} <= set(patterns_for_skill(7))


def test_plan_is_checked_against_the_piece_it_is_for():
    plan = ArrangementPlan(sections=[Section(1, 4), Section(9, 12, LHPattern.STRIDE)])
    problems = plan.validate_for_source(last_bar=12, skill_level=3)
    assert any("5-8" in p and "not covered" in p for p in problems)
    assert any("stride" in p and "skill_level" in p for p in problems)
    # Near miss: full coverage, and a pattern exactly one level up, is accepted.
    fine = ArrangementPlan(sections=[Section(1, 12, LHPattern.ARPEGGIO)])
    assert fine.validate_for_source(last_bar=12, skill_level=3) == []
    assert any("after the last bar" in p for p in
               ArrangementPlan(sections=[Section(1, 4), Section(40, 44)]).validate_for_source(4))


def test_hostile_plans_are_cheap_to_reject():
    import time

    started = time.perf_counter()
    problems = ArrangementPlan(sections=[Section(1, 10**12), Section(2, 10**12)]).validate()
    assert time.perf_counter() - started < 0.05
    assert any("beyond the limit" in p for p in problems)
    assert any("both cover bar 2" in p for p in problems)

    for bad in ({"sections": [{"start_bar": "1", "end_bar": 4}]},
                {"sections": [{"start_bar": True, "end_bar": 4}]},
                {"sections": [{"start_bar": 1.5, "end_bar": 4}]},
                {"sections": [{"end_bar": 4}]},
                {"sections": [{"start_bar": 1, "end_bar": 4, "label": "x" * 10_000}]},
                {"sections": [{"start_bar": 1, "end_bar": 2}] * 1000},
                {"sections": [{"start_bar": 1, "end_bar": 2}], "pedal_bars": [10**9]},
                {"sections": [{"start_bar": 1, "end_bar": 2}], "notes": "x" * 100_000},
                {"sections": {"start_bar": 1}}, "not a plan", None):
        with pytest.raises(ValueError):
            ArrangementPlan.from_dict(bad)


def test_left_hand_never_lands_on_or_above_the_sounding_melody():
    # A low tune over a high-voiced arpeggio: the figure would climb onto the melody's key.
    rows = chord(60, 0, 4) + [(64, 0, 4)]
    out = render(one_section(1, LHPattern.ARPEGGIO, lh_octave=4), build(rows))
    melody_pitch = next(n.pitch for n in out.notes if n.staff == 1)
    assert all(n.pitch < melody_pitch for n in left(out))
    assert {n.pitch % 12 for n in left(out)} == {0, 4, 7}, "moved by octaves, so the harmony is intact"
    # Near miss: with the tune well above, nothing is moved.
    high = render(one_section(1, LHPattern.ARPEGGIO), build(chord(48, 0, 4) + [(84, 0, 4)]))
    assert sorted({n.pitch for n in left(high)}) == [48, 52, 55, 60]
