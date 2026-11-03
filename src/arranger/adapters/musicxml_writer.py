"""Write a Score as MusicXML 4.0 (score-partwise), one piano part on two staves.

All layout decisions were made in `arranger.notation`; this file only spells
them in XML. Text is escaped by ElementTree, never concatenated, so a title
like `<script>` is just an odd title.

Stdlib only.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction

from ..ir import Score
from ..notation import NEvent, NMeasure, NotatedScore, notate

DIVISIONS = 480  # per quarter note: divisible by every grid the quantiser uses

_DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
    '"http://www.musicxml.org/dtds/partwise.dtd">\n'
)
_ARTICULATIONS = {
    "staccato": "staccato", "staccatissimo": "staccatissimo", "accent": "accent",
    "marcato": "strong-accent", "tenuto": "tenuto",
}
_DYNAMICS = {"ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "fp"}


def _ticks(value: Fraction) -> int:
    return int(round(value * DIVISIONS))


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def _xml_id(value: str, used: set[str]) -> str:
    """Make a note id a valid, unique XML ID.

    Valid: letters, digits, `-`, `_`, `.`, starting with a letter. Unique:
    cleaning can map two different ids ("a b" and "a_b") onto one, and a
    document with duplicate ids is not valid XML, so a clash gets a suffix.
    """
    cleaned = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in "-_." else "_" for ch in value)
    if not cleaned[:1].isalpha():
        cleaned = f"n{cleaned}"
    candidate, serial = cleaned, 1
    while candidate in used:
        serial += 1
        candidate = f"{cleaned}.{serial}"
    used.add(candidate)
    return candidate


def _attributes(measure_el: ET.Element, m: NMeasure, first: bool) -> None:
    if not (first or m.show_time or m.show_key):
        return
    attrs = _sub(measure_el, "attributes")
    if first:
        _sub(attrs, "divisions", str(DIVISIONS))
    if first or m.show_key:
        key = _sub(attrs, "key")
        _sub(key, "fifths", str(m.fifths))
        _sub(key, "mode", m.mode)
    if first or m.show_time:
        time = _sub(attrs, "time")
        _sub(time, "beats", str(m.numerator))
        _sub(time, "beat-type", str(m.denominator))
    if first:
        _sub(attrs, "staves", "2")
        for number, sign, line in (("1", "G", "2"), ("2", "F", "4")):
            clef = _sub(attrs, "clef", number=number)
            _sub(clef, "sign", sign)
            _sub(clef, "line", line)


def _tempo(measure_el: ET.Element, bpm: float, offset: Fraction, marked: bool) -> None:
    """A tempo change at `offset` into the bar. Unmarked ones are playback-only.

    The tempo is written with full precision. Rounding it to two decimals, as
    an earlier version did, made every later note drift after a round trip.
    """
    direction = _sub(measure_el, "direction", placement="above")
    kind = _sub(direction, "direction-type")
    if marked:
        metronome = _sub(kind, "metronome")
        _sub(metronome, "beat-unit", "quarter")
        _sub(metronome, "per-minute", str(round(bpm)))
    else:
        _sub(kind, "words", "")
    if offset > 0:
        _sub(direction, "offset", str(_ticks(offset)), sound="yes")
    _sub(direction, "staff", "1")
    _sub(direction, "sound", tempo=repr(float(bpm)))


def _dynamic(measure_el: ET.Element, mark: str, staff: int) -> None:
    if mark not in _DYNAMICS:
        return
    direction = _sub(measure_el, "direction", placement="below")
    _sub(_sub(_sub(direction, "direction-type"), "dynamics"), mark)
    _sub(direction, "staff", str(staff))


def _pedal(measure_el: ET.Element, kind: str, offset: Fraction) -> None:
    direction = _sub(measure_el, "direction", placement="below")
    _sub(_sub(direction, "direction-type"), "pedal", type=kind, line="yes")
    if offset > 0:
        _sub(direction, "offset", str(_ticks(offset)))
    _sub(direction, "staff", "2")


def _event(
    measure_el: ET.Element, ev: NEvent, voice: int, staff: int, m: NMeasure, used_ids: set[str]
) -> None:
    if ev.hidden:
        forward = _sub(measure_el, "forward")
        _sub(forward, "duration", str(_ticks(ev.duration)))
        _sub(forward, "voice", str(voice))
        _sub(forward, "staff", str(staff))
        return

    if ev.dynamic:
        _dynamic(measure_el, ev.dynamic, staff)

    targets = ev.pitches or [None]
    for index, pitch in enumerate(targets):
        attrs = {}
        if pitch is not None and pitch.note_id:
            attrs["id"] = _xml_id(pitch.note_id, used_ids)
        note = _sub(measure_el, "note", **attrs)
        if pitch is None:
            rest = _sub(note, "rest", **({"measure": "yes"} if ev.whole_bar_rest else {}))
            del rest
        else:
            if index > 0:
                _sub(note, "chord")
            p = _sub(note, "pitch")
            _sub(p, "step", pitch.step)
            if pitch.alter:
                _sub(p, "alter", str(pitch.alter))
            _sub(p, "octave", str(pitch.octave))
        _sub(note, "duration", str(_ticks(ev.duration)))
        if pitch is not None:
            if pitch.tie_stop:
                _sub(note, "tie", type="stop")
            if pitch.tie_start:
                _sub(note, "tie", type="start")
        _sub(note, "voice", str(voice))
        if not ev.whole_bar_rest:
            _sub(note, "type", ev.note_type)
            for _ in range(ev.dots):
                _sub(note, "dot")
        if ev.tuplet:
            mod = _sub(note, "time-modification")
            _sub(mod, "actual-notes", str(ev.tuplet[0]))
            _sub(mod, "normal-notes", str(ev.tuplet[1]))
        _sub(note, "staff", str(staff))

        notations: list[ET.Element] = []
        if pitch is not None and pitch.tie_stop:
            notations.append(ET.Element("tied", type="stop"))
        if pitch is not None and pitch.tie_start:
            notations.append(ET.Element("tied", type="start"))
        if index == 0 and ev.tuplet_start:
            notations.append(ET.Element("tuplet", type="start", bracket="yes"))
        if index == 0 and ev.tuplet_stop:
            notations.append(ET.Element("tuplet", type="stop"))
        if pitch is not None and index == 0:
            marks = [_ARTICULATIONS[a] for a in ev.articulations if a in _ARTICULATIONS]
            if marks:
                art = ET.Element("articulations")
                for mark in marks:
                    ET.SubElement(art, mark)
                notations.append(art)
            if "fermata" in ev.articulations:
                notations.append(ET.Element("fermata"))
        if pitch is not None and ev.rolled:
            notations.append(ET.Element("arpeggiate"))
        if notations:
            holder = _sub(note, "notations")
            holder.extend(notations)


def notated_to_musicxml(notated: NotatedScore) -> bytes:
    root = ET.Element("score-partwise", version="4.0")
    work = _sub(root, "work")
    _sub(work, "work-title", notated.title or "untitled")
    identification = _sub(root, "identification")
    if notated.composer:
        _sub(identification, "creator", notated.composer, type="composer")
    encoding = _sub(identification, "encoding")
    _sub(encoding, "software", "Arranger")
    part_list = _sub(root, "part-list")
    score_part = _sub(part_list, "score-part", id="P1")
    _sub(score_part, "part-name", "Piano")
    part = _sub(root, "part", id="P1")
    used_ids: set[str] = {"P1"}

    for index, m in enumerate(notated.measures):
        attrs = {"number": str(m.number)}
        if m.is_pickup:
            attrs["implicit"] = "yes"
        measure_el = _sub(part, "measure", **attrs)
        _attributes(measure_el, m, first=index == 0)
        for offset, bpm, marked in sorted(m.tempo_changes, key=lambda c: c[0]):
            _tempo(measure_el, bpm, offset, marked)
        for offset in sorted(m.pedal_stops):
            _pedal(measure_el, "stop", offset)
        for offset in sorted(m.pedal_starts):
            _pedal(measure_el, "start", offset)

        first_voice = True
        for staff in (1, 2):
            for voice in m.staves.get(staff, []):
                if not first_voice:
                    backup = _sub(measure_el, "backup")
                    _sub(backup, "duration", str(_ticks(m.length)))
                first_voice = False
                for ev in voice.events:
                    _event(measure_el, ev, voice.number, staff, m, used_ids)

    ET.indent(root, space="  ")
    return _DOCTYPE.encode() + ET.tostring(root, encoding="utf-8")


def write_musicxml(score: Score) -> tuple[bytes, list[str]]:
    """(uncompressed MusicXML bytes, conversion warnings)."""
    notated = notate(score)
    return notated_to_musicxml(notated), notated.warnings


def write_mxl(score: Score, *, filename: str = "score.musicxml") -> tuple[bytes, list[str]]:
    """Compressed MusicXML (.mxl): a zip with a container manifest."""
    xml, warnings = write_musicxml(score)
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<container><rootfiles>'
        f'<rootfile full-path="{filename}" media-type="application/vnd.recordare.musicxml+xml"/>'
        "</rootfiles></container>\n"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr(filename, xml)
    return buffer.getvalue(), warnings
