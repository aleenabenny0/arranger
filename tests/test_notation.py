"""Notation model and MusicXML writer.

The invariant every test leans on: in every bar, every voice is a gapless run
of events that sums exactly to the bar length. A writer that breaks that
produces files that open misaligned, or not at all.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction as F
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.musicxml_writer import DIVISIONS, write_musicxml, write_mxl  # noqa: E402
from arranger.ir import Note, PedalSpan, Score  # noqa: E402
from arranger.notation import estimate_key, notate, spell, spell_pitch_class  # noqa: E402
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402


def score_of(rows, *, meter=(4, 4), bpm=120.0, fifths=0, mode="major", pickup=0.0, title="t",
             keys=True, **extra) -> Score:
    """rows: (pitch, beat, beats, staff[, kwargs])"""
    timeline = Timeline(
        [TempoChange(0, bpm)], [MeterChange(1, *meter)],
        [KeyChange(0, fifths, mode)] if keys else [], pickup_beats=pickup,
    )
    notes = []
    for i, row in enumerate(rows):
        pitch, beat, beats, staff = row[:4]
        kwargs = row[4] if len(row) > 4 else {}
        onset = timeline.seconds_at(beat)
        notes.append(
            Note(pitch=pitch, onset=onset, duration=timeline.seconds_at(beat + beats) - onset,
                 staff=staff, bar=timeline.bar_at(beat), id=f"n{i}", beat=beat, beats=beats, **kwargs)
        )
    return Score(notes=notes, tempo_bpm=bpm, title=title, timeline=timeline, **extra)


def assert_well_formed(notated) -> None:
    for m in notated.measures:
        assert set(m.staves) == {1, 2}, f"bar {m.number} is missing a staff"
        for staff, voices in m.staves.items():
            for voice in voices:
                cursor = F(0)
                for ev in voice.events:
                    assert ev.start == cursor, f"gap or overlap in bar {m.number} staff {staff}"
                    assert ev.duration > 0
                    cursor += ev.duration
                assert cursor == m.length, f"bar {m.number} staff {staff} sums to {cursor}, not {m.length}"


def events(notated, bar, staff=1, voice=0):
    return notated.measures[bar - 1].staves[staff][voice].events


def shape(evs):
    return [(str(e.start), e.note_type, e.dots, tuple(p.midi for p in e.pitches)) for e in evs]


# --- spelling --------------------------------------------------------------


def test_spelling_follows_the_key():
    assert spell_pitch_class(6, 0) == ("F", 1)      # F# in C major
    assert spell_pitch_class(10, 0) == ("B", -1)    # Bb in C major
    assert spell_pitch_class(8, 0) == ("A", -1)     # Ab, not G#, in C major
    assert spell_pitch_class(8, 3) == ("G", 1)      # G# in A major
    assert spell_pitch_class(3, -3) == ("E", -1)    # Eb in Eb major
    assert spell_pitch_class(1, -5) == ("D", -1)    # Db in Db major
    assert spell_pitch_class(1, 4) == ("C", 1)      # C# in E major
    assert spell_pitch_class(5, 6) == ("E", 1)      # E# only where it belongs: F# major


def test_minor_keys_spell_their_leading_tone_sharp():
    # Violating case for the old behaviour: G# in A minor came out as Ab.
    assert spell_pitch_class(8, 0, "minor") == ("G", 1)
    assert spell_pitch_class(11, -3, "minor") == ("B", 0)    # B natural in C minor
    assert spell_pitch_class(6, -3, "minor") == ("F", 1)     # F# in C minor
    assert spell_pitch_class(1, -1, "minor") == ("C", 1)     # C# in D minor
    # Near miss: the flat second degree is still a flat in minor.
    assert spell_pitch_class(10, 0, "minor") == ("B", -1)    # Bb in A minor
    assert spell_pitch_class(1, -3, "minor") == ("D", -1)    # Db in C minor
    # And the same pitch class in the relative major keeps its flat spelling.
    assert spell_pitch_class(8, 0, "major") == ("A", -1)


def test_notated_minor_piece_uses_the_minor_window():
    notated = notate(score_of([(68, 0, 1, 1), (69, 1, 3, 1)], mode="minor"))
    first = events(notated, 1)[0].pitches[0]
    assert (first.step, first.alter, first.octave) == ("G", 1, 4)


def test_every_key_spells_its_own_scale_without_accidental_clashes():
    major = [0, 2, 4, 5, 7, 9, 11]
    tonic_pc = {k: (k * 7) % 12 for k in range(-7, 8)}
    for fifths, tonic in tonic_pc.items():
        letters = [spell_pitch_class((tonic + d) % 12, fifths)[0] for d in major]
        assert len(set(letters)) == 7, f"key {fifths} repeats a letter: {letters}"


def test_octave_accounts_for_the_accidental():
    assert spell(59, -7, ("C", -1)) == ("C", -1, 4)   # Cb4 sounds as B3
    assert spell(60, 7, ("B", 1)) == ("B", 1, 3)      # B#3 sounds as C4
    assert spell(60, 0) == ("C", 0, 4)


def test_source_spelling_is_kept_only_when_it_matches_the_pitch():
    assert spell(61, 0, ("D", -1))[:2] == ("D", -1)
    assert spell(61, 0, ("E", 0))[:2] == ("C", 1)     # wrong hint is ignored


def test_key_estimation_on_unambiguous_material():
    c_major = [Note(p, i * 0.5, 0.5) for i, p in enumerate([60, 62, 64, 65, 67, 69, 71, 72, 67, 64, 60])]
    assert estimate_key(c_major) == (0, "major")
    a_minor = [Note(p, i * 0.5, 0.5) for i, p in enumerate([57, 59, 60, 62, 64, 65, 68, 69, 64, 60, 57, 57])]
    assert estimate_key(a_minor) == (0, "minor")


def test_missing_key_is_estimated_and_said_so():
    notated = notate(score_of([(62, 0, 1, 1), (66, 1, 1, 1), (69, 2, 1, 1), (74, 3, 1, 1)], keys=False))
    assert notated.key_was_estimated
    assert notated.measures[0].fifths == 2
    assert any("estimated" in w for w in notated.warnings)


# --- rhythm ----------------------------------------------------------------


def test_simple_bar_and_whole_bar_rest_in_the_empty_staff():
    notated = notate(score_of([(60, 0, 1, 1), (62, 1, 1, 1), (64, 2, 2, 1)]))
    assert_well_formed(notated)
    assert shape(events(notated, 1)) == [("0", "quarter", 0, (60,)), ("1", "quarter", 0, (62,)),
                                          ("2", "half", 0, (64,))]
    left = events(notated, 1, staff=2)
    assert len(left) == 1 and left[0].whole_bar_rest and left[0].duration == 4


def test_note_across_a_barline_is_tied():
    notated = notate(score_of([(60, 3, 2, 1)]))
    assert_well_formed(notated)
    first, second = events(notated, 1)[-1], events(notated, 2)[0]
    assert first.pitches[0].tie_start and not first.pitches[0].tie_stop
    assert second.pitches[0].tie_stop and not second.pitches[0].tie_start
    assert first.pitches[0].note_id == "n0" and second.pitches[0].note_id == "n0-t1"


def test_the_middle_of_a_four_four_bar_stays_visible():
    # A half note starting on beat 2 is written as two tied quarters.
    notated = notate(score_of([(60, 1, 2, 1)]))
    assert shape(events(notated, 1))[1:3] == [("1", "quarter", 0, (60,)), ("2", "quarter", 0, (60,))]
    # Near miss: a dotted half from the downbeat may cross it.
    assert shape(events(notate(score_of([(60, 0, 3, 1)])), 1))[0] == ("0", "half", 1, (60,))


def test_syncopated_quarter_is_not_split():
    notated = notate(score_of([(60, 0, 0.5, 1), (62, 0.5, 1, 1), (64, 1.5, 0.5, 1)]))
    assert shape(events(notated, 1))[:3] == [("0", "eighth", 0, (60,)), ("1/2", "quarter", 0, (62,)),
                                              ("3/2", "eighth", 0, (64,))]


def test_compound_meter_groups_in_dotted_quarters():
    notated = notate(score_of([(60, 1, 1, 1)], meter=(6, 8)))
    assert_well_formed(notated)
    assert notated.measures[0].length == 3
    # A quarter across the middle of 6/8 becomes two tied eighths.
    assert shape(events(notated, 1))[1:3] == [("1", "eighth", 0, (60,)), ("3/2", "eighth", 0, (60,))]
    full = notate(score_of([(60, 0, 3, 1)], meter=(6, 8)))
    assert shape(events(full, 1)) == [("0", "half", 1, (60,))]


def test_triplets_are_written_as_tuplets_and_sum_exactly():
    third = 1 / 3
    notated = notate(score_of([(60, 0, third, 1), (62, third, third, 1), (64, 2 * third, third, 1),
                               (65, 1, 1, 1)]))
    assert_well_formed(notated)
    trip = events(notated, 1)[:3]
    assert [e.duration for e in trip] == [F(1, 3)] * 3
    assert all(e.tuplet == (3, 2) and e.note_type == "eighth" for e in trip)
    assert trip[0].tuplet_start and trip[2].tuplet_stop and not trip[1].tuplet_start
    assert events(notated, 1)[3].tuplet is None


def test_a_rest_inside_a_triplet_beat_belongs_to_the_tuplet():
    third = 1 / 3
    notated = notate(score_of([(62, third, third, 1), (64, 2 * third, third, 1), (65, 1, 3, 1)]))
    assert_well_formed(notated)
    trip = events(notated, 1)[:3]
    assert [e.is_rest for e in trip] == [True, False, False]
    assert all(e.tuplet == (3, 2) for e in trip), "the rest must sit inside the bracket"
    assert trip[0].tuplet_start and trip[2].tuplet_stop
    assert sum((e.duration for e in trip), F(0)) == 1


def test_two_consecutive_triplet_beats_are_two_groups():
    third = 1 / 3
    rows = [(60 + i, i * third, third, 1) for i in range(6)]
    notated = notate(score_of(rows))
    evs = events(notated, 1)[:6]
    assert [e.tuplet_start for e in evs] == [True, False, False, True, False, False]
    assert [e.tuplet_stop for e in evs] == [False, False, True, False, False, True]


def test_pickup_bar_is_short_and_flagged():
    notated = notate(score_of([(67, 0, 1, 1), (72, 1, 3, 1)], meter=(3, 4), pickup=1.0))
    assert_well_formed(notated)
    assert notated.measures[0].is_pickup and notated.measures[0].length == 1
    assert notated.measures[1].length == 3
    assert shape(events(notated, 2)) == [("0", "half", 1, (72,))]


def test_performed_timing_is_snapped_and_reported():
    # A player's sixteenths, each a few milliseconds off the grid.
    rows = [(60, 0.01, 0.22, 1), (62, 0.26, 0.2, 1), (64, 0.49, 0.24, 1), (65, 0.76, 0.2, 1)]
    notated = notate(score_of(rows))
    assert_well_formed(notated)
    assert shape(events(notated, 1))[:4] == [("0", "16th", 0, (60,)), ("1/4", "16th", 0, (62,)),
                                              ("1/2", "16th", 0, (64,)), ("3/4", "16th", 0, (65,))]


def test_detached_playing_becomes_staccato_not_a_page_of_rests():
    rows = [(60, 0, 0.2, 1), (62, 1, 0.2, 1), (64, 2, 0.2, 1), (65, 3, 1, 1)]
    notated = notate(score_of(rows))
    evs = events(notated, 1)
    assert [e.note_type for e in evs] == ["quarter"] * 4
    assert "staccato" in evs[0].articulations and "staccato" not in evs[3].articulations


def test_a_long_silence_stays_a_rest():
    notated = notate(score_of([(60, 0, 1, 1), (62, 3, 1, 1)]))
    evs = events(notated, 1)
    rests = [e for e in evs if e.is_rest]
    assert not evs[0].is_rest and not evs[-1].is_rest
    assert sum((e.duration for e in rests), F(0)) == 2
    assert evs[0].duration == 1 and "staccato" not in evs[0].articulations


def test_chords_and_a_second_voice():
    rows = [(60, 0, 4, 1), (64, 0, 4, 1),          # held chord
            (72, 0, 1, 1), (74, 1, 1, 1)]          # moving line above it
    notated = notate(score_of(rows))
    assert_well_formed(notated)
    voices = notated.measures[0].staves[1]
    assert len(voices) == 2
    assert [p.midi for p in voices[0].events[0].pitches] == [72]
    assert [p.midi for p in voices[1].events[0].pitches] == [60, 64]
    assert any(e.hidden for e in voices[0].events) is False
    # The second voice pads with hidden rests, never visible ones.
    assert all(e.hidden or not e.is_rest for e in voices[1].events)


def test_three_way_overlap_is_simplified_and_reported():
    rows = [(60, 0, 4, 1), (64, 0.5, 3, 1), (67, 1, 2, 1)]
    notated = notate(score_of(rows))
    assert_well_formed(notated)
    assert any("simplified" in w for w in notated.warnings)
    sounding = {p.midi for m in notated.measures for v in m.staves[1] for e in v.events for p in e.pitches}
    assert sounding == {60, 64, 67}, "simplifying overlaps must never drop a pitch"


def test_notes_without_a_staff_split_at_middle_c():
    notated = notate(score_of([(72, 0, 1, None), (48, 0, 1, None)]))
    assert events(notated, 1, 1)[0].pitches[0].midi == 72
    assert events(notated, 1, 2)[0].pitches[0].midi == 48


def test_score_without_a_timeline_gets_four_four_and_a_warning():
    score = Score.from_tuples([(60, 0.0, 0.5, 1), (62, 0.5, 0.5, 1)], tempo_bpm=120)
    notated = notate(score)
    assert_well_formed(notated)
    assert any("4/4 was assumed" in w for w in notated.warnings)


def test_tempo_marks_only_where_the_tempo_really_changes():
    timeline = Timeline([TempoChange(0, 100), TempoChange(4, 101), TempoChange(8, 60)], [MeterChange(1, 4, 4)])
    notes = [Note(60, timeline.seconds_at(b), 0.5, staff=1, beat=float(b), beats=1.0) for b in range(12)]
    notated = notate(Score(notes=notes, timeline=timeline))
    assert [m.tempo_bpm for m in notated.measures] == [100, None, 60]


# --- MusicXML --------------------------------------------------------------


def parse(xml: bytes) -> ET.Element:
    return ET.fromstring(xml[xml.index(b"<score-partwise"):])


def test_musicxml_measures_add_up_in_divisions():
    third = 1 / 3
    rows = [(60, 0, third, 1), (62, third, third, 1), (64, 2 * third, third, 1), (65, 1, 4.5, 1),
            (48, 0, 4, 2), (55, 0, 2, 2), (43, 4, 4, 2)]
    xml, _ = write_musicxml(score_of(rows, fifths=-1))
    root = parse(xml)
    assert root.tag == "score-partwise" and root.get("version") == "4.0"
    for measure in root.iter("measure"):
        position, furthest = 0, 0
        for el in measure:
            if el.tag == "note" and el.find("chord") is None:
                position += int(el.findtext("duration"))
            elif el.tag == "forward":
                position += int(el.findtext("duration"))
            elif el.tag == "backup":
                furthest = max(furthest, position)
                position -= int(el.findtext("duration"))
                assert position == 0, "every voice must start at the barline"
            furthest = max(furthest, position)
        assert furthest == 4 * DIVISIONS


def test_musicxml_child_order_matches_the_schema():
    xml, _ = write_musicxml(score_of([(61, 0, 1 / 3, 1, {"articulations": ("accent",)}), (63, 1 / 3, 2 / 3, 1)]))
    order = ["chord", "pitch", "rest", "duration", "tie", "voice", "type", "dot",
             "time-modification", "staff", "notations"]
    for note in parse(xml).iter("note"):
        tags = [child.tag for child in note]
        ranks = [order.index(t) for t in tags]
        assert ranks == sorted(ranks), tags


def test_musicxml_carries_ids_ties_key_time_tempo_pedal_and_articulations():
    score = score_of(
        [(61, 3, 2, 1, {"articulations": ("staccato",), "dynamic": "mf"}), (49, 0, 4, 2, {"rolled": True})],
        fifths=2, bpm=96,
    )
    score.pedals.append(PedalSpan(0.0, 2.5))
    root = parse(write_musicxml(score)[0])

    ids = [n.get("id") for n in root.iter("note") if n.get("id")]
    assert "n0" in ids and "n0-t1" in ids and len(ids) == len(set(ids))
    assert root.find(".//key/fifths").text == "2"
    assert (root.find(".//time/beats").text, root.find(".//time/beat-type").text) == ("4", "4")
    assert root.find(".//metronome/per-minute").text == "96"
    assert {p.get("type") for p in root.iter("pedal")} == {"start", "stop"}
    assert root.find(".//articulations/staccato") is not None
    assert root.find(".//dynamics/mf") is not None
    assert root.find(".//arpeggiate") is not None
    tied = [t.get("type") for t in root.iter("tied")]
    assert tied == ["start", "stop"]
    step = root.find(".//note[@id='n0']/pitch")
    assert (step.findtext("step"), step.findtext("alter")) == ("C", "1")


def test_hostile_title_is_inert_text():
    score = score_of([(60, 0, 1, 1)], title='</work-title><script>alert(1)</script>&"')
    xml, _ = write_musicxml(score)
    root = parse(xml)
    assert root.findtext(".//work-title") == '</work-title><script>alert(1)</script>&"'
    assert root.find(".//script") is None


def test_mxl_is_a_zip_with_a_container_pointing_at_the_score():
    data, _ = write_mxl(score_of([(60, 0, 1, 1)]))
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        assert names[0] == "META-INF/container.xml"
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//rootfile").get("full-path")
        assert parse(archive.read(rootfile)).tag == "score-partwise"


@pytest.mark.parametrize("meter", [(2, 4), (3, 4), (4, 4), (5, 4), (6, 8), (9, 8), (12, 8), (2, 2), (3, 8), (7, 8)])
def test_every_meter_fills_its_bars(meter):
    unit = 4 / meter[1]
    rows = []
    beat = 0.0
    lengths = [unit, unit / 2, unit * 1.5, unit * 2.5, unit / 4, unit * 3.25, unit]
    for i in range(40):
        length = lengths[i % len(lengths)]
        rows.append((60 + i % 12, beat, length, 1 if i % 3 else 2))
        beat += length * (1.5 if i % 5 == 0 else 1)
    notated = notate(score_of(rows, meter=meter))
    assert_well_formed(notated)
    assert all(m.length == F(meter[0] * 4, meter[1]) for m in notated.measures)


def test_an_estimated_key_is_named_by_its_own_tonic():
    from arranger.notation import key_name

    assert key_name(0, "major") == "C major"
    assert key_name(0, "minor") == "A minor"
    assert key_name(3, "minor") == "F-sharp minor"
    assert key_name(-3, "major") == "E-flat major"
    # A harmonic-minor scale on A, with no key signature stated anywhere.
    pitches = [57, 59, 60, 62, 64, 65, 68, 69, 68, 65, 64, 62, 60, 59, 57, 57]
    notes = [Note(pitch=p, onset=i * 0.5, duration=0.45, bar=i // 4 + 1) for i, p in enumerate(pitches)]
    notated = notate(Score(notes=notes, tempo_bpm=120, timeline=Timeline.constant(120, 4, 4)))
    warning = next(w for w in notated.warnings if "estimated" in w)
    assert "A minor was estimated" in warning, warning
    assert "relative" not in warning

