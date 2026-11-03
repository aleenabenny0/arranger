"""MusicXML importer tests.

Two halves, like the importer: what the music means, and what a hostile file
cannot do. Fixtures are small hand-written fragments of public-domain tunes,
built from the helpers below so each test shows exactly the XML it is about.

Every score loaded through `load()` is also checked against the invariants
the rest of the system relies on (see `check`), so each musical test doubles
as a consistency test.

No test touches the network: `no_network` makes any attempt fail loudly.
"""

import hashlib
import io
import random
import socket
import struct
import sys
import time
import urllib.request
import zipfile
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters import musicxml_reader as mx  # noqa: E402
from arranger.adapters.musicxml_reader import (  # noqa: E402
    read_musicxml,
    read_musicxml_bytes,
    sniff_musicxml,
)
from arranger.ir import Note, PedalSpan, Score  # noqa: E402
from arranger.limits import (  # noqa: E402
    DEFAULT_LIMITS,
    EmptyScore,
    ImportLimits,
    LimitExceeded,
    MalformedFile,
    ScoreImportError,
    UnsafeContent,
    UnsupportedFormat,
)
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "musicxml"
PUBLIC_DOCTYPE = (
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
    '"http://www.musicxml.org/dtds/partwise.dtd">'
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any socket or URL fetch is a test failure, not a slow test."""

    def blocked(*args, **kwargs):
        raise AssertionError("the importer tried to touch the network")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


# --- building MusicXML ---------------------------------------------------


def note(step, octave, dur, *, alter=None, chord=False, voice=None, staff=None,
         tie=(), tied=(), notations="", attrs="", extra="", time_mod=""):
    parts = [f"<note{(' ' + attrs) if attrs else ''}>"]
    if chord:
        parts.append("<chord/>")
    parts.append(f"<pitch><step>{step}</step>")
    if alter is not None:
        parts.append(f"<alter>{alter}</alter>")
    parts.append(f"<octave>{octave}</octave></pitch><duration>{dur}</duration>")
    parts += [f'<tie type="{t}"/>' for t in tie]
    if voice is not None:
        parts.append(f"<voice>{voice}</voice>")
    parts.append(time_mod)
    if staff is not None:
        parts.append(f"<staff>{staff}</staff>")
    inner = notations + "".join(f'<tied type="{t}"/>' for t in (*tie, *tied))
    if inner:
        parts.append(f"<notations>{inner}</notations>")
    parts.append(extra)
    parts.append("</note>")
    return "".join(parts)


def rest(dur, voice=None, staff=None):
    v = f"<voice>{voice}</voice>" if voice is not None else ""
    s = f"<staff>{staff}</staff>" if staff is not None else ""
    return f"<note><rest/><duration>{dur}</duration>{v}{s}</note>"


def backup(dur):
    return f"<backup><duration>{dur}</duration></backup>"


def forward(dur):
    return f"<forward><duration>{dur}</duration></forward>"


def attributes(divisions=None, fifths=None, mode=None, time=None, staves=None, extra=""):
    out = ["<attributes>"]
    if divisions is not None:
        out.append(f"<divisions>{divisions}</divisions>")
    if fifths is not None:
        m = f"<mode>{mode}</mode>" if mode else ""
        out.append(f"<key><fifths>{fifths}</fifths>{m}</key>")
    if time is not None:
        out.append(f"<time><beats>{time[0]}</beats><beat-type>{time[1]}</beat-type></time>")
    if staves is not None:
        out.append(f"<staves>{staves}</staves>")
    out.append(extra)
    out.append("</attributes>")
    return "".join(out)


def direction(kind, staff=None, sound="", offset=""):
    s = f"<staff>{staff}</staff>" if staff is not None else ""
    return f"<direction><direction-type>{kind}</direction-type>{offset}{s}{sound}</direction>"


def dynamics(mark, staff=None):
    return direction(f"<dynamics><{mark}/></dynamics>", staff)


def pedal(kind):
    return direction(f'<pedal type="{kind}" line="yes"/>')


def tempo(bpm):
    return f'<direction><direction-type><words>tempo</words></direction-type><sound tempo="{bpm}"/></direction>'


def measure(number, *children, implicit=False):
    imp = ' implicit="yes"' if implicit else ""
    return f'<measure number="{number}"{imp}>{"".join(children)}</measure>'


def part(pid, *measures):
    return f'<part id="{pid}">{"".join(measures)}</part>'


def score_part(pid, name, midi=""):
    return f'<score-part id="{pid}"><part-name>{name}</part-name>{midi}</score-part>'


def score(*parts, part_list=None, header="", prolog=""):
    if part_list is None:
        part_list = score_part("P1", "Music")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>{prolog}<score-partwise version="4.0">'
        f"{header}<part-list>{part_list}</part-list>{''.join(parts)}</score-partwise>"
    ).encode("utf-8")


C44 = attributes(divisions=1, fifths=0, time=(4, 4))
Q = [note("C", 4, 1), note("D", 4, 1), note("E", 4, 1), note("F", 4, 1)]


def check(result: Score) -> Score:
    """Invariants every imported score must satisfy."""
    assert result.source_format == "musicxml"
    assert result.timeline is not None
    tl = result.timeline
    ids = [n.id for n in result.notes]
    assert len(ids) == len(set(ids)), "note ids must be unique"
    assert result.notes == sorted(result.notes, key=lambda n: (n.onset, n.pitch))
    for n in result.notes:
        assert n.bar == tl.bar_at(n.beat), f"{n.id}: bar {n.bar} != bar_at({n.beat})"
        assert n.onset == pytest.approx(tl.seconds_at(n.beat), abs=1e-9)
        assert n.duration == pytest.approx(
            tl.seconds_at(n.beat + n.beats) - tl.seconds_at(n.beat), abs=1e-9
        )
        assert n.duration > 0 and n.beats > 0
        assert 0 <= n.pitch <= 127 and 1 <= n.velocity <= 127
        assert 0 <= n.track < len(result.tracks)
        assert n.id
    for index, track in enumerate(result.tracks):
        assert track.index == index
        assert track.note_count == sum(1 for n in result.notes if n.track == index)
    assert result.tempo_bpm == tl.initial_bpm
    return result


def load(data: bytes, **kwargs) -> Score:
    return check(read_musicxml_bytes(data, **kwargs))


def by_id(result: Score) -> dict:
    return {n.id: n for n in result.notes}


def warned(result: Score, fragment: str) -> bool:
    return any(fragment in w for w in result.warnings)


def make_mxl(files: dict, rootfile: str | None = "score.musicxml", compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.recordare.musicxml")
        if rootfile is not None:
            zf.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?><container><rootfiles>'
                f'<rootfile full-path="{rootfile}" '
                'media-type="application/vnd.recordare.musicxml+xml"/>'
                "</rootfiles></container>",
            )
        for name, content in files.items():
            zf.writestr(zipfile.ZipInfo(name), content, compress_type=compression)
    return buf.getvalue()


SIMPLE = score(part("P1", measure(1, C44, tempo(120), *Q)))


# =========================================================================
# Music
# =========================================================================


def test_ode_to_joy_fixture_pitches_beats_bars_seconds_and_metadata():
    result = check(read_musicxml(FIXTURES / "ode_to_joy.musicxml"))

    assert result.title == "Ode to Joy"
    assert result.composer == "Ludwig van Beethoven"
    assert result.warnings == []
    assert result.tempo_bpm == 120
    assert result.timeline.tempos == (TempoChange(0.0, 120.0),)
    assert result.timeline.meters == (MeterChange(1, 4, 4),)
    assert result.timeline.keys == (KeyChange(0.0, 2, "major"),)
    assert result.timeline.pickup_beats == 0.0

    melody = [n for n in result.notes if n.staff == 1]
    assert [n.pitch for n in melody] == [66, 66, 67, 69, 69, 67, 66, 64, 62, 62, 64, 66, 66, 64, 64]
    assert [n.beat for n in melody] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13.5, 14]
    assert [n.beats for n in melody] == [1] * 12 + [1.5, 0.5, 2]
    assert [n.bar for n in melody] == [1] * 4 + [2] * 4 + [3] * 4 + [4] * 3
    assert [n.onset for n in melody] == [b / 2 for b in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13.5, 14]]
    assert [n.duration for n in melody[-3:]] == [0.75, 0.25, 1.0]
    assert melody[0].spelling == ("F", 1)  # F sharp, as written, not G flat
    assert melody[2].spelling == ("G", 0)
    assert {n.voice for n in melody} == {1}

    bass = [n for n in result.notes if n.staff == 2]
    assert [(n.pitch, n.beat, n.beats) for n in bass] == [
        (50, 0, 4), (57, 0, 4), (45, 4, 4), (52, 4, 4),
        (50, 8, 4), (57, 8, 4), (45, 12, 4), (52, 12, 4),
    ]
    assert {n.voice for n in bass} == {5}
    assert len(result.notes) == 23

    # ids: part, measure ordinal, k-th pitched note of that measure, all 0-based
    assert melody[0].id == "p0m0n0" and melody[3].id == "p0m0n3"
    assert [n.id for n in bass[:2]] == ["p0m0n4", "p0m0n5"]
    assert [n.id for n in melody[-3:]] == ["p0m3n0", "p0m3n1", "p0m3n2"]

    (track,) = result.tracks
    assert (track.name, track.program, track.channel) == ("Piano", 0, 0)  # 1-based -> 0-based
    assert (track.note_count, track.lowest_pitch, track.highest_pitch) == (23, 45, 69)
    assert track.is_percussion is False

    # one marking, written on the upper staff: both hands follow it, one note carries it
    assert {n.velocity for n in result.notes} == {49}
    assert [n.id for n in result.notes if n.dynamic] == ["p0m0n0"]


def test_reading_is_deterministic():
    a = read_musicxml(FIXTURES / "ode_to_joy.musicxml")
    b = read_musicxml(FIXTURES / "ode_to_joy.musicxml")
    assert a.notes == b.notes and a.timeline == b.timeline


def test_default_tempo_is_120_with_a_warning():
    result = load(score(part("P1", measure(1, C44, *Q))))
    assert result.tempo_bpm == 120
    assert warned(result, "No tempo is stated")
    assert [n.onset for n in result.notes] == [0.0, 0.5, 1.0, 1.5]


def test_chord_backup_forward_voices_and_staves():
    doc = score(part("P1", measure(
        1, attributes(divisions=2, fifths=0, time=(4, 4), staves=2), tempo(60),
        note("C", 5, 4, voice=1, staff=1),
        note("E", 5, 4, chord=True, voice=1, staff=1),
        note("G", 5, 4, chord=True, voice=1, staff=1),
        note("D", 5, 4, voice=1, staff=1),          # after the chord: beat 2, not beat 6
        backup(8),
        forward(2),                                 # voice 2 enters on beat 1
        note("A", 4, 6, voice=2, staff=1),
        backup(8),
        note("C", 3, 8, voice=5, staff=2),
    )))
    result = load(doc)
    got = {(n.pitch, n.beat, n.beats, n.voice, n.staff) for n in result.notes}
    assert got == {
        (72, 0.0, 2.0, 1, 1), (76, 0.0, 2.0, 1, 1), (79, 0.0, 2.0, 1, 1),
        (74, 2.0, 2.0, 1, 1),
        (69, 1.0, 3.0, 2, 1),
        (48, 0.0, 4.0, 5, 2),
    }
    assert by_id(result)["p0m0n4"].onset == 1.0  # 60 bpm: one beat is one second


def test_single_staff_part_has_no_staff_and_piano_part_maps_staves_through():
    midi = "<midi-instrument id='i'><midi-channel>2</midi-channel><midi-program>53</midi-program></midi-instrument>"
    doc = score(
        part("P1", measure(1, C44, tempo(120), note("E", 5, 4, voice=1))),
        part("P2", measure(
            1, attributes(divisions=1, staves=2),
            note("C", 4, 4, voice=1, staff=1), backup(4), note("C", 2, 4, voice=5, staff=2),
        )),
        part_list=score_part("P1", "Voice", midi) + score_part("P2", "Piano"),
    )
    result = load(doc)
    assert [(n.pitch, n.track, n.staff) for n in result.notes] == [
        (36, 1, 2), (60, 1, 1), (76, 0, None),
    ]
    voice, piano = result.tracks
    assert (voice.name, voice.program, voice.channel, voice.note_count) == ("Voice", 52, 1, 1)
    assert (piano.name, piano.program, piano.channel) == ("Piano", None, None)
    assert (piano.lowest_pitch, piano.highest_pitch) == (36, 60)


def test_divisions_may_change_mid_piece_and_differ_between_parts():
    doc = score(
        part("P1",
             measure(1, C44, tempo(120), *Q),
             measure(2, attributes(divisions=480), *[note("G", 4, 480)] * 4)),
        part("P2",
             measure(1, attributes(divisions=2), note("C", 3, 8)),
             measure(2, note("D", 3, 4), note("E", 3, 4))),
        part_list=score_part("P1", "A") + score_part("P2", "B"),
    )
    result = load(doc)
    upper = [n for n in result.notes if n.track == 0]
    lower = [n for n in result.notes if n.track == 1]
    assert [n.beat for n in upper] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert [(n.beat, n.beats) for n in lower] == [(0, 4), (4, 2), (6, 2)]


def amazing_grace(implicit=True):
    g = attributes(divisions=2, fifths=1, time=(3, 4))
    return score(part(
        "P1",
        measure(0, g, tempo(90), note("D", 4, 2), implicit=implicit),
        measure(1, note("G", 4, 4), note("B", 4, 1), note("G", 4, 1)),
        measure(2, note("B", 4, 4), note("A", 4, 2)),
        measure(3, note("G", 4, 4), note("E", 4, 2)),
        measure(4, note("D", 4, 4)),  # final bar: the two beats the pickup leaves over
    ))


@pytest.mark.parametrize("implicit", [True, False])
def test_pickup_is_bar_one_and_every_note_bar_agrees_with_the_timeline(implicit):
    result = load(amazing_grace(implicit))
    tl = result.timeline
    assert tl.pickup_beats == 1.0
    assert tl.meters == (MeterChange(1, 3, 4),)
    assert [(n.pitch, n.beat, n.bar) for n in result.notes] == [
        (62, 0.0, 1),
        (67, 1.0, 2), (71, 3.0, 2), (67, 3.5, 2),
        (71, 4.0, 3), (69, 6.0, 3),
        (67, 7.0, 4), (64, 9.0, 4),
        (62, 10.0, 5),
    ]
    for n in result.notes:  # the contract, stated outright
        assert n.bar == tl.bar_at(n.beat)
    assert tl.bar_start(2) == 1.0 and tl.bar_start(5) == 10.0
    assert result.notes[1].onset == pytest.approx(60 / 90)
    assert result.notes[0].id == "p0m0n0" and result.notes[-1].id == "p0m4n0"
    assert result.warnings == []  # a short last bar completing the pickup is normal


def test_a_single_short_measure_is_not_a_pickup():
    result = load(score(part("P1", measure(1, C44, tempo(120), note("C", 4, 2)))))
    assert result.timeline.pickup_beats == 0.0
    assert result.notes[0].bar == 1


def test_irregular_measure_does_not_shift_what_follows():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), *Q),
        measure(2, note("G", 4, 2)),                  # two beats in a 4/4 bar
        measure(3, *Q),
        measure(4, *Q),
    ))
    result = load(doc)
    assert [n.beat for n in result.notes] == [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13]
    assert [n.bar for n in result.notes] == [1, 1, 1, 1, 2, 3, 3, 3, 3, 4, 4, 4, 4]
    assert result.timeline.meters == (
        MeterChange(1, 4, 4), MeterChange(2, 2, 4), MeterChange(3, 4, 4),
    )
    assert warned(result, "did not match the time signature (bar 2)")


def test_measure_no_meter_can_express_still_keeps_bars_consistent():
    doc = score(part(
        "P1",
        measure(1, attributes(divisions=3, time=(4, 4)), tempo(120),
                *[note("C", 4, 3)] * 4, note("D", 4, 1)),  # 4 1/3 beats
        measure(2, *[note("E", 4, 3)] * 4),
    ))
    result = load(doc)  # check() proves bar == bar_at(beat) even here
    assert result.notes[5].beat == float(Fraction(13, 3))
    assert warned(result, "no time signature can express")


def test_empty_measure_takes_a_full_bar():
    doc = score(part("P1", measure(1, C44, tempo(120), *Q), measure(2), measure(3, *Q)))
    result = load(doc)
    assert [n.beat for n in result.notes][4:] == [8, 9, 10, 11]
    assert result.notes[4].bar == 3
    assert warned(result, "held no notes or rests")


def test_metronome_with_dotted_beat_unit_and_mid_piece_tempo_change():
    metronome = direction(
        "<metronome><beat-unit>quarter</beat-unit><beat-unit-dot/>"
        "<per-minute>80</per-minute></metronome>"
    )
    doc = score(part(
        "P1",
        measure(1, attributes(divisions=2, time=(6, 8)), metronome,
                note("C", 4, 3), note("D", 4, 3)),
        measure(2, tempo(60), note("E", 4, 3), note("F", 4, 3)),
    ))
    result = load(doc)
    # dotted quarter = 80  ->  quarter = 120
    assert result.timeline.tempos == (TempoChange(0.0, 120.0), TempoChange(3.0, 60.0))
    assert result.timeline.has_tempo_changes
    assert [n.beat for n in result.notes] == [0, 1.5, 3, 4.5]
    assert [n.onset for n in result.notes] == [0.0, 0.75, 1.5, 3.0]
    assert [n.duration for n in result.notes] == [0.75, 0.75, 1.5, 1.5]
    assert result.tempo_bpm == 120
    assert not warned(result, "tempo")


def test_a_note_held_across_a_tempo_change_gets_both_tempos():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), note("C", 4, 4), backup(2), tempo(60), forward(2)),
    ))
    (n,) = load(doc).notes
    assert n.duration == pytest.approx(1.0 + 2.0)  # two beats at 120, two at 60


def test_late_first_tempo_is_assumed_from_the_start_with_a_warning():
    doc = score(part("P1", measure(1, C44, *Q), measure(2, tempo(60), *Q)))
    result = load(doc)
    assert result.timeline.tempos == (TempoChange(0.0, 60.0), TempoChange(4.0, 60.0))
    assert warned(result, "first tempo marking comes after the music starts")


def test_metric_modulation_and_unusable_tempos_are_reported():
    modulation = direction(
        "<metronome><beat-unit>quarter</beat-unit><beat-unit>half</beat-unit></metronome>"
    )
    doc = score(part("P1", measure(
        1, C44, tempo(120), modulation,
        '<sound tempo="fast"/>', '<sound tempo="100000"/>', *Q,
    )))
    result = load(doc)
    assert warned(result, "metric modulation")
    assert warned(result, "1 tempo marking was not understood and ignored")
    assert warned(result, "1 tempo marking was outside 5-1000")


def test_meter_change_and_key_change():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), *Q),
        measure(2, *Q),
        measure(3, attributes(fifths=-3, mode="minor", time=(3, 4)), *Q[:3]),
        measure(4, *Q[:3]),
    ))
    result = load(doc)
    assert result.timeline.meters == (MeterChange(1, 4, 4), MeterChange(3, 3, 4))
    assert result.timeline.keys == (KeyChange(0.0, 0, "major"), KeyChange(8.0, -3, "minor"))
    assert [n.beat for n in result.notes][8:] == [8, 9, 10, 11, 12, 13]
    assert [n.bar for n in result.notes][8:] == [3, 3, 3, 4, 4, 4]
    assert result.timeline.key_at(9.0).fifths == -3
    assert result.warnings == []


def test_composite_time_signature_is_summed():
    time = "<time><beats>3+2</beats><beat-type>8</beat-type></time>"
    doc = score(part("P1", measure(
        1, attributes(divisions=2, extra=time), tempo(120), *[note("C", 4, 1)] * 5,
    ), measure(2, *[note("D", 4, 1)] * 5)))
    result = load(doc)
    assert result.timeline.meters == (MeterChange(1, 5, 8),)
    assert result.notes[5].beat == 2.5 and result.notes[5].bar == 2


def test_senza_misura_warns_and_assumes_four_four():
    attrs = attributes(divisions=1, extra="<time><senza-misura/></time>")
    result = load(score(part("P1", measure(1, attrs, tempo(120), *Q), measure(2, *Q))))
    assert result.timeline.meters == (MeterChange(1, 4, 4),)
    assert warned(result, "senza misura")


def test_missing_time_signature_warns_and_assumes_four_four():
    result = load(score(part("P1", measure(1, attributes(divisions=1), tempo(120), *Q))))
    assert result.timeline.meters == (MeterChange(1, 4, 4),)
    assert warned(result, "No time signature is stated")


def test_transposing_part_gives_sounding_pitch_and_drops_spelling():
    clarinet = attributes(
        divisions=1, fifths=2, time=(4, 4),
        extra="<transpose><diatonic>-1</diatonic><chromatic>-2</chromatic></transpose>",
    )
    doc = score(
        part("P1", measure(1, clarinet, tempo(120), note("D", 4, 2), note("F", 4, 2, alter=1))),
        part_list=score_part("P1", "Clarinet in B&#x266D;"),
    )
    result = load(doc)
    assert [n.pitch for n in result.notes] == [60, 64]  # written D4, F#4 sound C4, E4
    assert [n.spelling for n in result.notes] == [None, None]
    assert warned(result, "converted to sounding pitch")
    # written D major is concert C major
    assert result.timeline.keys == (KeyChange(0.0, 0, "major"),)


def test_octave_transposition_keeps_spelling_and_needs_no_warning():
    guitar = attributes(
        divisions=1, fifths=0, time=(4, 4),
        extra="<transpose><diatonic>0</diatonic><chromatic>0</chromatic>"
              "<octave-change>-1</octave-change></transpose>",
    )
    result = load(score(part("P1", measure(1, guitar, tempo(120), note("C", 4, 4, alter=1)))))
    (n,) = result.notes
    assert (n.pitch, n.spelling) == (49, ("C", 1))
    assert result.warnings == []


def test_microtonal_alter_is_rounded_with_a_warning():
    doc = score(part("P1", measure(
        1, C44, tempo(120), note("C", 4, 2, alter="0.5"), note("E", 4, 2, alter="-1"),
    )))
    result = load(doc)
    assert [(n.pitch, n.spelling) for n in result.notes] == [(61, ("C", 1)), (63, ("E", -1))]
    assert warned(result, "1 microtonal note")


def test_tie_across_a_barline_is_one_note_with_the_first_id():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), note("C", 4, 4, tie=["start"])),
        measure(2, note("C", 4, 2, tie=["stop"]), note("D", 4, 2)),
    ))
    result = load(doc)
    assert [(n.pitch, n.beat, n.beats, n.id) for n in result.notes] == [
        (60, 0.0, 6.0, "p0m0n0"),
        (62, 6.0, 2.0, "p0m1n1"),  # the continuation used up k=0 in its measure
    ]
    assert result.notes[0].duration == 3.0
    assert result.tracks[0].note_count == 2
    assert result.warnings == []


def test_tie_chain_over_three_bars():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), note("A", 3, 4, tie=["start"])),
        measure(2, note("A", 3, 4, tie=["stop", "start"])),
        measure(3, note("A", 3, 1, tie=["stop"]), note("B", 3, 3)),
    ))
    result = load(doc)
    assert [(n.pitch, n.beat, n.beats) for n in result.notes] == [(57, 0, 9), (59, 9, 3)]


def test_ties_survive_backups_and_match_by_voice():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120),
                note("E", 5, 4, voice=1, tie=["start"]), backup(4),
                note("C", 4, 4, voice=2, tie=["start"])),
        measure(2,
                note("E", 5, 4, voice=1, tie=["stop"]), backup(4),
                note("C", 4, 2, voice=2, tie=["stop"]), note("D", 4, 2, voice=2)),
    ))
    result = load(doc)
    assert [(n.pitch, n.beat, n.beats, n.voice, n.id) for n in result.notes] == [
        (60, 0.0, 6.0, 2, "p0m0n1"),
        (76, 0.0, 8.0, 1, "p0m0n0"),
        (62, 6.0, 2.0, 2, "p0m1n2"),
    ]


def test_tied_chord_merges_note_by_note():
    doc = score(part("P1", measure(
        1, C44, tempo(120),
        note("C", 4, 2, tie=["start"]), note("E", 4, 2, chord=True, tie=["start"]),
        note("C", 4, 2, tie=["stop"]), note("E", 4, 2, chord=True),  # E is re-struck
    )))
    result = load(doc)
    assert [(n.pitch, n.beat, n.beats) for n in result.notes] == [
        (60, 0, 4), (64, 0, 2), (64, 2, 2),
    ]
    assert warned(result, "1 tie had no matching note")


def test_tied_element_is_the_fallback_when_tie_is_absent():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), note("G", 4, 4, tied=["start"])),
        measure(2, note("G", 4, 4, tied=["stop"])),
    ))
    (n,) = load(doc).notes
    assert (n.beat, n.beats) == (0.0, 8.0)


def test_unterminated_tie_swallows_nothing():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), note("C", 4, 1, tie=["start"]), *Q[1:]),
        measure(2, note("C", 4, 1), *Q[1:]),                 # same pitch, no tie stop
        measure(3, note("C", 4, 1, tie=["start"]), rest(1),  # a gap, then a "stop"
                note("C", 4, 1, tie=["stop"]), rest(1)),
    ))
    result = load(doc)
    cs = [(n.beat, n.beats) for n in result.notes if n.pitch == 60]
    assert cs == [(0, 1), (4, 1), (8, 1), (10, 1)]
    assert len(result.notes) == 10
    assert warned(result, "3 ties had no matching note")


def test_triplets_land_on_exact_thirds():
    mod = ("<time-modification><actual-notes>3</actual-notes>"
           "<normal-notes>2</normal-notes></time-modification>")
    triplets = [note("CDE"[k % 3], 4, 1, time_mod=mod) for k in range(12)]
    doc = score(part("P1", measure(1, attributes(divisions=3, time=(4, 4)), tempo(120), *triplets),
                     measure(2, note("C", 5, 12))))
    result = load(doc)
    for k, n in enumerate(result.notes[:12]):
        assert n.beat == k / 3 == float(Fraction(k, 3))   # exactly, not approximately
        assert n.beats == 1 / 3
    assert result.notes[12].beat == 4.0 and result.notes[12].bar == 2
    assert result.warnings == []  # twelve thirds fill the bar exactly: nothing irregular


def test_grace_notes_are_skipped_with_one_counted_warning():
    grace = "<note><grace slash='yes'/><pitch><step>B</step><octave>4</octave></pitch><voice>1</voice></note>"
    doc = score(part("P1", measure(1, C44, tempo(120), grace, Q[0], grace, grace, *Q[1:])))
    result = load(doc)
    assert [n.pitch for n in result.notes] == [60, 62, 64, 65]
    assert [n.beat for n in result.notes] == [0, 1, 2, 3]
    assert [n.id for n in result.notes] == ["p0m0n0", "p0m0n1", "p0m0n2", "p0m0n3"]
    assert [w for w in result.warnings if "grace" in w] == [
        "3 grace notes were skipped: grace notes have no duration of their own."
    ]


def test_cue_notes_and_ornaments_are_reported():
    trill = "<ornaments><trill-mark/></ornaments>"
    doc = score(part("P1", measure(
        1, C44, tempo(120),
        note("C", 4, 2, notations=trill),
        note("G", 5, 2, extra="").replace("<pitch>", "<cue/><pitch>"),
    )))
    result = load(doc)
    assert [n.pitch for n in result.notes] == [60]
    assert warned(result, "1 cue note") and warned(result, "1 ornament")


def drum_part():
    hit = ("<note><unpitched><display-step>C</display-step><display-octave>5</display-octave>"
           "</unpitched><duration>1</duration></note>")
    return part("P2", measure(1, attributes(divisions=1), *[hit] * 4))


def test_percussion_is_flagged_and_left_out_with_a_warning():
    midi = "<midi-instrument id='d'><midi-channel>10</midi-channel><midi-program>1</midi-program></midi-instrument>"
    doc = score(
        part("P1", measure(1, C44, tempo(120), *Q)), drum_part(),
        part("P3", measure(1, attributes(divisions=1), note("C", 2, 4))),  # "pitched", on channel 10
        part_list=score_part("P1", "Piano") + score_part("P2", "Drums") + score_part("P3", "Kit", midi),
    )
    result = load(doc)
    assert {n.track for n in result.notes} == {0}
    piano, drums, kit = result.tracks
    assert (piano.is_percussion, drums.is_percussion, kit.is_percussion) == (False, True, True)
    assert (drums.note_count, drums.lowest_pitch, kit.channel) == (0, None, 9)
    assert warned(result, "5 unpitched percussion notes were left out")


def test_score_with_only_percussion_is_empty():
    doc = score(drum_part().replace('id="P2"', 'id="P1"'))
    with pytest.raises(EmptyScore) as err:
        read_musicxml_bytes(doc)
    assert "no pitched notes" in err.value.public


def test_score_with_only_rests_is_empty():
    with pytest.raises(EmptyScore):
        read_musicxml_bytes(score(part("P1", measure(1, C44, rest(4)))))


def test_dynamics_set_velocity_and_mark_only_the_first_note():
    doc = score(part("P1", measure(
        1, C44, tempo(120),
        note("C", 4, 1),                         # before any marking
        dynamics("p"), note("D", 4, 1), note("E", 4, 1),
        dynamics("ff"), note("F", 4, 1),
    ), measure(
        2, note("G", 4, 1, attrs='dynamics="50"'),  # its own value beats the prevailing ff
        dynamics("pp"), note("A", 4, 1),
        dynamics("sfz"), note("B", 4, 1), note("C", 5, 1),
    )))
    result = load(doc)
    assert [(n.velocity, n.dynamic) for n in result.notes] == [
        (80, None), (49, "p"), (49, None), (112, "ff"),
        (45, None), (33, "pp"), (96, "sfz"), (33, None),   # after the sfz it is pp again
    ]


def test_every_named_dynamic_level():
    marks = ["pp", "p", "mp", "mf", "f", "ff"]
    body = [x for m in marks for x in (dynamics(m), note("C", 4, 1))]
    doc = score(part("P1", measure(1, attributes(divisions=1, time=(6, 4)), tempo(120), *body)))
    assert [n.velocity for n in load(doc).notes] == [33, 49, 64, 80, 96, 112]


def test_dynamics_per_staff_and_shared_between_staves():
    def piano(lower_marking):
        return score(part("P1", measure(
            1, attributes(divisions=1, time=(4, 4), staves=2), tempo(120),
            dynamics("f", staff=1), note("C", 5, 4, staff=1), backup(4),
            lower_marking, note("C", 3, 4, staff=2),
        )))

    shared = load(piano(""))
    assert [(n.staff, n.velocity, n.dynamic) for n in shared.notes] == [(2, 96, None), (1, 96, "f")]
    split = load(piano(dynamics("p", staff=2)))
    assert [(n.staff, n.velocity, n.dynamic) for n in split.notes] == [(2, 49, "p"), (1, 96, "f")]


def test_articulations_fermata_and_arpeggio():
    def arts(*names):
        return "<articulations>" + "".join(f"<{n}/>" for n in names) + "</articulations>"

    doc = score(part("P1", measure(
        1, C44, tempo(120),
        note("C", 4, 1, notations=arts("staccato", "accent")),
        note("D", 4, 1, notations=arts("strong-accent", "tenuto")),
        note("E", 4, 1, notations=arts("staccatissimo") + "<fermata/>"),
        note("F", 4, 1, notations="<arpeggiate/>"),
        note("A", 4, 1, chord=True, notations="<arpeggiate/>"),
    ), measure(2, note("G", 4, 4))))
    result = load(doc)
    got = {n.pitch: (n.articulations, n.rolled) for n in result.notes}
    assert got == {
        60: (("staccato", "accent"), False),
        62: (("marcato", "tenuto"), False),
        64: (("staccatissimo", "fermata"), False),
        65: ((), True),
        69: ((), True),
        67: ((), False),
    }


def test_chord_articulations_written_once_apply_to_the_whole_chord():
    staccato = "<articulations><staccato/></articulations>"
    doc = score(part("P1", measure(
        1, C44, tempo(120),
        note("C", 4, 2, notations=staccato), note("E", 4, 2, chord=True), note("G", 4, 2, chord=True),
        note("D", 4, 2), note("F", 4, 2, chord=True, notations="<articulations><tenuto/></articulations>"),
    )))
    got = {n.pitch: n.articulations for n in load(doc).notes}
    assert got == {60: ("staccato",), 64: ("staccato",), 67: ("staccato",), 62: (), 65: ("tenuto",)}


def test_dynamic_lands_on_the_note_it_was_written_in_front_of():
    doc = score(part("P1", measure(
        1, C44, tempo(120),
        note("E", 5, 4, voice=1), backup(4),
        dynamics("f"), note("C", 4, 4, voice=2),   # same instant as the E, later in the file
    )))
    result = load(doc)
    assert {n.pitch: n.dynamic for n in result.notes} == {76: None, 60: "f"}
    assert {n.velocity for n in result.notes} == {96}


def test_note_id_attribute_is_kept_when_usable_and_never_duplicated():
    def with_id(value, step):
        return note(step, 4, 1, attrs=f'id="{value}"')

    doc = score(part(
        "P1",
        measure(1, C44, tempo(120),
                with_id("rh-1.a_b", "C"),       # a plain XML name: kept
                with_id("rh-1.a_b", "D"),       # already used: generated instead
                with_id("9lives", "E"),         # not a name: generated
                with_id("has space", "F")),
        measure(2,
                with_id("p0m1n1", "C"),         # squats on the id the next note would get
                note("D", 4, 1),
                with_id("x" * 129, "E"),        # absurdly long: generated
                note("F", 4, 1, tie=["start"], attrs='id="tie-head"')),
        measure(3, note("F", 4, 4, tie=["stop"], attrs='id="tie-head-t1"')),
    ))
    result = load(doc)  # check() asserts uniqueness
    assert [n.id for n in sorted(result.notes, key=lambda n: n.beat)] == [
        "rh-1.a_b", "p0m0n1", "p0m0n2", "p0m0n3",
        "p0m1n1", "p0m1n1_2", "p0m1n2", "tie-head",
    ]
    assert by_id(result)["tie-head"].beats == 5.0


def test_pedal_start_change_stop_and_unclosed_pedal():
    doc = score(part(
        "P1",
        measure(1, C44, tempo(120),
                pedal("start"), note("C", 4, 2), pedal("change"), note("D", 4, 2), pedal("stop")),
        measure(2, note("E", 4, 1), pedal("start"), note("F", 4, 3)),   # never lifted
    ))
    result = load(doc)
    assert result.pedals == [PedalSpan(0.0, 1.0), PedalSpan(1.0, 2.0), PedalSpan(2.5, 4.0)]
    assert result.pedal_down_at(0.5) and result.pedal_down_at(1.5)
    assert not result.pedal_down_at(2.2) and result.pedal_down_at(3.9)


def test_repeats_and_jumps_are_imported_once_with_a_clear_warning():
    def barline(side, inner):
        return f'<barline location="{side}">{inner}</barline>'

    doc = score(part(
        "P1",
        measure(1, C44, tempo(120), barline("left", '<repeat direction="forward"/>'), *Q),
        measure(2, barline("left", '<ending number="1" type="start"/>'), *Q,
                barline("right", '<ending number="1" type="stop"/><repeat direction="backward"/>')),
        measure(3, barline("left", '<ending number="2" type="start"/>'), *Q,
                '<sound dacapo="yes"/>'),
    ))
    result = load(doc)
    assert len(result.notes) == 12  # as written, once
    matching = [w for w in result.warnings if "not expanded" in w]
    assert len(matching) == 1
    for word in ("repeat barlines", "volta endings", "D.C."):
        assert word in matching[0]


def test_timewise_is_converted_to_the_same_score_as_partwise():
    a1 = C44 + tempo(120) + "".join(Q)
    a2 = note("G", 4, 4, tie=["start"])
    a3 = note("G", 4, 4, tie=["stop"])
    b1 = attributes(divisions=2) + note("C", 3, 8)
    b2 = note("D", 3, 8)
    b3 = note("E", 3, 8)
    plist = score_part("P1", "Upper") + score_part("P2", "Lower")
    partwise = score(
        part("P1", measure(1, a1), measure(2, a2), measure(3, a3)),
        part("P2", measure(1, b1), measure(2, b2), measure(3, b3)),
        part_list=plist,
    )
    timewise = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE score-timewise PUBLIC "-//Recordare//DTD MusicXML 4.0 Timewise//EN" '
        '"http://www.musicxml.org/dtds/timewise.dtd">'
        f'<score-timewise version="4.0"><part-list>{plist}</part-list>'
        + "".join(
            f'<measure number="{i}"><part id="P1">{a}</part><part id="P2">{b}</part></measure>'
            for i, (a, b) in enumerate([(a1, b1), (a2, b2), (a3, b3)], start=1)
        )
        + "</score-timewise>"
    ).encode()
    expected, got = load(partwise), load(timewise)
    assert got.notes == expected.notes
    assert len(got.notes) == 8 and got.notes[-1].beats in (4.0, 8.0)
    assert got.timeline == expected.timeline
    assert got.tracks == expected.tracks
    assert [n.beats for n in got.notes if n.pitch == 67] == [8.0]


def test_mxl_happy_path_matches_the_uncompressed_file(tmp_path):
    raw = (FIXTURES / "ode_to_joy.musicxml").read_bytes()
    archive = make_mxl({"score.musicxml": raw, "images/cover.png": b"\x89PNG\r\n\x1a\n" + b"0" * 64})
    plain = read_musicxml_bytes(raw)
    packed = load(archive, filename="ode.mxl")
    assert packed.notes == plain.notes and packed.title == "Ode to Joy"

    path = tmp_path / "ode.mxl"
    path.write_bytes(archive)
    assert check(read_musicxml(path)).notes == plain.notes
    assert check(read_musicxml(str(FIXTURES / "ode_to_joy.musicxml"))).notes == plain.notes


def test_mxl_without_container_falls_back_to_the_first_score_entry():
    archive = make_mxl(
        {"META-INF/notes.xml": b"<nope/>", "../../evil.xml": b"<nope/>", "music.xml": SIMPLE},
        rootfile=None,
    )
    assert [n.pitch for n in load(archive).notes] == [60, 62, 64, 65]


def test_utf16_and_namespace_free_parsing():
    text = SIMPLE.decode().replace('encoding="UTF-8"', 'encoding="UTF-16"')
    assert [n.pitch for n in load(text.encode("utf-16")).notes] == [60, 62, 64, 65]


@pytest.mark.parametrize(
    ("header", "filename", "expected"),
    [
        ("<work><work-title>Work</work-title></work><movement-title>Mvt</movement-title>", "f.xml", "Work"),
        ("<work><work-title>  </work-title></work><movement-title>Mvt</movement-title>", "f.xml", "Mvt"),
        ("<credit><credit-type>title</credit-type><credit-words>From Credit</credit-words></credit>",
         "f.xml", "From Credit"),
        ("<credit><credit-type>composer</credit-type><credit-words>X</credit-words></credit>",
         "C:\\scores\\my song.mxl", "my song"),
        ("", "../up/ode.to.joy.musicxml", "ode.to.joy"),
        ("", None, "untitled"),
    ],
)
def test_title_fallback_chain(header, filename, expected):
    # <credit> must follow <part-list>'s predecessors in a real file; order is not our business
    doc = score(part("P1", measure(1, C44, tempo(120), *Q)), header=header)
    assert load(doc, filename=filename).title == expected


def test_title_and_composer_are_cleaned_and_truncated():
    header = (
        "<work><work-title>Evil&#x202E;gnp.exe \n\t Title&#x85;" + "x" * 500 + "</work-title></work>"
        "<identification><creator type='lyricist'>L</creator>"
        "<creator type='composer'> J. S.\nBach </creator></identification>"
    )
    doc = score(part("P1", measure(1, C44, tempo(120), *Q)), header=header)
    result = load(doc, limits=ImportLimits(max_title_chars=20))
    assert result.title == "Evilgnp.exe Titlexxx"
    assert len(result.title) == 20
    assert result.composer == "J. S. Bach"


def test_sniff():
    assert sniff_musicxml(SIMPLE)
    assert sniff_musicxml((FIXTURES / "ode_to_joy.musicxml").read_bytes())
    assert sniff_musicxml(b'<?xml version="1.0"?><!-- c --><score-timewise/>')
    assert sniff_musicxml(SIMPLE.decode().replace("UTF-8", "UTF-16").encode("utf-16"))
    assert sniff_musicxml(SIMPLE[:300])  # a truncated head is enough
    assert sniff_musicxml(make_mxl({"score.musicxml": SIMPLE}))
    assert not sniff_musicxml(make_mxl({"score.musicxml": SIMPLE}, rootfile=None))
    assert not sniff_musicxml(b"<html><body/></html>")
    assert not sniff_musicxml(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0")
    assert not sniff_musicxml(b"")
    assert not sniff_musicxml(b"PK\x03\x04 not really a zip")
    assert not sniff_musicxml(b"\x00\x01\x02\x03" * 100)


# =========================================================================
# Security: XML
# =========================================================================


def hostile(internal_subset: str, body: str = "&x;") -> bytes:
    return (
        f'<?xml version="1.0"?><!DOCTYPE score-partwise [{internal_subset}]>'
        f"<score-partwise><work><work-title>{body}</work-title></work>"
        f"<part-list/></score-partwise>"
    ).encode()


def test_public_doctype_is_accepted_without_touching_the_network():
    doc = score(part("P1", measure(1, C44, tempo(120), *Q)), prolog=PUBLIC_DOCTYPE)
    assert [n.pitch for n in load(doc).notes] == [60, 62, 64, 65]
    # nor is a SYSTEM identifier ever opened: this one does not exist
    local = '<!DOCTYPE score-partwise SYSTEM "file:///definitely/not/here.dtd">'
    assert len(load(score(part("P1", measure(1, C44, tempo(120), *Q)), prolog=local)).notes) == 4


def test_xxe_file_read_is_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-TOKEN")
    doc = hostile(f'<!ENTITY x SYSTEM "{secret.as_uri()}">')
    with pytest.raises(UnsafeContent) as err:
        read_musicxml_bytes(doc)
    assert "TOP-SECRET-TOKEN" not in err.value.public + err.value.detail
    assert err.value.code == "unsafe_content"

    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(hostile('<!ENTITY x SYSTEM "file:///etc/passwd">'))


def test_billion_laughs_is_refused():
    subset = '<!ENTITY a0 "lol">' + "".join(
        f'<!ENTITY a{i} "{f"&a{i - 1};" * 10}">' for i in range(1, 10)
    )
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(hostile(subset, "&a9;"))


def test_quadratic_blowup_is_refused():
    doc = hostile(f'<!ENTITY x "{"A" * 100_000}">', "&x;" * 20_000)
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(doc)


def test_internal_subset_without_any_entity_is_refused():
    with pytest.raises(UnsafeContent) as err:
        read_musicxml_bytes(hostile("<!ELEMENT score-partwise ANY>", "fine"))
    assert "internal subset" in err.value.detail
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(hostile("", "fine"))  # even an empty one


def test_external_parameter_entity_and_external_dtd_entities_are_refused():
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(hostile('<!ENTITY % remote SYSTEM "http://127.0.0.1:9/evil.dtd"> %remote;'))
    # No internal subset at all: the entity could only come from the remote DTD.
    doc = (
        '<?xml version="1.0"?><!DOCTYPE score-partwise SYSTEM "http://127.0.0.1:9/evil.dtd">'
        "<score-partwise><work><work-title>&xxe;</work-title></work></score-partwise>"
    ).encode()
    with pytest.raises(UnsafeContent) as err:
        read_musicxml_bytes(doc)
    assert "xxe" in err.value.detail and "xxe" not in err.value.public


def test_ten_megabyte_nested_entity_payload_is_rejected_fast():
    subset = f'<!ENTITY a0 "{"A" * (10 * 1024 * 1024)}">' + "".join(
        f'<!ENTITY a{i} "{f"&a{i - 1};" * 10}">' for i in range(1, 8)
    )
    doc = hostile(subset, "&a7;")
    assert len(doc) > 10 * 1024 * 1024
    roomy = ImportLimits(max_bytes=64 * 1024 * 1024)

    started = time.perf_counter()
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(doc, limits=roomy)
    with pytest.raises(LimitExceeded):  # and under default limits it never reaches the parser
        read_musicxml_bytes(doc)
    assert sniff_musicxml(doc)  # routed to the importer, which refuses it; still no expansion
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"took {elapsed:.3f}s"


def test_excessive_depth_is_stopped_during_parsing():
    deep = b"<score-partwise>" + b"<a>" * 100 + b"</a>" * 100 + b"</score-partwise>"
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(deep)
    assert "max_xml_depth" in err.value.detail
    # The closing tags are missing here. Reaching them would be MalformedFile:
    # LimitExceeded proves the parser was stopped on the way down.
    started = time.perf_counter()
    with pytest.raises(LimitExceeded):
        read_musicxml_bytes(b"<score-partwise>" + b"<a>" * 1_000_000)
    assert time.perf_counter() - started < 0.5


def test_element_count_is_stopped_during_parsing():
    limits = ImportLimits(max_events=100)
    flood = b"<score-partwise>" + b"<a/>" * 5000 + b"<<< not xml"
    with pytest.raises(LimitExceeded) as err:  # not MalformedFile: it never got that far
        read_musicxml_bytes(flood, limits=limits)
    assert "max_events" in err.value.detail
    assert len(load(SIMPLE, limits=ImportLimits(max_events=40)).notes) == 4


@pytest.mark.parametrize(
    "data",
    [
        SIMPLE[: len(SIMPLE) // 2],                       # truncated
        b"<score-partwise><part-list></score-partwise>",  # mismatched
        b"\x00\x01\x02 garbage \xff\xfe",
        b"   \n  ",
        b"",
    ],
)
def test_truncated_or_garbage_xml_is_malformed(data):
    with pytest.raises(MalformedFile) as err:
        read_musicxml_bytes(data)
    assert err.value.code == "malformed_file"
    for internal in ("line", "column", "expat", "Traceback"):
        assert internal not in err.value.public


def test_parser_internals_go_in_detail_not_in_the_public_message():
    with pytest.raises(MalformedFile) as err:
        read_musicxml_bytes(SIMPLE[:-20])
    assert "line" in err.value.detail and "column" in err.value.detail
    assert str(err.value) == err.value.public == "This file is damaged or is not valid XML."


@pytest.mark.parametrize("root", ["html", "svg", "container", "opus", "score"])
def test_other_xml_is_unsupported_not_malformed(root):
    with pytest.raises(UnsupportedFormat) as err:
        read_musicxml_bytes(f"<{root}><a/></{root}>".encode())
    assert err.value.code == "unsupported_format"
    # decided at the root element: the rest of the document is never parsed
    with pytest.raises(UnsupportedFormat):
        read_musicxml_bytes(f"<{root}><<<< broken".encode())


# =========================================================================
# Security: .mxl archives
# =========================================================================


def patch_zip(data: bytes, *, flags=None, file_size=None, name=b"score.musicxml") -> bytes:
    """Rewrite header fields of one entry, in both the local and central header."""
    out = bytearray(data)
    for signature, flag_at, size_at, name_at in ((b"PK\x03\x04", 6, 22, 30), (b"PK\x01\x02", 8, 24, 46)):
        start = 0
        while (at := out.find(signature, start)) != -1:
            start = at + 4
            if out[at + name_at : at + name_at + len(name)] != name:
                continue
            if flags is not None:
                struct.pack_into("<H", out, at + flag_at, flags)
            if file_size is not None:
                struct.pack_into("<I", out, at + size_at, file_size)
    return bytes(out)


def test_zip_bomb_is_refused_on_ratio():
    bomb = make_mxl({"score.musicxml": b" " * (20 * 1024 * 1024)})
    assert len(bomb) < 64 * 1024
    started = time.perf_counter()
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(bomb)
    assert "max_compression_ratio" in err.value.detail
    assert time.perf_counter() - started < 0.5  # refused from the headers, nothing inflated


def test_too_many_archive_entries():
    files = {f"extra/{i}.txt": b"x" for i in range(DEFAULT_LIMITS.max_archive_entries)}
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(make_mxl({"score.musicxml": SIMPLE, **files}))
    assert "max_archive_entries" in err.value.detail


def test_declared_sizes_are_capped_per_entry_and_in_total():
    limits = ImportLimits(max_uncompressed_bytes=len(SIMPLE) + 2000)
    assert len(load(make_mxl({"score.musicxml": SIMPLE}), limits=limits).notes) == 4
    noise = bytes(range(256)) * 12  # 3 KB, incompressible enough to pass the ratio check
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(make_mxl({"score.musicxml": SIMPLE + noise}), limits=limits)
    assert "declares" in err.value.detail
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(
            make_mxl({"score.musicxml": SIMPLE, "a.bin": noise[:1500], "b.bin": noise[:1500]},
                     compression=zipfile.ZIP_STORED),
            limits=limits,
        )
    assert "in total" in err.value.detail


def test_header_that_lies_about_size_never_yields_the_hidden_bytes():
    filler = b"".join(hashlib.sha256(bytes([i % 251, i // 251])).hexdigest().encode() for i in range(3000))
    padded = SIMPLE + b"<!-- " + filler + b" -->"  # ~190 KB that deflate cannot shrink much
    honest = make_mxl({"score.musicxml": padded})
    assert len(load(honest).notes) == 4
    lying = patch_zip(honest, file_size=len(SIMPLE))  # claims to be small
    assert zipfile.ZipFile(io.BytesIO(lying)).getinfo("score.musicxml").file_size == len(SIMPLE)
    with pytest.raises(ScoreImportError) as err:
        read_musicxml_bytes(lying, limits=ImportLimits(max_uncompressed_bytes=len(SIMPLE) + 4096))
    assert isinstance(err.value, (MalformedFile, UnsafeContent, LimitExceeded))


class _LyingArchive:
    """Stands in for a ZipFile whose entry inflates to far more than it declared."""

    def __init__(self, produced: int):
        self.left = produced
        self.served = 0

    def open(self, info):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        chunk = b"A" * min(n, self.left)
        self.left -= len(chunk)
        self.served += len(chunk)
        return chunk


def _info(file_size: int, compress_size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo("score.musicxml")
    info.file_size, info.compress_size = file_size, compress_size
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def test_limits_are_enforced_while_inflating_not_only_from_headers():
    ten_mb = 10 * 1024 * 1024
    # more bytes than the header declared
    archive = _LyingArchive(ten_mb)
    with pytest.raises(UnsafeContent):
        mx._read_entry(archive, _info(1000, 900), DEFAULT_LIMITS, 0)
    assert archive.served <= 2 * 65536  # stopped at the first chunk past the lie

    # the header is "honest" about a size the archive-wide cap forbids
    archive = _LyingArchive(ten_mb)
    with pytest.raises(LimitExceeded) as err:
        mx._read_entry(archive, _info(ten_mb, ten_mb), ImportLimits(max_uncompressed_bytes=300_000), 200_000)
    assert "max_uncompressed_bytes" in err.value.detail
    assert archive.served <= 300_000

    # inflating past the permitted ratio
    archive = _LyingArchive(ten_mb)
    with pytest.raises(LimitExceeded) as err:
        mx._read_entry(archive, _info(ten_mb, 100), DEFAULT_LIMITS, 0)
    assert "max_compression_ratio" in err.value.detail
    assert archive.served <= 2 * 65536


@pytest.mark.parametrize(
    "rootfile", ["../../evil.musicxml", "/etc/passwd", "C:\\Windows\\win.ini", "a/../../b.xml", ""]
)
def test_traversing_or_absolute_rootfile_is_refused(rootfile, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = make_mxl({"score.musicxml": SIMPLE, rootfile or "x.xml": SIMPLE}, rootfile=rootfile)
    with pytest.raises(UnsafeContent) as err:
        read_musicxml_bytes(archive)
    assert "full-path" in err.value.detail
    assert list(tmp_path.iterdir()) == []  # nothing was ever written anywhere


def test_traversal_entry_names_are_harmless_lookup_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = make_mxl({"../../evil.musicxml": b"<evil/>", "score.musicxml": SIMPLE})
    assert [n.pitch for n in load(archive).notes] == [60, 62, 64, 65]
    assert list(tmp_path.iterdir()) == [] and not (tmp_path.parent.parent / "evil.musicxml").exists()


def test_container_naming_a_missing_file_is_malformed():
    with pytest.raises(MalformedFile):
        read_musicxml_bytes(make_mxl({"other.musicxml": SIMPLE}, rootfile="score.musicxml"))


def test_encrypted_entry_is_refused():
    archive = patch_zip(make_mxl({"score.musicxml": SIMPLE}), flags=0x0001)
    assert zipfile.ZipFile(io.BytesIO(archive)).getinfo("score.musicxml").flag_bits & 1
    with pytest.raises(UnsafeContent) as err:
        read_musicxml_bytes(archive)
    assert "encrypted" in err.value.detail


def test_nested_archives_are_refused_by_name_and_by_content():
    inner = make_mxl({"score.musicxml": SIMPLE})
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(make_mxl({"score.musicxml": SIMPLE, "more/inner.MXL": inner}))
    with pytest.raises(UnsafeContent) as err:  # an archive behind an innocent name
        read_musicxml_bytes(make_mxl({"score.musicxml": SIMPLE, "cover.png": inner}))
    assert "archive magic" in err.value.detail
    with pytest.raises(UnsafeContent):  # the score itself is an archive
        read_musicxml_bytes(make_mxl({"score.musicxml": inner}))


def test_hostile_container_xml_gets_the_same_xml_defences():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<!DOCTYPE container [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                    '<container><rootfiles><rootfile full-path="&x;"/></rootfiles></container>')
        zf.writestr("score.musicxml", SIMPLE)
    with pytest.raises(UnsafeContent):
        read_musicxml_bytes(buf.getvalue())


def test_damaged_and_unsupported_archives():
    with pytest.raises(MalformedFile) as err:
        read_musicxml_bytes(b"PK\x03\x04" + b"\x00" * 200)
    assert "zip" in err.value.detail and "zip" not in err.value.public.lower()
    good = make_mxl({"score.musicxml": SIMPLE})
    with pytest.raises(MalformedFile):
        read_musicxml_bytes(good[: len(good) // 2])
    with pytest.raises(UnsupportedFormat):
        read_musicxml_bytes(make_mxl({"readme.txt": b"hello"}, rootfile=None))
    pytest.importorskip("bz2")
    with pytest.raises(UnsupportedFormat):
        read_musicxml_bytes(make_mxl({"score.musicxml": SIMPLE}, compression=zipfile.ZIP_BZIP2))


# =========================================================================
# Limits
# =========================================================================


def test_oversize_raw_input(tmp_path):
    limits = ImportLimits(max_bytes=len(SIMPLE) - 1)
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(SIMPLE, limits=limits)
    assert err.value.code == "limit_exceeded" and "max_bytes" in err.value.detail
    path = tmp_path / "big.musicxml"
    path.write_bytes(SIMPLE)
    with pytest.raises(LimitExceeded):
        read_musicxml(path, limits=limits)
    assert len(read_musicxml(path, limits=ImportLimits(max_bytes=len(SIMPLE))).notes) == 4


def test_too_many_notes_counts_sounding_notes_not_tie_fragments():
    tied = score(part(
        "P1",
        measure(1, C44, tempo(120), *Q[:3], note("F", 4, 1, tie=["start"])),
        measure(2, note("F", 4, 4, tie=["stop"])),
    ))
    assert len(load(tied, limits=ImportLimits(max_notes=4)).notes) == 4
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(tied, limits=ImportLimits(max_notes=3))
    assert "max_notes" in err.value.detail


def test_too_many_parts_bars_and_seconds():
    three = score(*[part(f"P{i}", measure(1, C44, tempo(120), *Q)) for i in (1, 2, 3)],
                  part_list="".join(score_part(f"P{i}", "x") for i in (1, 2, 3)))
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(three, limits=ImportLimits(max_tracks=2))
    assert "max_tracks" in err.value.detail

    five_bars = score(part("P1", measure(1, C44, tempo(120), *Q), *[measure(i, *Q) for i in range(2, 6)]))
    assert len(load(five_bars, limits=ImportLimits(max_bars=5)).notes) == 20
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(five_bars, limits=ImportLimits(max_bars=4))
    assert "max_bars" in err.value.detail

    # 20 beats at 120 bpm is 10 seconds
    assert load(five_bars, limits=ImportLimits(max_duration_seconds=10)).duration() == 10.0
    with pytest.raises(LimitExceeded) as err:
        read_musicxml_bytes(five_bars, limits=ImportLimits(max_duration_seconds=9.5))
    assert "max_duration_seconds" in err.value.detail

    one_huge_note = score(part("P1", measure(1, C44, tempo(120), note("C", 4, 999_999_999_999))))
    with pytest.raises(LimitExceeded):
        read_musicxml_bytes(one_huge_note)


def test_hostile_numbers_cost_nothing():
    started = time.perf_counter()
    doc = score(part("P1", measure(
        1, C44, tempo(120), *Q,
        note("C", 4, "1e999999999"), note("D", 4, "9" * 5000), note("E", 99999999999, 1),
    )))
    result = load(doc)
    assert [n.pitch for n in result.notes] == [60, 62, 64, 65]
    assert warned(result, "2 durations were unreadable")
    assert warned(result, "1 note was skipped: no usable pitch")

    with pytest.raises(MalformedFile):
        read_musicxml_bytes(score(part("P1", measure(1, attributes(divisions=0), *Q))))
    with pytest.raises(MalformedFile):
        read_musicxml_bytes(score(part("P1", measure(1, attributes(divisions="1e9999999"), *Q))))

    # Mutually prime <divisions> in every measure: an attack on exact arithmetic.
    primes = [p for p in range(2, 400) if all(p % d for d in range(2, int(p**0.5) + 1))]
    measures = [measure(i, attributes(divisions=p), note("C", 4, 1)) for i, p in enumerate(primes)]
    with pytest.raises(MalformedFile) as err:
        read_musicxml_bytes(score(part("P1", *measures)))
    assert "denominator" in err.value.detail
    assert time.perf_counter() - started < 1.0


def test_every_error_is_typed_and_carries_public_and_detail():
    for exc_type in (UnsafeContent, LimitExceeded, MalformedFile, UnsupportedFormat, EmptyScore):
        assert issubclass(exc_type, ScoreImportError)
    with pytest.raises(ScoreImportError) as err:
        read_musicxml_bytes(hostile('<!ENTITY x "y">'))
    assert err.value.public and err.value.detail and err.value.public != err.value.detail
    assert "DOCTYPE" in err.value.detail and "DOCTYPE" not in err.value.public


def test_entity_handlers_are_a_second_line_behind_the_doctype_check():
    """The internal-subset check fires first, so the entity handlers never run
    in practice. Take the first line away and they must still hold."""
    from xml.parsers import expat

    attacks = {
        '<!ENTITY x "y">': "entity declaration 'x'",
        '<!ENTITY % p SYSTEM "http://127.0.0.1:9/p.dtd">': "entity declaration 'p'",
        '<!NOTATION n SYSTEM "n"><!ENTITY u SYSTEM "file:///etc/passwd" NDATA n>': "'u'",
    }
    for subset, expected in attacks.items():
        parser = expat.ParserCreate()
        mx._harden(parser)
        parser.StartDoctypeDeclHandler = None
        with pytest.raises(UnsafeContent) as err:
            parser.Parse(hostile(subset), True)
        assert expected in err.value.detail


# =========================================================================
# Fuzz: whatever the file says, the answer is a consistent Score or a typed error
# =========================================================================


def _random_note(rng):
    kw = {}
    if rng.random() < 0.3:
        kw["chord"] = True
    if rng.random() < 0.5:
        kw["voice"] = rng.choice([1, 2, 5, "x", 0, -1, 99999])
    if rng.random() < 0.5:
        kw["staff"] = rng.choice([1, 2, 3, 0, 70, "a"])
    if rng.random() < 0.4:
        kw["tie"] = rng.sample(["start", "stop", "continue", "let-ring"], rng.randint(1, 2))
    if rng.random() < 0.2:
        kw["alter"] = rng.choice([1, -1, "0.5", "-1.5", 9, "x", ""])
    if rng.random() < 0.3:
        kw["attrs"] = rng.choice(['dynamics="50"', 'dynamics="-5"', 'dynamics="x"', 'id="dup"', 'id="p0m0n0"'])
    if rng.random() < 0.2:
        kw["notations"] = rng.choice([
            "<arpeggiate/>", "<fermata/>", "<ornaments><trill-mark/></ornaments>",
            "<articulations><staccato/><accent/></articulations>",
        ])
    duration = rng.choice([1, 2, 3, 4, 6, 0, -1, "0.5", "x", 7, 12, 10**11])
    return note(rng.choice("CDEFGABX"), rng.choice([0, 3, 4, 5, 9, 10, 11, -1]), duration, **kw)


def _random_measure(rng, number):
    children = []
    for _ in range(rng.randint(0, 10)):
        roll = rng.random()
        if roll < 0.5:
            children.append(_random_note(rng))
        elif roll < 0.58:
            children.append(rest(rng.choice([1, 2, 4, 0])))
        elif roll < 0.66:
            children.append(backup(rng.choice([1, 2, 4, 100, 0, "x"])))
        elif roll < 0.72:
            children.append(forward(rng.choice([1, 2, 4, 0])))
        elif roll < 0.78:
            children.append(attributes(
                divisions=rng.choice([1, 2, 3, 7, 480, "0.5"]),
                fifths=rng.choice([None, 0, -3, 9]),
                time=rng.choice([None, (3, 4), (6, 8), (0, 4), (4, 3), (65, 4), ("3+2", 8)]),
                staves=rng.choice([None, 1, 2, 3]),
            ))
        elif roll < 0.84:
            children.append(dynamics(rng.choice(["p", "ff", "sfz", "fp", "n", "mf"]),
                                     staff=rng.choice([None, 1, 2])))
        elif roll < 0.90:
            children.append(pedal(rng.choice(["start", "stop", "change", "continue", "sostenuto", ""])))
        elif roll < 0.95:
            children.append(tempo(rng.choice([120, 60, 0, -1, 5, 1000, 99999, "x", "0.001"])))
        else:
            children.append(rng.choice([
                '<barline><repeat direction="backward"/></barline>',
                '<sound dacapo="yes" damper-pedal="yes"/>',
                "<attributes><transpose><chromatic>-2</chromatic>"
                "<octave-change>-1</octave-change></transpose></attributes>",
                "<direction><direction-type><metronome><beat-unit>half</beat-unit><beat-unit-dot/>"
                '<per-minute>c. 60</per-minute></metronome></direction-type>'
                '<offset sound="yes">-3</offset></direction>',
            ]))
    return measure(number, *children, implicit=rng.random() < 0.1)


def test_fuzzed_scores_give_a_consistent_score_or_a_typed_error():
    rng = random.Random(20260919)
    imported = refused = 0
    for _ in range(400):
        parts = [
            part(f"P{p}", *[_random_measure(rng, i) for i in range(rng.randint(1, 6))])
            for p in range(1, rng.randint(2, 4))
        ]
        doc = score(*parts, part_list="".join(score_part(f"P{p}", f"N{p}") for p in (1, 2, 3)))
        try:
            load(doc)  # check(): bars agree with the timeline, ids unique, seconds match beats
            imported += 1
        except ScoreImportError as exc:
            assert exc.public and exc.code
            refused += 1
    assert imported > 50 and refused > 50  # the generator exercises both outcomes


def test_mutated_bytes_give_a_score_or_a_typed_error():
    rng = random.Random(7)
    seeds = [SIMPLE, (FIXTURES / "ode_to_joy.musicxml").read_bytes(), amazing_grace(),
             make_mxl({"score.musicxml": SIMPLE})]
    for _ in range(600):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 6)):
            at = rng.randrange(len(data))
            action = rng.random()
            if action < 0.5:
                data[at] = rng.randrange(256)
            elif action < 0.75:
                del data[at : at + rng.randint(1, 40)]
            else:
                data[at:at] = data[at : at + rng.randint(1, 60)]
        try:
            load(bytes(data))
        except ScoreImportError:
            pass


# =========================================================================
# Round trip: arranger.adapters.musicxml_writer -> this reader
# =========================================================================


def _writers():
    # Imported here so a problem in the writer cannot take the reader's tests down.
    from arranger.adapters.musicxml_writer import write_musicxml, write_mxl

    return {"musicxml": write_musicxml, "mxl": write_mxl}


def round_trip_source() -> Score:
    """Pickup bar, 3/4, D major, tempo change at a barline, one triplet beat, a
    note tied across a barline, a two-note chord, both staves, a staccato, a
    dynamic, a rolled chord and one pedal span. Every beat is an exact Fraction
    so the comparison after the round trip can be exact too."""
    timeline = Timeline(
        [TempoChange(0.0, 96.0), TempoChange(4.0, 72.0)],   # bar 3 begins on beat 4
        [MeterChange(1, 3, 4)],
        [KeyChange(0.0, 2, "major")],
        pickup_beats=1.0,
    )
    third = Fraction(1, 3)

    def mk(note_id, pitch, beat, beats, staff, spelling, **extra):
        start, end = float(Fraction(beat)), float(Fraction(beat) + Fraction(beats))
        onset = timeline.seconds_at(start)
        return Note(
            pitch=pitch, onset=onset, duration=timeline.seconds_at(end) - onset, staff=staff,
            bar=timeline.bar_at(start), id=note_id, velocity=80, beat=start,
            beats=float(Fraction(beats)), track=0, spelling=spelling, **extra,
        )

    notes = [
        mk("rh-pickup", 69, 0, 1, 1, ("A", 0), dynamic="mf"),
        mk("rh-stacc", 74, 1, 1, 1, ("D", 0), articulations=("staccato",)),
        mk("trip.1", 76, 2, third, 1, ("E", 0)),
        mk("trip.2", 78, 2 + third, third, 1, ("F", 1)),
        mk("trip.3", 79, 2 + 2 * third, third, 1, ("G", 0)),
        mk("rh_asharp", 70, 3, 1, 1, ("A", 1)),          # the key would say B flat
        mk("pair-lo", 78, 4, 2, 1, ("F", 1)),
        mk("pair-hi", 81, 4, 2, 1, ("A", 0)),
        mk("roll-1", 74, 6, 1, 1, ("D", 0), rolled=True),
        mk("roll-2", 78, 6, 1, 1, ("F", 1), rolled=True),
        mk("roll-3", 81, 6, 1, 1, ("A", 0), rolled=True),
        mk("rh-last", 74, 7, 3, 1, ("D", 0)),
        mk("lh-tied", 50, 1, 6, 2, ("D", 0)),             # bars 2 and 3: tied over the barline
        mk("lh-last", 38, 7, 3, 2, ("D", 0)),
    ]
    return Score(
        notes=notes, tempo_bpm=96.0, title="Round <Trip> & back", composer="A. Tester",
        timeline=timeline, source_format="json",
        pedals=[PedalSpan(timeline.seconds_at(1.0), timeline.seconds_at(3.5))],
    )


@pytest.mark.parametrize("fmt", ["musicxml", "mxl"])
def test_writer_round_trip(fmt):
    source = round_trip_source()
    data, writer_warnings = _writers()[fmt](source)
    assert writer_warnings == []
    assert sniff_musicxml(data)
    assert (data[:2] == b"PK") == (fmt == "mxl")
    if fmt == "musicxml":  # what the test is about really is in the file
        text = data.decode()
        assert text.count('<tie type="start"') == 1 and text.count("<time-modification>") == 3
        assert 'implicit="yes"' in text and 'id="lh-tied-t1"' in text

    got = load(data)
    assert got.warnings == []

    def shape(n):
        return (n.pitch, n.beat, n.beats, n.staff)

    assert sorted(map(shape, got.notes)) == sorted(map(shape, source.notes))  # ties merged back
    assert len(got.notes) == len(source.notes) == 14

    assert got.timeline == source.timeline
    assert got.timeline.pickup_beats == 1.0
    assert got.timeline.meters == (MeterChange(1, 3, 4),)
    assert got.timeline.keys == (KeyChange(0.0, 2, "major"),)
    assert got.timeline.tempos == (TempoChange(0.0, 96.0), TempoChange(4.0, 72.0))
    assert got.tempo_bpm == 96.0

    theirs = by_id(got)
    assert set(theirs) == {n.id for n in source.notes}  # ids survive; "-t1" tie segments do not leak
    for ours in source.notes:
        back = theirs[ours.id]
        assert shape(back) == shape(ours)
        assert back.bar == ours.bar
        assert back.spelling == ours.spelling
        assert back.onset == pytest.approx(ours.onset, abs=1e-9)
        assert back.duration == pytest.approx(ours.duration, abs=1e-9)
        assert back.articulations == ours.articulations
        assert back.dynamic == ours.dynamic
        assert back.rolled == ours.rolled
    assert [theirs[i].bar for i in ("rh-pickup", "rh-stacc", "pair-lo", "rh-last")] == [1, 2, 3, 4]
    assert theirs["lh-tied"].beats == 6.0 and theirs["lh-tied"].bar == 2
    assert theirs["trip.2"].beat == float(Fraction(7, 3))
    assert theirs["rh_asharp"].spelling == ("A", 1)
    assert {n.velocity for n in got.notes} == {80}  # mf, and it reaches the other staff too

    assert len(got.pedals) == 1
    assert got.pedals[0].start == pytest.approx(source.pedals[0].start)
    assert got.pedals[0].end == pytest.approx(source.pedals[0].end)
    assert (got.title, got.composer) == ("Round <Trip> & back", "A. Tester")
    assert [(t.name, t.note_count) for t in got.tracks] == [("Piano", 14)]


def test_writer_round_trip_is_stable_in_the_second_generation():
    write = _writers()["musicxml"]
    first = load(write(round_trip_source())[0])
    second = load(write(first)[0])
    assert second.notes == first.notes
    assert second.timeline == first.timeline
    assert second.pedals == first.pedals


def test_writer_round_trip_of_an_imported_file_keeps_generated_ids():
    original = read_musicxml(FIXTURES / "ode_to_joy.musicxml")
    data, _ = _writers()["mxl"](original)
    back = load(data)
    assert {n.id for n in back.notes} == {n.id for n in original.notes}
    assert sorted((n.pitch, n.beat, n.beats, n.staff, n.id) for n in back.notes) == sorted(
        (n.pitch, n.beat, n.beats, n.staff, n.id) for n in original.notes
    )
    assert back.timeline == original.timeline


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
