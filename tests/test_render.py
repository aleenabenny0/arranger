"""Plan and renderer tests.

Runs under pytest, or standalone: `python tests/test_render.py`

The renderer's job is to be *predictable*, not clever. These tests pin the
behaviour that the agent will depend on: that a plan fully determines the
output, that invalid plans fail loudly, and that the melody is never lost.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.ir import Note, Score  # noqa: E402
from arranger.plan import (  # noqa: E402
    ArrangementPlan, LHPattern, Reduction, ReductionKind, Section, simple_plan,
)
from arranger.render import (  # noqa: E402
    RenderError, detect_chords, extract_melody, last_bar, render,
)

# A tiny C-major -> G-major source: melody on top, triads underneath.
SOURCE = Score(notes=[])
SOURCE.notes = [
    n for bar, root, mel in ((1, 60, 72), (2, 67, 74))
    for n in Score.from_tuples([
        (root, (bar - 1) * 2.0, 1.9),
        (root + 4, (bar - 1) * 2.0, 1.9),
        (root + 7, (bar - 1) * 2.0, 1.9),
        (mel, (bar - 1) * 2.0, 0.9),
        (mel + 2, (bar - 1) * 2.0 + 1.0, 0.9),
    ]).notes
]
for n in SOURCE.notes:
    object.__setattr__(n, "bar", 1 if n.onset < 2.0 else 2)
SOURCE = Score(notes=SOURCE.notes)


# --- plan validation -----------------------------------------------------

def test_empty_plan_is_invalid():
    assert ArrangementPlan().validate()


def test_backwards_section_is_invalid():
    p = ArrangementPlan(sections=[Section(start_bar=8, end_bar=2)])
    assert any("start_bar after end_bar" in x for x in p.validate())


def test_overlapping_sections_are_rejected():
    # Two sections covering the same bar means one silently wins. Refuse.
    p = ArrangementPlan(sections=[Section(1, 8), Section(6, 12)])
    assert any("both cover bar" in x for x in p.validate())


def test_adjacent_sections_are_fine():
    assert not ArrangementPlan(sections=[Section(1, 8), Section(9, 16)]).validate()


def test_absurd_transposition_is_rejected():
    p = ArrangementPlan(sections=[Section(1, 4, melody_shift=36)])
    assert any("melody_shift" in x for x in p.validate())


# --- the model-output boundary ------------------------------------------

def test_unknown_keys_are_an_error_not_a_default():
    # A model that writes lh_style instead of lh_pattern must be told, not
    # silently handed the default.
    try:
        ArrangementPlan.from_dict({"sections": [{"start_bar": 1, "end_bar": 4,
                                                 "lh_style": "block"}]})
    except ValueError as exc:
        assert "lh_style" in str(exc)
    else:
        raise AssertionError("unknown key was accepted")


def test_bad_pattern_names_list_the_valid_ones():
    # The error message is the repair agent's only guidance. It must enumerate.
    try:
        ArrangementPlan.from_dict({"sections": [{"start_bar": 1, "end_bar": 4,
                                                 "lh_pattern": "boogie"}]})
    except ValueError as exc:
        assert "alberti" in str(exc) and "boogie" in str(exc)
    else:
        raise AssertionError("invalid pattern was accepted")


def test_plans_survive_a_round_trip():
    original = ArrangementPlan(
        title="x", target_skill=6,
        sections=[Section(1, 8, LHPattern.ALBERTI, melody_shift=-12)],
        reductions=[Reduction(ReductionKind.INNER_VOICE, 1, 8, "too thick")],
    )
    import json
    copy = ArrangementPlan.from_dict(json.loads(original.to_json()))
    assert copy.sections[0].lh_pattern == LHPattern.ALBERTI
    assert copy.sections[0].melody_shift == -12
    assert copy.reductions[0].kind == ReductionKind.INNER_VOICE


# --- analysis ------------------------------------------------------------

def test_melody_is_the_top_line():
    melody = extract_melody(SOURCE)
    assert melody, "no melody found"
    assert max(n.pitch for n in SOURCE.notes) == max(n.pitch for n in melody)


def test_melody_does_not_dive_into_the_accompaniment():
    # The bug that made the first renderer worse than no renderer: when the
    # melody rests, "highest sounding note" is a bass note two octaves down.
    melody = extract_melody(SOURCE)
    span = max(n.pitch for n in melody) - min(n.pitch for n in melody)
    assert span <= 12, f"melody spans {span} semitones; it is grabbing bass notes"


def test_chords_are_detected_per_bar():
    chords = detect_chords(SOURCE, extract_melody(SOURCE))
    assert chords[1][0] == 0, f"bar 1 should be rooted on C, got {chords[1]}"
    assert chords[2][0] == 7, f"bar 2 should be rooted on G, got {chords[2]}"


# --- rendering -----------------------------------------------------------

def test_render_is_deterministic():
    plan = simple_plan(last_bar(SOURCE))
    a, b = render(plan, SOURCE), render(plan, SOURCE)
    assert [(n.pitch, n.onset) for n in a.notes] == [(n.pitch, n.onset) for n in b.notes]


def test_invalid_plans_raise_rather_than_render():
    try:
        render(ArrangementPlan(), SOURCE)
    except RenderError:
        pass
    else:
        raise AssertionError("empty plan rendered anyway")


def test_empty_source_raises():
    try:
        render(simple_plan(4), Score(notes=[]))
    except RenderError:
        pass
    else:
        raise AssertionError("empty source rendered anyway")


def test_melody_is_never_dropped():
    # CLAUDE.md: the melody is sacred. Every pattern must preserve it.
    melody = {n.pitch for n in extract_melody(SOURCE)}
    for pattern in LHPattern:
        out = render(
            ArrangementPlan(sections=[Section(1, 2, pattern)]), SOURCE
        )
        kept = {n.pitch for n in out.notes if n.staff == 1}
        assert melody <= kept, f"{pattern} lost melody notes"


def test_melody_shift_moves_only_the_melody():
    plan = ArrangementPlan(sections=[Section(1, 2, melody_shift=-12)])
    shifted = render(plan, SOURCE)
    plain = render(ArrangementPlan(sections=[Section(1, 2)]), SOURCE)
    assert {n.pitch for n in shifted.notes if n.staff == 2} == \
           {n.pitch for n in plain.notes if n.staff == 2}
    assert min(n.pitch for n in shifted.notes if n.staff == 1) == \
           min(n.pitch for n in plain.notes if n.staff == 1) - 12


def test_zero_voices_means_melody_only():
    out = render(ArrangementPlan(sections=[Section(1, 2, lh_voices=0)]), SOURCE)
    assert not [n for n in out.notes if n.staff == 2]


def test_patterns_produce_different_output():
    # If the pattern field did nothing, the agent's choices would be theatre.
    outputs = {
        pattern: tuple(
            sorted((n.pitch, round(n.onset, 3))
                   for n in render(ArrangementPlan(sections=[Section(1, 2, pattern)]),
                                   SOURCE).notes if n.staff == 2)
        )
        for pattern in LHPattern
    }
    assert len(set(outputs.values())) >= 4, "patterns are not distinguishable"


def test_melody_fold_narrows_the_range():
    # The lever that fixes right-hand violations. melody_shift moves a whole
    # section and cannot fix a leap inside it; folding can.
    wide = Score(notes=[
        Note(pitch=60 + 4 * i, onset=i * 0.4, duration=0.3, bar=1)
        for i in range(9)
    ], title="wide")
    plan = ArrangementPlan(sections=[Section(1, 1, lh_voices=0, melody_fold_window=12)])
    out = render(plan, wide)
    pitches = [n.pitch for n in out.notes if n.staff == 1]
    assert max(pitches) - min(pitches) <= 12


def test_melody_fold_moves_only_by_octaves():
    # Folding must preserve pitch class. Any other interval changes the tune.
    wide = Score(notes=[
        Note(pitch=60 + 4 * i, onset=i * 0.4, duration=0.3, bar=1)
        for i in range(9)
    ], title="wide")
    plan = ArrangementPlan(sections=[Section(1, 1, lh_voices=0, melody_fold_window=12)])
    folded = {n.pitch % 12 for n in render(plan, wide).notes if n.staff == 1}
    assert folded == {n.pitch % 12 for n in wide.notes}


def test_absurd_fold_window_is_rejected():
    assert ArrangementPlan(
        sections=[Section(1, 4, melody_fold_window=3)]
    ).validate()


def test_left_hand_stays_below_the_melody():
    out = render(simple_plan(last_bar(SOURCE)), SOURCE)
    lh = [n.pitch for n in out.notes if n.staff == 2]
    rh = [n.pitch for n in out.notes if n.staff == 1]
    assert max(lh) < max(rh), "left hand is above the melody"


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
