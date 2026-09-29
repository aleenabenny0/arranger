"""LilyPond source generation and the engraving subprocess.

Source generation is pure and always runs. Tests that launch LilyPond skip,
with a stated reason, when the program is not installed; they are the proof
that PDF download produces a real document rather than a placeholder.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.lilypond import (  # noqa: E402
    EngraverUnavailable,
    EngravingCancelled,
    EngravingTimeout,
    LilyPondEngraver,
    quote,
    to_lilypond,
)
from arranger.ir import Note, PedalSpan, Score  # noqa: E402
from arranger.limits import LimitExceeded  # noqa: E402
from arranger.notation import NMeasure, NotatedScore, notate  # noqa: E402
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402

ENGRAVER = LilyPondEngraver()
needs_lilypond = pytest.mark.skipif(
    not ENGRAVER.status().available,
    reason=f"LilyPond not installed: {ENGRAVER.status().detail}",
)


def score_of(rows, *, meter=(4, 4), fifths=0, mode="major", pickup=0.0, title="Test Piece", bpm=100.0):
    timeline = Timeline([TempoChange(0, bpm)], [MeterChange(1, *meter)], [KeyChange(0, fifths, mode)],
                        pickup_beats=pickup)
    notes = []
    for i, row in enumerate(rows):
        pitch, beat, beats, staff = row[:4]
        kwargs = row[4] if len(row) > 4 else {}
        onset = timeline.seconds_at(beat)
        notes.append(Note(pitch=pitch, onset=onset, duration=timeline.seconds_at(beat + beats) - onset,
                          staff=staff, id=f"n{i}", beat=beat, beats=beats, **kwargs))
    return Score(notes=notes, tempo_bpm=bpm, title=title, timeline=timeline, composer="Anon.")


RICH = [
    (72, 0, 1 / 3, 1), (74, 1 / 3, 1 / 3, 1), (76, 2 / 3, 1 / 3, 1),          # triplet
    (77, 1, 1.5, 1, {"articulations": ("accent",), "dynamic": "mf"}),          # dotted, marked
    (79, 2.5, 3.5, 1),                                                          # tied over the bar
    (60, 0, 4, 1), (64, 0, 4, 1),                                               # second voice chord
    (36, 0, 2, 2, {"rolled": True}), (43, 0, 2, 2, {"rolled": True}), (52, 0, 2, 2, {"rolled": True}),
    (41, 2, 0.5, 2, {"articulations": ("staccato",)}), (48, 2.5, 0.5, 2), (53, 3, 1, 2),
    (36, 4, 4, 2),
]


# --- source ----------------------------------------------------------------


def test_source_has_the_expected_musical_content():
    source = to_lilypond(notate(score_of(RICH, fifths=-1)))
    assert '\\version "2.24.0"' in source
    assert "\\key f \\major" in source and "\\time 4/4" in source and "\\tempo 4 = 100" in source
    assert "\\tuplet 3/2 { c''8 d''8 e''8 }" in source
    assert "f''4.->\\mf" in source
    assert "~" in source, "the note held over the barline must be tied"
    assert "<< {" in source and "\\\\" in source, "two voices on the upper staff"
    assert "<c, g, e>2\\arpeggio" in source
    assert "f,8-." in source
    assert source.count("| %") == 4, "two bars on each of two staves, each with a bar check"


def test_pitch_names_cover_accidentals_and_octaves():
    source = to_lilypond(notate(score_of([(61, 0, 1, 1), (58, 1, 1, 1), (24, 0, 1, 2), (96, 2, 1, 1)], fifths=2)))
    # D major spells pitch class 10 as its flat sixth, B flat, not A sharp.
    assert "cis'4" in source and "bes4" in source and "c,,4" in source and "c''''4" in source


def test_minor_key_and_compound_meter_and_pickup():
    source = to_lilypond(notate(score_of([(69, 0, 0.5, 1), (72, 0.5, 3, 1)], meter=(6, 8), mode="minor", pickup=0.5)))
    assert "\\key a \\minor" in source and "\\time 6/8" in source
    assert "\\partial 64*8" in source, "an eighth-note pickup"
    assert "c''2." in source


def test_pedal_marks_are_balanced():
    score = score_of([(48, 0, 1, 2), (50, 1, 1, 2), (52, 2, 1, 2), (53, 3, 1, 2)])
    score.pedals = [PedalSpan(0.0, 1.2), PedalSpan(1.2, 2.4)]
    source = to_lilypond(notate(score))
    assert source.count("\\sustainOn") == 2 and source.count("\\sustainOff") == 2
    assert source.index("\\sustainOn") < source.index("\\sustainOff")


def test_empty_staff_is_full_bar_rests_for_any_meter():
    source = to_lilypond(notate(score_of([(72, 0, 5, 1)], meter=(5, 4))))
    assert "R1*5/4" in source


@pytest.mark.parametrize(
    "title",
    ['"', "\\", '" } #(system "calc.exe") \\header { title = "', "a\nb\x00c", "x" * 5000],
    ids=["quote", "backslash", "scheme-injection", "control-chars", "very-long"],
)
def test_titles_cannot_escape_their_string_literal(title):
    literal = quote(title)
    assert literal.startswith('"') and literal.endswith('"')
    inner = literal[1:-1]
    # Every quote inside is escaped, and no escape is left dangling at the end.
    i = 0
    while i < len(inner):
        if inner[i] == "\\":
            assert i + 1 < len(inner), "dangling backslash would escape the closing quote"
            i += 2
            continue
        assert inner[i] != '"', "unescaped quote ends the string early"
        i += 1
    assert "\n" not in literal and "\x00" not in literal
    assert len(literal) <= 2 * 200 + 2

    source = to_lilypond(notate(score_of([(60, 0, 1, 1)], title=title)))
    header = source[source.index("\\header"):source.index("upper =")]
    assert header.count("#(") == (1 if "#(system" in title else 0), "only ever as inert text"


def test_too_long_to_engrave_is_refused_before_launching_anything():
    measures = [NMeasure(i, 4, 4, 4, 0, "major") for i in range(1, 1600)]
    with pytest.raises(LimitExceeded):
        to_lilypond(NotatedScore("long", "", measures))


def test_missing_binary_is_reported_not_crashed():
    engraver = LilyPondEngraver(executable=str(Path(__file__).with_name("no-such-lilypond")))
    # An explicit path that does not exist falls back to discovery; force "none found".
    engraver._status = None
    import arranger.adapters.lilypond as module

    original = module.find_lilypond
    module.find_lilypond = lambda explicit=None: None
    try:
        assert not engraver.status().available
        with pytest.raises(EngraverUnavailable) as info:
            engraver.engrave(notate(score_of([(60, 0, 1, 1)])))
        assert "LILYPOND_PATH" in info.value.detail
        assert "LILYPOND_PATH" not in info.value.public
    finally:
        module.find_lilypond = original


# --- the real program -------------------------------------------------------


@needs_lilypond
def test_engraves_a_real_pdf_with_every_notation_feature():
    score = score_of(RICH, fifths=-1, title='Tricky "Title" \\ #(display 1)')
    score.pedals = [PedalSpan(0.0, 1.0)]
    pdf, warnings = ENGRAVER.engrave_score(score)
    assert pdf.startswith(b"%PDF") and b"%%EOF" in pdf[-1024:]
    assert len(pdf) > 5_000, "a real page of music, not an empty document"
    assert isinstance(warnings, list)


@needs_lilypond
def test_cancellation_stops_the_subprocess_promptly():
    rows = [(60 + (i % 24), i * 0.25, 0.25, 1) for i in range(1600)]
    started = time.monotonic()
    with pytest.raises(EngravingCancelled):
        ENGRAVER.engrave(notate(score_of(rows)), should_cancel=lambda: time.monotonic() - started > 0.3)
    assert time.monotonic() - started < 10


@needs_lilypond
def test_timeout_is_enforced():
    rows = [(60 + (i % 24), i * 0.25, 0.25, 1) for i in range(1600)]
    quick = LilyPondEngraver(timeout=0.2)
    with pytest.raises(EngravingTimeout):
        quick.engrave(notate(score_of(rows)))
