"""MusicXML importer: ``.musicxml`` / ``.xml`` / compressed ``.mxl`` -> ``Score``.

Standard library only. Every file this module sees comes from a stranger, so
it is written as two layers that do not trust each other:

**The container layer** turns bytes into one XML element tree, or refuses.

* XML is parsed by driving ``xml.parsers.expat`` directly into an
  ``ElementTree.TreeBuilder``. (CPython's C ``XMLParser`` does not expose its
  expat parser, so the defusedxml trick of setting handlers on
  ``XMLParser.parser`` is not available without swapping ``sys.modules``
  around; owning the expat parser gives the same handlers without the hack.)
  A DOCTYPE with an internal subset, any entity declaration, any unparsed
  entity, any external entity reference and any reference to an entity only
  an unfetched DTD could define all raise ``UnsafeContent``. The ordinary
  ``<!DOCTYPE score-partwise PUBLIC ...>`` line is accepted and its DTD is
  never fetched. Element count and nesting depth are enforced while the
  document streams in, 64 KiB at a time, so a hostile file is dropped at the
  first offending element rather than after it is all in memory.
* ``.mxl`` archives are read in memory, never extracted. Entry names are
  lookup keys and nothing else. Declared sizes are checked first, then
  enforced again while inflating in chunks, because headers lie.

**The music layer** walks the tree and produces notes on two clocks: exact
quarter-note beats (``fractions.Fraction`` until the last moment, so triplets
land on exact thirds) and seconds through the ``Timeline`` tempo map.

Conventions worth knowing before reading the code:

* Note ids. A ``<note id="...">`` attribute (MusicXML 4.0) is kept when it is
  a plain XML name of sane length that no earlier note has used, so ids
  written by ``musicxml_writer`` survive a round trip. Otherwise the id is
  ``p{part}m{measure}n{k}``, all 0-based: ``part`` equals ``Note.track``;
  ``measure`` is the measure's ordinal position in the part (its ``number``
  attribute can repeat or be non-numeric); ``k`` counts the pitched,
  non-grace, non-cue notes of that part and measure in document order. A
  merged tie keeps the id of its first note. Ids are unique within a score.
* A measure is as long as the furthest the cursor got in any part, not as
  long as the time signature claims. When a measure that is not the pickup
  disagrees with its time signature, the timeline gets a meter change for
  exactly that bar (3 beats in 4/4 becomes one bar of 3/4), so bar numbers
  keep matching the source's measures and ``Note.bar`` always equals
  ``timeline.bar_at(note.beat)``. This is reported as a warning.
* ``TrackInfo.channel`` and ``TrackInfo.program`` are 0-based, like the MIDI
  importer's. MusicXML writes both 1-based.
* Anything that changes what sounds, and that this importer does not act on
  (repeats, grace notes, ornaments, cue notes, ...), produces a warning.
  Nothing that affects pitch or time is dropped silently.
"""

from __future__ import annotations

import io
import math
import re
import struct
import unicodedata
import zipfile
import zlib
from bisect import bisect_right
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from xml.parsers import expat

from ..ir import Note, PedalSpan, Score, TrackInfo
from ..limits import (
    DEFAULT_LIMITS,
    EmptyScore,
    ImportLimits,
    LimitExceeded,
    MalformedFile,
    ScoreImportError,
    UnsafeContent,
    UnsupportedFormat,
)
from ..timeline import (
    MAX_BPM,
    MAX_TEMPO_CHANGES,
    MIN_BPM,
    KeyChange,
    MeterChange,
    TempoChange,
    Timeline,
    TimelineError,
)

__all__ = ["read_musicxml", "read_musicxml_bytes", "sniff_musicxml"]

_SCORE_ROOTS = frozenset({"score-partwise", "score-timewise"})
_CONTAINER_ROOTS = frozenset({"container"})
_CONTAINER_PATH = "META-INF/container.xml"
_MUSICXML_MEDIA_TYPES = frozenset(
    {"", "application/vnd.recordare.musicxml+xml", "application/vnd.recordare.musicxml"}
)
_FEED_CHUNK = 1 << 16
_READ_CHUNK = 1 << 16
_SNIFF_BYTES = 1 << 18

_ARCHIVE_SUFFIXES = (
    ".zip", ".mxl", ".jar", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".zst",
)
_ARCHIVE_MAGIC = (
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07", b"BZh", b"\xfd7zXZ\x00",
)
_ZIP_ERRORS = (
    zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, struct.error,
    EOFError, OSError, ValueError, NotImplementedError, RuntimeError, OverflowError,
)

# A position whose denominator outgrows this is not music, it is an attack on
# Fraction arithmetic (thousands of mutually prime <divisions> values). The
# least common multiple of every subdivision a real score uses is far smaller.
_MAX_DENOMINATOR = 10**15

_STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

_BEAT_UNITS = {
    "maxima": Fraction(32), "long": Fraction(16), "breve": Fraction(8), "whole": Fraction(4),
    "half": Fraction(2), "quarter": Fraction(1), "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4), "32nd": Fraction(1, 8), "64th": Fraction(1, 16),
    "128th": Fraction(1, 32), "256th": Fraction(1, 64),
}

DEFAULT_VELOCITY = 80
# Steady levels: the six the contract names, plus the outer ones on the same scale.
_DYNAMIC_LEVELS = {
    "pppppp": 4, "ppppp": 6, "pppp": 10, "ppp": 16, "pp": 33, "p": 49, "mp": 64,
    "mf": 80, "f": 96, "ff": 112, "fff": 126, "ffff": 127, "fffff": 127, "ffffff": 127,
}
# One-onset accents: floor velocity at the marked onset. The prevailing level is
# untouched - a sforzando in a pianissimo passage does not make the rest loud.
_DYNAMIC_ACCENTS = {"sf": 96, "sfz": 96, "fz": 96, "rf": 96, "rfz": 96, "sffz": 112}
# Loud attack, then a new quiet level: (velocity at the marked onset, level after).
_DYNAMIC_ATTACKS = {"fp": (96, "p"), "sfp": (112, "p"), "sfzp": (112, "p"), "sfpp": (112, "pp")}

_ARTICULATIONS = {
    "staccato": "staccato",
    "staccatissimo": "staccatissimo",
    "accent": "accent",
    "strong-accent": "marcato",
    "tenuto": "tenuto",
}
_ORNAMENTS = frozenset({
    "trill-mark", "turn", "delayed-turn", "inverted-turn", "delayed-inverted-turn",
    "vertical-turn", "inverted-vertical-turn", "shake", "wavy-line", "mordent",
    "inverted-mordent", "schleifer", "tremolo", "haydn",
})
_JUMP_SOUND_ATTRS = ("segno", "coda", "dacapo", "dalsegno", "tocoda", "fine", "forward-repeat")
_JUMP_LABELS = {
    "repeat": "repeat barlines", "ending": "volta endings", "segno": "segno",
    "coda": "coda", "dacapo": "D.C.", "dalsegno": "D.S.", "tocoda": "to coda",
    "fine": "fine", "forward-repeat": "repeat barlines",
}

_NUMBER_RE = re.compile(r"[+-]?(?:\d{1,12}(?:\.\d{0,12})?|\.\d{1,12})\Z")
_PER_MINUTE_RE = re.compile(r"\d{1,4}(?:\.\d{1,4})?")
_MAX_ID_CHARS = 128
_BIDI_CONTROLS = frozenset(
    [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)


# =========================================================================
# Public API
# =========================================================================


def read_musicxml(path: str | Path, *, limits: ImportLimits = DEFAULT_LIMITS) -> Score:
    """Read a ``.musicxml`` / ``.xml`` / ``.mxl`` file from disk."""
    path = Path(path)
    size = path.stat().st_size
    if size > limits.max_bytes:
        raise LimitExceeded(
            "This file is larger than the upload limit.",
            detail=f"{size} > max_bytes={limits.max_bytes}",
        )
    with path.open("rb") as fh:
        data = fh.read(limits.max_bytes + 1)  # the file may have grown since stat()
    return read_musicxml_bytes(data, filename=path.name, limits=limits)


def read_musicxml_bytes(
    data: bytes, *, filename: str | None = None, limits: ImportLimits = DEFAULT_LIMITS
) -> Score:
    """Parse MusicXML held in memory. Compressed or not is decided by content."""
    if len(data) > limits.max_bytes:
        raise LimitExceeded(
            "This file is larger than the upload limit.",
            detail=f"{len(data)} > max_bytes={limits.max_bytes}",
        )
    if not isinstance(data, bytes):
        data = bytes(data)
    if not data.strip():
        raise MalformedFile("This file is empty.", detail="no bytes")
    document = _rootfile_from_archive(data, limits) if data[:2] == b"PK" else data
    root = _parse_xml(document, limits, _SCORE_ROOTS, "score")
    return _build_score(root, filename, limits)


def sniff_musicxml(data: bytes) -> bool:
    """Cheap check. True for a zip holding ``META-INF/container.xml``, or XML
    whose root is ``score-partwise`` / ``score-timewise``. Never raises, never
    expands an entity, reads at most the first 256 KiB of an XML document."""
    try:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
                return _CONTAINER_PATH in zf.namelist()
        head = bytes(data[:_SNIFF_BYTES])
        if b"<" not in head:
            return False
        return _sniff_root(head) in _SCORE_ROOTS
    except Exception:  # a sniffer answers yes or no, nothing else
        return False


# =========================================================================
# Container layer: safe XML
# =========================================================================


class _Found(Exception):
    """Internal: the sniffer has its answer."""


def _local(tag: str) -> str:
    return tag.rpartition("}")[2] if "}" in tag else tag


def _sniff_root(head: bytes) -> str | None:
    found: list[str] = []

    def on_doctype(name, _sysid, _pubid, has_internal_subset):
        # Do not parse an internal subset even to sniff. The declared name is
        # enough to route the file to the importer, which will refuse it.
        if has_internal_subset:
            found.append(_local(name))
            raise _Found

    def on_start(name, _attrs):
        found.append(_local(name))
        raise _Found

    parser = expat.ParserCreate(None, "}")
    parser.StartDoctypeDeclHandler = on_doctype
    parser.StartElementHandler = on_start
    try:
        parser.Parse(head, False)
    except _Found:
        pass
    except expat.ExpatError:
        return None
    return found[0] if found else None


def _harden(parser) -> None:
    """Refuse everything in XML that a music file has no business doing."""

    def on_doctype(name, sysid, pubid, has_internal_subset):
        if has_internal_subset:
            raise UnsafeContent(
                "This file declares its own document type rules, which a MusicXML "
                "file never needs. It was not opened.",
                detail=f"DOCTYPE {name!r} has an internal subset (system={sysid!r}, "
                f"public={pubid!r})",
            )

    def on_entity(name, is_parameter, value, base, sysid, pubid, notation):
        raise UnsafeContent(
            "This file defines XML entities, which a MusicXML file never needs. "
            "It was not opened.",
            detail=f"entity declaration {name!r} (parameter={bool(is_parameter)}, "
            f"system={sysid!r}, public={pubid!r})",
        )

    def on_unparsed_entity(name, base, sysid, pubid, notation):
        raise UnsafeContent(
            "This file defines XML entities, which a MusicXML file never needs. "
            "It was not opened.",
            detail=f"unparsed entity declaration {name!r} (system={sysid!r})",
        )

    def on_external_ref(context, base, sysid, pubid):
        raise UnsafeContent(
            "This file tries to pull in outside content. It was not opened.",
            detail=f"external entity reference (system={sysid!r}, public={pubid!r})",
        )

    def on_skipped_entity(name, is_parameter):
        # Only reachable when a DOCTYPE names an external DTD (which is never
        # fetched) and the body then uses an entity only that DTD could define.
        raise UnsafeContent(
            "This file relies on outside content. It was not opened.",
            detail=f"reference to entity {name!r}, which only an external DTD could define",
        )

    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = on_doctype
    parser.EntityDeclHandler = on_entity
    parser.UnparsedEntityDeclHandler = on_unparsed_entity
    parser.ExternalEntityRefHandler = on_external_ref
    parser.SkippedEntityHandler = on_skipped_entity


def _parse_xml(
    data: bytes, limits: ImportLimits, roots: frozenset[str], what: str
) -> ET.Element:
    """bytes -> element tree, with limits enforced while the bytes stream in."""
    builder = ET.TreeBuilder()
    depth = 0
    count = 0

    def on_start(tag, attrs):
        nonlocal depth, count
        tag = _local(tag)
        depth += 1
        count += 1
        if depth == 1 and tag not in roots:
            raise UnsupportedFormat(
                "This is an XML file, but not a MusicXML score.",
                detail=f"{what}: root element is {tag[:80]!r}",
            )
        if count > limits.max_events:
            raise LimitExceeded(
                "This file has more content than this service will process.",
                detail=f"{what}: more than max_events={limits.max_events} XML elements",
            )
        if depth > limits.max_xml_depth:
            raise LimitExceeded(
                "This file is nested more deeply than any real score.",
                detail=f"{what}: XML depth exceeds max_xml_depth={limits.max_xml_depth}",
            )
        builder.start(tag, attrs)

    def on_end(tag):
        nonlocal depth
        depth -= 1
        builder.end(_local(tag))

    parser = expat.ParserCreate(None, "}")
    parser.buffer_text = True
    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    parser.CharacterDataHandler = builder.data
    _harden(parser)

    view = memoryview(data)
    try:
        for start in range(0, len(view), _FEED_CHUNK):
            parser.Parse(view[start : start + _FEED_CHUNK], False)
        parser.Parse(b"", True)
        root = builder.close()
    except ScoreImportError:
        raise
    except expat.ExpatError as exc:
        raise MalformedFile(
            "This file is damaged or is not valid XML.", detail=f"{what}: {exc}"
        ) from exc
    except (ValueError, LookupError, AssertionError) as exc:  # odd encodings, builder state
        raise MalformedFile(
            "This file is damaged or is not valid XML.", detail=f"{what}: {exc!r}"
        ) from exc
    if root is None:
        raise MalformedFile("This file is damaged or is not valid XML.", detail=f"{what}: no root")
    return root


# =========================================================================
# Container layer: .mxl
# =========================================================================


def _unsafe_archive_path(name: str) -> bool:
    """True for a path that points outside the archive, or is not a path at all."""
    if not name or "\x00" in name:
        return True
    unified = name.replace("\\", "/")
    if unified.startswith("/") or re.match(r"[A-Za-z]:", unified):
        return True
    return ".." in PurePosixPath(unified).parts


def _check_archive_headers(infos: list[zipfile.ZipInfo], limits: ImportLimits) -> None:
    """Everything that can be refused before a single byte is inflated."""
    if len(infos) > limits.max_archive_entries:
        raise LimitExceeded(
            "This compressed score contains too many files.",
            detail=f"{len(infos)} entries > max_archive_entries={limits.max_archive_entries}",
        )
    declared_total = 0
    for info in infos:
        name = info.filename[:120]
        if info.flag_bits & 0x41:  # bit 0: encrypted, bit 6: strong encryption
            raise UnsafeContent(
                "This compressed score is password-protected, which is not supported.",
                detail=f"entry {name!r} is encrypted (flags={info.flag_bits:#06x})",
            )
        if info.filename.lower().endswith(_ARCHIVE_SUFFIXES):
            raise UnsafeContent(
                "This compressed score contains another archive, which is not allowed.",
                detail=f"nested archive entry {name!r}",
            )
        if info.file_size > limits.max_uncompressed_bytes:
            raise LimitExceeded(
                "This compressed score is too large when unpacked.",
                detail=f"entry {name!r} declares {info.file_size} bytes "
                f"> max_uncompressed_bytes={limits.max_uncompressed_bytes}",
            )
        declared_total += info.file_size
        if declared_total > limits.max_uncompressed_bytes:
            raise LimitExceeded(
                "This compressed score is too large when unpacked.",
                detail="entries declare more than "
                f"max_uncompressed_bytes={limits.max_uncompressed_bytes} in total",
            )
        if info.file_size > max(info.compress_size, 1) * limits.max_compression_ratio:
            raise LimitExceeded(
                "This compressed score unpacks to suspiciously more than it holds.",
                detail=f"entry {name!r} declares ratio {info.file_size}/{info.compress_size} "
                f"> max_compression_ratio={limits.max_compression_ratio}",
            )


def _read_entry(archive, info: zipfile.ZipInfo, limits: ImportLimits, spent: int) -> bytes:
    """Inflate one entry in chunks, re-checking every promise the header made.

    `spent` is how many bytes earlier entries already cost: the cap is on the
    archive, not the entry. `archive` only needs an ``open(info)`` method.
    """
    name = info.filename[:120]
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise UnsupportedFormat(
            "This compressed score uses a compression method that is not supported.",
            detail=f"entry {name!r} compress_type={info.compress_type}",
        )
    ratio_cap = max(info.compress_size, 1) * limits.max_compression_ratio
    chunks: list[bytes] = []
    total = 0
    with archive.open(info) as fh:
        while True:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > info.file_size:
                raise UnsafeContent(
                    "This compressed score lies about its own size. It was not opened.",
                    detail=f"entry {name!r} inflated past its declared {info.file_size} bytes",
                )
            if spent + total > limits.max_uncompressed_bytes:
                raise LimitExceeded(
                    "This compressed score is too large when unpacked.",
                    detail="inflated more than "
                    f"max_uncompressed_bytes={limits.max_uncompressed_bytes}",
                )
            if total > ratio_cap:
                raise LimitExceeded(
                    "This compressed score unpacks to suspiciously more than it holds.",
                    detail=f"entry {name!r} inflated past "
                    f"max_compression_ratio={limits.max_compression_ratio}",
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _refuse_hidden_archives(archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> None:
    """Look at the first bytes of every entry for an archive behind an innocent
    name. A few bytes each; nothing is inflated in full."""
    for info in infos:
        if info.is_dir() or info.file_size == 0:
            continue
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            continue  # never read here; refused by _read_entry if it is the score
        with archive.open(info) as fh:
            if fh.read(8).startswith(_ARCHIVE_MAGIC):
                raise UnsafeContent(
                    "This compressed score contains another archive, which is not allowed.",
                    detail=f"entry {info.filename[:120]!r} starts with archive magic",
                )


def _rootfile_from_archive(data: bytes, limits: ImportLimits) -> bytes:
    """The bytes of the score inside an .mxl. Nothing touches the disk."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            _check_archive_headers(infos, limits)
            _refuse_hidden_archives(archive, infos)
            by_name = {info.filename: info for info in infos}  # lookup keys, never paths

            spent = 0
            rootfile: str | None = None
            container = by_name.get(_CONTAINER_PATH)
            if container is not None:
                raw = _read_entry(archive, container, limits, spent)
                spent += len(raw)
                rootfile = _rootfile_path(
                    _parse_xml(raw, limits, _CONTAINER_ROOTS, "container.xml")
                )
                if rootfile not in by_name:
                    raise MalformedFile(
                        "This compressed score is incomplete.",
                        detail=f"container.xml names {rootfile[:120]!r}, which is not "
                        "in the archive",
                    )
            else:
                for info in infos:
                    lowered = info.filename.lower()
                    if (
                        lowered.endswith((".xml", ".musicxml"))
                        and not lowered.startswith("meta-inf/")
                        and not info.is_dir()
                        and not _unsafe_archive_path(info.filename)
                    ):
                        rootfile = info.filename
                        break
                if rootfile is None:
                    raise UnsupportedFormat(
                        "This compressed file does not contain a MusicXML score.",
                        detail="no META-INF/container.xml and no .xml/.musicxml entry",
                    )
            return _read_entry(archive, by_name[rootfile], limits, spent)
    except ScoreImportError:
        raise
    except _ZIP_ERRORS as exc:
        raise MalformedFile(
            "This compressed score is damaged and could not be opened.",
            detail=f"zip: {exc!r}",
        ) from exc


def _rootfile_path(container: ET.Element) -> str:
    chosen = next(
        (
            candidate
            for candidate in container.findall("rootfiles/rootfile")
            if (candidate.get("media-type") or "").strip().lower() in _MUSICXML_MEDIA_TYPES
        ),
        None,
    )
    if chosen is None:
        raise MalformedFile(
            "This compressed score does not say where its music is.",
            detail="container.xml has no usable <rootfile>",
        )
    path = chosen.get("full-path") or ""
    if _unsafe_archive_path(path):
        raise UnsafeContent(
            "This compressed score points outside itself. It was not opened.",
            detail=f"container.xml rootfile full-path={path[:120]!r}",
        )
    return path


# =========================================================================
# Music layer: small parsing helpers
# =========================================================================


def _number(text: str | None) -> Fraction | None:
    """Exact decimal, or None. Bounded on purpose: ``Fraction('1e999999999')``
    would happily try to build a billion-digit integer."""
    if text is None:
        return None
    text = text.strip()
    if len(text) > 26:  # longer than the pattern can match; also keeps the cache small
        return None
    return _parse_number(text)


@lru_cache(maxsize=4096)  # a score says "480" tens of thousands of times
def _parse_number(text: str) -> Fraction | None:
    return Fraction(text) if _NUMBER_RE.match(text) else None


def _integer(text: str | None) -> int | None:
    value = _number(text)
    if value is None or value.denominator != 1:
        return None
    return int(value)


def _round_half_up(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


def _clean_text(text: str | None, max_chars: int) -> str:
    """User-visible text from a stranger: no control or bidi-override
    characters, single spaces, bounded length."""
    if not text:
        return ""
    text = text[: max_chars * 4 + 64]  # bound the work before doing it
    kept: list[str] = []
    for ch in text:
        if ch in "\t\n\r":
            kept.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn") or ord(ch) in _BIDI_CONTROLS:
            continue
        else:
            kept.append(ch)
    return " ".join("".join(kept).split())[:max_chars].rstrip()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _were(count: int, noun: str) -> str:
    return f"{count} {noun} was" if count == 1 else f"{count} {noun}s were"


def _bars(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:8])
    return f"{shown}, ..." if len(numbers) > 8 else shown


def _key_shift(semitones: int) -> int:
    """How many fifths a key signature moves when the music moves `semitones`."""
    delta = (semitones * 7) % 12
    return delta - 12 if delta > 6 else delta


def _bar_beats(meter: tuple[int, int]) -> Fraction:
    return Fraction(meter[0] * 4, meter[1])


def _meter_for_length(length: Fraction, prefer_denominator: int) -> tuple[int, int] | None:
    """A time signature whose bar is exactly `length` quarter-note beats."""
    for denominator in (prefer_denominator, 4, 8, 16, 32, 64, 2, 1):
        numerator = length * denominator / 4
        if numerator.denominator == 1 and 1 <= numerator <= 64:
            return int(numerator), denominator
    return None


# =========================================================================
# Music layer: data gathered while walking
# =========================================================================


@dataclass(slots=True)
class _RawNote:
    measure: int
    offset: Fraction          # beats from the start of its measure
    length: Fraction          # beats
    pitch: int                # sounding
    spelling: tuple[str, int] | None
    staff: int                # as written; 1 when the file does not say
    voice: int
    id: str
    order: int                # document order within the part
    tie_start: bool
    tie_stop: bool
    articulations: tuple[str, ...]
    rolled: bool
    own_velocity: int | None
    start: Fraction = Fraction(0)   # absolute beats; known once every part is walked
    velocity: int = DEFAULT_VELOCITY
    dynamic: str | None = None


@dataclass(slots=True)
class _Marking:
    """A dynamic marking: where, which staff (None = the whole part), what."""

    measure: int
    offset: Fraction
    staff: int | None
    text: str
    order: int
    start: Fraction = Fraction(0)


@dataclass
class _PartMeta:
    name: str = ""
    program: int | None = None
    channel: int | None = None


@dataclass
class _Tally:
    """Everything the importer noticed and could not represent. Becomes warnings."""

    grace: int = 0
    cue: int = 0
    percussion: int = 0
    microtonal: int = 0
    bad_pitch: int = 0
    no_duration: int = 0
    backup_underflow: int = 0
    ornaments: int = 0
    harmonics: int = 0
    orphan_tie_stops: int = 0
    unterminated_ties: int = 0
    bad_tempo: int = 0
    clamped_tempo: int = 0
    metric_modulation: int = 0
    bad_time: int = 0
    senza_misura: bool = False
    odd_key: bool = False
    modal_key: bool = False
    doubled_transposition: bool = False
    measure_repeat: bool = False
    sostenuto: bool = False
    missing_divisions: bool = False
    jumps: set[str] = field(default_factory=set)
    respelled_parts: list[str] = field(default_factory=list)
    extra_staff_parts: list[str] = field(default_factory=list)


@dataclass
class _PartData:
    index: int
    meta: _PartMeta
    notes: list[_RawNote] = field(default_factory=list)
    measure_lengths: list[Fraction] = field(default_factory=list)
    meters: dict[int, tuple[int, int]] = field(default_factory=dict)
    # (measure, offset, ...) until every part is walked and measures have starts
    keys: list[tuple[int, Fraction, int, str]] = field(default_factory=list)
    tempos: list[tuple[int, Fraction, int, float]] = field(default_factory=list)
    pedals: list[tuple[int, Fraction, int, str]] = field(default_factory=list)
    markings: list[_Marking] = field(default_factory=list)
    max_staff: int = 1
    uses_unpitched: bool = False

    @property
    def label(self) -> str:
        return self.meta.name or f"part {self.index + 1}"


# =========================================================================
# Music layer: walking one part
# =========================================================================


class _PartWalker:
    """Turns one part's measures into raw, measure-relative events.

    Absolute positions are not known here: a measure is as long as its
    longest part, and the other parts have not been walked yet.
    """

    def __init__(
        self,
        part: _PartData,
        tally: _Tally,
        limits: ImportLimits,
        budget: list[int],
        used_ids: set[str],
    ):
        self.part = part
        self.tally = tally
        self.limits = limits
        self.budget = budget  # [notes still allowed], shared across parts
        self.used_ids = used_ids  # shared across parts: ids are unique per score
        self.divisions = Fraction(1)
        self.divisions_known = False
        self.transpose: dict[int | None, tuple[int, bool]] = {}
        self.voices: dict[str, int] = {}
        self.order = 0
        self.percussion_channel = part.meta.channel == 9
        # per measure
        self.measure = 0
        self.cursor = Fraction(0)
        self.reached = Fraction(0)
        self.chord_start = Fraction(0)
        self.chord_articulations: tuple[str, ...] = ()
        self.k = 0

    # --- driving ---------------------------------------------------------

    def walk(self, measures: list[ET.Element]) -> None:
        for index, measure in enumerate(measures):
            self.measure = index
            self.cursor = self.reached = self.chord_start = Fraction(0)
            self.k = 0
            for element in measure:
                handler = self._HANDLERS.get(element.tag)
                if handler is not None:
                    handler(self, element)
            self.part.measure_lengths.append(self.reached)

    def _next_order(self) -> int:
        self.order += 1
        return self.order

    def _beats(self, element: ET.Element | None) -> Fraction | None:
        """A <duration>/<offset> in divisions -> quarter-note beats."""
        value = _number(element.text if element is not None else None)
        return None if value is None else value / self.divisions

    def _move(self, to: Fraction) -> None:
        if to.denominator > _MAX_DENOMINATOR:
            raise MalformedFile(
                "This file's timing is not usable.",
                detail=f"part {self.part.index} measure {self.measure}: position "
                "denominator exceeds the sane bound",
            )
        self.cursor = to
        if to > self.reached:
            self.reached = to

    # --- time ------------------------------------------------------------

    def _backup(self, element: ET.Element) -> None:
        length = self._beats(element.find("duration"))
        if length is None or length < 0:
            self.tally.no_duration += 1
            return
        target = self.cursor - length
        if target < 0:
            self.tally.backup_underflow += 1
            target = Fraction(0)
        self._move(target)

    def _forward(self, element: ET.Element) -> None:
        length = self._beats(element.find("duration"))
        if length is None or length < 0:
            self.tally.no_duration += 1
            return
        self._move(self.cursor + length)

    # --- attributes ------------------------------------------------------

    def _attributes(self, element: ET.Element) -> None:
        divisions = element.find("divisions")
        if divisions is not None:
            value = _number(divisions.text)
            if value is None or value <= 0:
                raise MalformedFile(
                    "This file's timing is not usable.",
                    detail=f"part {self.part.index} measure {self.measure}: "
                    f"<divisions>{(divisions.text or '')[:40]!r}",
                )
            self.divisions = value
            self.divisions_known = True

        staves = _integer(element.findtext("staves"))
        if staves is not None and 1 <= staves <= 64:
            self.part.max_staff = max(self.part.max_staff, staves)

        # <transpose> follows <key> in the file, but the key needs it.
        for transpose in element.findall("transpose"):
            chromatic = _number(transpose.findtext("chromatic")) or Fraction(0)
            octaves = _integer(transpose.findtext("octave-change")) or 0
            if abs(chromatic) > 127 or abs(octaves) > 10:
                continue
            semitones = _round_half_up(chromatic)
            keeps_spelling = semitones % 12 == 0
            self.transpose[_integer(transpose.get("number"))] = (
                semitones + 12 * octaves, keeps_spelling,
            )
            if not keeps_spelling and self.part.label not in self.tally.respelled_parts:
                self.tally.respelled_parts.append(self.part.label)
            if transpose.find("double") is not None:
                self.tally.doubled_transposition = True

        for key in element.findall("key"):
            if (key.get("number") or "1").strip() != "1":
                continue
            fifths = _integer(key.findtext("fifths"))
            if fifths is None or not -7 <= fifths <= 7:
                self.tally.odd_key = True
                break
            shift = self._transposition(1)[0]
            if shift:  # a transposing part writes its key transposed too
                fifths += _key_shift(shift)
                fifths += 12 if fifths < -7 else -12 if fifths > 7 else 0
            mode = (key.findtext("mode") or "major").strip().lower()
            if mode not in ("major", "minor"):
                self.tally.modal_key = True
                mode = "major"
            self.part.keys.append((self.measure, self.cursor, fifths, mode))
            break

        for time in element.findall("time"):
            if (time.get("number") or "1").strip() != "1":
                continue
            self._time(time)
            break

        style = element.find("measure-style")
        if style is not None and (
            style.find("measure-repeat") is not None or style.find("beat-repeat") is not None
        ):
            self.tally.measure_repeat = True

    def _time(self, time: ET.Element) -> None:
        counts = time.findall("beats")
        units = time.findall("beat-type")
        if time.find("senza-misura") is not None or not counts:
            self.tally.senza_misura = True
            self.part.meters[self.measure] = (4, 4)
            return
        pairs: list[tuple[int, int]] = []
        for count, unit in zip(counts, units, strict=False):
            terms = [_integer(t) for t in (count.text or "").split("+")]
            denominator = _integer(unit.text)
            if denominator not in (1, 2, 4, 8, 16, 32, 64) or any(
                t is None or t < 1 for t in terms
            ):
                self.tally.bad_time += 1
                return
            pairs.append((sum(terms), denominator))
        if not pairs:
            self.tally.bad_time += 1
            return
        common = max(d for _, d in pairs)
        numerator = sum(n * (common // d) for n, d in pairs)  # 3/8 + 2/4 -> 7/8
        if not 1 <= numerator <= 64:
            self.tally.bad_time += 1
            return
        self.part.meters[self.measure] = (numerator, common)

    def _transposition(self, staff: int) -> tuple[int, bool]:
        return self.transpose.get(staff) or self.transpose.get(None) or (0, True)

    # --- directions ------------------------------------------------------

    def _direction(self, element: ET.Element) -> None:
        staff = _integer(element.findtext("staff"))
        offset_el = element.find("offset")
        offset = self._beats(offset_el) or Fraction(0)
        seen_at = max(Fraction(0), self.cursor + offset)  # where the eye finds the mark
        offset_sounds = offset_el is not None and offset_el.get("sound") == "yes"

        metronome_bpm: float | None = None
        has_pedal_mark = False
        for direction_type in element.findall("direction-type"):
            for item in direction_type:
                tag = item.tag
                if tag == "dynamics":
                    for mark in item:
                        text = mark.tag
                        if text == "other-dynamics":
                            text = _clean_text(mark.text, 24)
                        if text:
                            self.part.markings.append(
                                _Marking(self.measure, seen_at, staff, text, self._next_order())
                            )
                elif tag == "pedal":
                    has_pedal_mark = True
                    self._pedal((item.get("type") or "").strip().lower(), seen_at)
                elif tag == "metronome":
                    metronome_bpm = self._metronome(item)
                elif tag in ("segno", "coda"):
                    self.tally.jumps.add(tag)

        sound = element.find("sound")
        sounded = False
        if sound is not None:
            base = self.cursor + (offset if offset_sounds else Fraction(0))
            sounded = self._sound_at(sound, base, pedal=not has_pedal_mark)
        if metronome_bpm is not None and not sounded:
            self._tempo(metronome_bpm, seen_at)

    def _sound(self, element: ET.Element) -> None:
        self._sound_at(element, self.cursor, pedal=True)

    def _sound_at(self, sound: ET.Element, base: Fraction, *, pedal: bool) -> bool:
        """Returns True when the element stated a usable tempo."""
        own_offset = self._beats(sound.find("offset")) or Fraction(0)
        at = max(Fraction(0), base + own_offset)
        for attr in _JUMP_SOUND_ATTRS:
            if sound.get(attr) is not None:
                self.tally.jumps.add(attr)
        if pedal and sound.get("damper-pedal") is not None:
            value = sound.get("damper-pedal", "").strip().lower()
            number = _number(value)
            down = value == "yes" or (number is not None and number > 0)
            self._pedal("start" if down else "stop", at)
        if sound.get("tempo") is None:
            return False
        tempo = _number(sound.get("tempo"))
        if tempo is None or tempo <= 0:
            self.tally.bad_tempo += 1
            return False
        self._tempo(float(tempo), at)
        return True

    def _tempo(self, bpm: float, at: Fraction) -> None:
        if not MIN_BPM <= bpm <= MAX_BPM:
            self.tally.clamped_tempo += 1
            bpm = min(max(bpm, MIN_BPM), MAX_BPM)
        self.part.tempos.append((self.measure, at, self._next_order(), bpm))

    def _metronome(self, element: ET.Element) -> float | None:
        """beat-unit (+dots) = per-minute -> quarter notes per minute."""
        unit: Fraction | None = None
        dots = 0
        for child in element:
            if child.tag == "beat-unit":
                if unit is not None:  # "quarter = dotted quarter": a ratio, not a tempo
                    self.tally.metric_modulation += 1
                    return None
                unit = _BEAT_UNITS.get((child.text or "").strip().lower())
                if unit is None:
                    self.tally.bad_tempo += 1
                    return None
            elif child.tag == "beat-unit-dot":
                dots += 1
            elif child.tag == "per-minute":
                match = _PER_MINUTE_RE.search(child.text or "")
                if unit is None or match is None or Fraction(match.group()) <= 0:
                    self.tally.bad_tempo += 1
                    return None
                dotted = unit * (2 - Fraction(1, 2 ** min(dots, 4)))
                return float(Fraction(match.group()) * dotted)
        if element.find("metronome-note") is not None:
            self.tally.metric_modulation += 1
        else:
            self.tally.bad_tempo += 1
        return None

    def _pedal(self, kind: str, at: Fraction) -> None:
        if kind in ("start", "stop", "change"):
            self.part.pedals.append((self.measure, at, self._next_order(), kind))
        elif kind == "sostenuto":
            self.tally.sostenuto = True
        # "continue" / "discontinue" / "resume" only describe how the line is drawn

    def _barline(self, element: ET.Element) -> None:
        if element.find("repeat") is not None:
            self.tally.jumps.add("repeat")
        if element.find("ending") is not None:
            self.tally.jumps.add("ending")
        for tag in ("segno", "coda"):
            if element.find(tag) is not None:
                self.tally.jumps.add(tag)

    # --- notes -----------------------------------------------------------

    def _note(self, element: ET.Element) -> None:
        if element.find("grace") is not None:
            self.tally.grace += 1  # no duration: takes no time, moves nothing
            return

        if not self.divisions_known:
            self.tally.missing_divisions = True
        length = self._beats(element.find("duration"))
        usable = length is not None and length >= 0
        if not usable:
            length = Fraction(0)
        in_chord = element.find("chord") is not None
        if in_chord:
            start = self.chord_start  # sounds with the previous note; the cursor stays
        else:
            start = self.chord_start = self.cursor
            self.chord_articulations = ()
            self._move(self.cursor + length)

        if element.find("rest") is not None:
            if not usable:
                self.tally.no_duration += 1
            return
        if element.find("cue") is not None:
            self.tally.cue += 1
            return
        if element.find("unpitched") is not None:
            self.part.uses_unpitched = True
            self.tally.percussion += 1
            return
        pitch_el = element.find("pitch")
        if pitch_el is None:
            self.tally.bad_pitch += 1
            return
        if self.percussion_channel:
            self.tally.percussion += 1  # a "pitch" on the drum channel names a drum
            return

        note_id = self._note_id(element.get("id"))
        self.k += 1

        staff = _integer(element.findtext("staff"))
        staff = staff if staff is not None and 1 <= staff <= 64 else 1
        self.part.max_staff = max(self.part.max_staff, staff)

        if not usable or length == 0:
            self.tally.no_duration += 1
            return

        step = (pitch_el.findtext("step") or "").strip().upper()
        octave = _integer(pitch_el.findtext("octave"))
        alter_text = pitch_el.findtext("alter")
        alter_exact = _number(alter_text) if alter_text and alter_text.strip() else Fraction(0)
        if (
            step not in _STEP_SEMITONES
            or octave is None
            or not 0 <= octave <= 10
            or alter_exact is None
            or abs(alter_exact) > 4
        ):
            self.tally.bad_pitch += 1
            return
        alter = _round_half_up(alter_exact)
        if alter_exact.denominator != 1:
            self.tally.microtonal += 1
        shift, keeps_spelling = self._transposition(staff)
        pitch = 12 * (octave + 1) + _STEP_SEMITONES[step] + alter + shift
        if not 0 <= pitch <= 127:
            self.tally.bad_pitch += 1
            return

        tie_start, tie_stop = self._ties(element)
        articulations, rolled = self._notations(element)
        # Articulations belong to the stem. Files state them once, on the
        # chord's first note; the notes stacked on it are played the same way.
        if not in_chord:
            self.chord_articulations = articulations
        elif not articulations:
            articulations = self.chord_articulations

        own_velocity = None
        percent = _number(element.get("dynamics"))
        if percent is not None and percent > 0:
            # MusicXML: a percentage of the default forte velocity, which is 90.
            own_velocity = min(127, max(1, _round_half_up(percent * 90 / 100)))

        if not tie_stop:  # a continuation is not a new note; the final count is re-checked
            self.budget[0] -= 1
            if self.budget[0] < 0:
                raise LimitExceeded(
                    "This score has more notes than this service will process.",
                    detail=f"max_notes={self.limits.max_notes}",
                )

        self.part.notes.append(
            _RawNote(
                measure=self.measure,
                offset=start,
                length=length,
                pitch=pitch,
                spelling=(step, alter) if keeps_spelling else None,
                staff=staff,
                voice=self._voice(element.findtext("voice")),
                id=note_id,
                order=self._next_order(),
                tie_start=tie_start,
                tie_stop=tie_stop,
                articulations=articulations,
                rolled=rolled,
                own_velocity=own_velocity,
            )
        )

    def _note_id(self, stated: str | None) -> str:
        """The file's own id when it is usable, else p{part}m{measure}n{k}."""
        if (
            stated
            and len(stated) <= _MAX_ID_CHARS
            and (stated[0].isalpha() or stated[0] == "_")
            and all(ch.isalnum() or ch in "-_." for ch in stated)
            and stated not in self.used_ids
        ):
            self.used_ids.add(stated)
            return stated
        generated = base = f"p{self.part.index}m{self.measure}n{self.k}"
        clash = 1
        while generated in self.used_ids:  # only if the file itself used our pattern
            clash += 1
            generated = f"{base}_{clash}"
        self.used_ids.add(generated)
        return generated

    def _voice(self, text: str | None) -> int:
        text = (text or "").strip()
        if not text:
            return 1
        number = _integer(text)
        if number is not None and 1 <= number <= 1000:
            return number
        # Non-numeric voice names are legal. Number them as they appear.
        return self.voices.setdefault(text[:32], 1001 + len(self.voices))

    @staticmethod
    def _ties(element: ET.Element) -> tuple[bool, bool]:
        """(starts a tie, ends a tie). <tie> is the sounding truth; <tied> is
        how it is drawn, consulted only when <tie> is absent."""
        kinds = {(t.get("type") or "").strip().lower() for t in element.findall("tie")}
        if not kinds:
            kinds = {
                (t.get("type") or "").strip().lower()
                for t in element.findall("notations/tied")
            }
        if "continue" in kinds:
            kinds |= {"start", "stop"}
        return "start" in kinds, "stop" in kinds

    def _notations(self, element: ET.Element) -> tuple[tuple[str, ...], bool]:
        found: list[str] = []
        rolled = False
        for notations in element.findall("notations"):
            for item in notations:
                tag = item.tag
                if tag == "articulations":
                    for mark in item:
                        name = _ARTICULATIONS.get(mark.tag)
                        if name and name not in found:
                            found.append(name)
                elif tag == "fermata":
                    if "fermata" not in found:
                        found.append("fermata")
                elif tag == "arpeggiate":
                    rolled = True
                elif tag == "ornaments":
                    if any(child.tag in _ORNAMENTS for child in item):
                        self.tally.ornaments += 1
                elif tag in ("glissando", "slide"):
                    if (item.get("type") or "").strip().lower() == "start":
                        self.tally.ornaments += 1
                elif tag == "technical" and item.find("harmonic") is not None:
                    self.tally.harmonics += 1
        return tuple(found), rolled

    _HANDLERS = {
        "note": _note,
        "backup": _backup,
        "forward": _forward,
        "attributes": _attributes,
        "direction": _direction,
        "sound": _sound,
        "barline": _barline,
    }


# =========================================================================
# Music layer: from raw events to a Score
# =========================================================================


def _partwise(root: ET.Element) -> list[tuple[str, list[ET.Element]]]:
    """[(part id, its measures)] in score order, whichever way the file is laid out."""
    if root.tag == "score-partwise":
        return [(part.get("id") or "", part.findall("measure")) for part in root.findall("part")]

    # score-timewise: measures hold parts. Turn it inside out.
    columns: dict[str, list[ET.Element]] = {}
    outer = root.findall("measure")
    for index, measure in enumerate(outer):
        for part in measure.findall("part"):
            column = columns.setdefault(part.get("id") or "", [])
            while len(column) < index:  # this part sat out earlier measures
                column.append(ET.Element("measure", dict(outer[len(column)].attrib)))
            if len(column) == index:  # a part listed twice in one measure: first wins
                inner = ET.Element("measure", dict(measure.attrib))
                inner.extend(list(part))
                column.append(inner)
    return list(columns.items())


def _part_list(root: ET.Element, limits: ImportLimits) -> dict[str, _PartMeta]:
    metas: dict[str, _PartMeta] = {}
    for score_part in root.findall("part-list/score-part"):
        meta = _PartMeta(name=_clean_text(score_part.findtext("part-name"), limits.max_title_chars))
        midi = score_part.find("midi-instrument")
        if midi is not None:
            channel = _integer(midi.findtext("midi-channel"))
            if channel is not None and 1 <= channel <= 16:
                meta.channel = channel - 1
            program = _integer(midi.findtext("midi-program"))
            if program is not None and 1 <= program <= 128:
                meta.program = program - 1
        metas.setdefault(score_part.get("id") or "", meta)
    return metas


def _title_and_composer(
    root: ET.Element, filename: str | None, limits: ImportLimits
) -> tuple[str, str]:
    cap = limits.max_title_chars
    title = _clean_text(root.findtext("work/work-title"), cap)
    if not title:
        title = _clean_text(root.findtext("movement-title"), cap)
    if not title:
        for credit in root.findall("credit"):
            kinds = {(t.text or "").strip().lower() for t in credit.findall("credit-type")}
            if "title" in kinds:
                words = " ".join(w.text or "" for w in credit.findall("credit-words"))
                title = _clean_text(words, cap)
                if title:
                    break
    if not title and filename:
        name = filename.replace("\\", "/").rpartition("/")[2]
        stem = name.rpartition(".")[0] if "." in name.strip(".") else name
        title = _clean_text(stem, cap)
    composer = ""
    for creator in root.findall("identification/creator"):
        if (creator.get("type") or "").strip().lower() == "composer":
            composer = _clean_text(creator.text, cap)
            if composer:
                break
    return title or "untitled", composer


def _merge_ties(notes: list[_RawNote], tally: _Tally) -> list[_RawNote]:
    """Tied notes become one note. `notes` is sorted by (start, document order).

    A continuation must carry a tie stop AND begin exactly where an open tie
    of the same pitch ends. Same staff and voice is preferred; any voice of
    the part is accepted, because real files do tie across voices. Anything
    less than that is left alone: an unterminated tie swallows nothing.
    """
    merged: list[_RawNote] = []
    open_ties: dict[int, list[_RawNote]] = {}
    for note in notes:
        waiting = open_ties.get(note.pitch)
        if waiting:
            live = [w for w in waiting if w.start + w.length >= note.start]
            tally.unterminated_ties += len(waiting) - len(live)
            waiting[:] = live
        if note.tie_stop:
            adjacent = [w for w in waiting or () if w.start + w.length == note.start]
            head = next(
                (w for w in adjacent if (w.staff, w.voice) == (note.staff, note.voice)),
                adjacent[0] if adjacent else None,
            )
            if head is not None:
                head.length += note.length
                head.articulations += tuple(
                    a for a in note.articulations if a not in head.articulations
                )
                head.rolled = head.rolled or note.rolled
                if not note.tie_start:
                    waiting.remove(head)
                continue
            tally.orphan_tie_stops += 1
        merged.append(note)
        if note.tie_start:
            open_ties.setdefault(note.pitch, []).append(note)
    tally.unterminated_ties += sum(len(w) for w in open_ties.values())
    return merged


def _apply_dynamics(notes: list[_RawNote], markings: list[_Marking]) -> None:
    """Velocity for every note from the prevailing marking; the marking's text
    on the first note it governs. `notes` is sorted by (start, document order).

    A marking belongs to its staff. A staff that never gets a marking of its
    own follows the part's: piano dynamics are written once, between the
    staves, and mean both hands.
    """
    markings = sorted(markings, key=lambda m: (m.start, m.order))
    own_staves = {m.staff for m in markings if m.staff is not None}
    sequences: dict[int, tuple[list[Fraction], list[_Marking], list[int]]] = {}

    def sequence_for(staff: int):
        if staff not in sequences:
            chosen = (
                [m for m in markings if m.staff in (staff, None)]
                if staff in own_staves
                else markings
            )
            level = DEFAULT_VELOCITY
            levels: list[int] = []
            for m in chosen:
                if m.text in _DYNAMIC_LEVELS:
                    level = _DYNAMIC_LEVELS[m.text]
                elif m.text in _DYNAMIC_ATTACKS:
                    level = _DYNAMIC_LEVELS[_DYNAMIC_ATTACKS[m.text][1]]
                levels.append(level)  # anything else leaves the level where it was
            sequences[staff] = ([m.start for m in chosen], chosen, levels)
        return sequences[staff]

    claimed_at: dict[int, Fraction] = {}
    carrier: dict[int, _RawNote] = {}
    for note in notes:
        starts, chosen, levels = sequence_for(note.staff)
        i = bisect_right(starts, note.start) - 1
        velocity = DEFAULT_VELOCITY
        if i >= 0:
            marking = chosen[i]
            velocity = levels[i]
            if marking.order not in claimed_at:
                claimed_at[marking.order] = note.start
                carrier[marking.order] = note
                note.dynamic = marking.text
            elif (
                claimed_at[marking.order] == note.start
                and carrier[marking.order].order < marking.order < note.order
            ):
                # Same instant, but this is the note the file wrote the marking
                # in front of (a second voice, after a <backup>): it carries it.
                carrier[marking.order].dynamic = None
                carrier[marking.order] = note
                note.dynamic = marking.text
            struck = _DYNAMIC_ACCENTS.get(marking.text) or _DYNAMIC_ATTACKS.get(
                marking.text, (None,)
            )[0]
            if struck is not None and claimed_at[marking.order] == note.start:
                # the whole chord under an sfz is struck, not just its first note
                before = levels[i - 1] if i > 0 else DEFAULT_VELOCITY
                velocity = min(127, max(struck, before + 16))
        note.velocity = note.own_velocity or velocity


def _pedal_spans(
    events_by_part: list[list[tuple[Fraction, int, str]]], timeline: Timeline, end: Fraction
) -> list[PedalSpan]:
    beats: list[tuple[Fraction, Fraction]] = []
    for events in events_by_part:
        down: Fraction | None = None
        for at, _order, kind in sorted(events):
            if kind == "start":
                if down is None:
                    down = at
            elif down is not None:  # "stop", or "change" = lift and catch again
                beats.append((down, at))
                down = at if kind == "change" else None
            elif kind == "change":
                down = at
        if down is not None:
            beats.append((down, end))  # never lifted: held to the end of the piece

    spans = sorted(
        (timeline.seconds_at(float(a)), timeline.seconds_at(float(b))) for a, b in beats if b > a
    )
    merged: list[PedalSpan] = []
    for start, stop in spans:
        # Two parts pedalling at once is one pedal. Spans that merely touch
        # stay separate: that is a pedal change, and the lift is the point.
        if merged and start < merged[-1].end - 1e-9:
            merged[-1] = PedalSpan(merged[-1].start, max(merged[-1].end, stop))
        else:
            merged.append(PedalSpan(start, stop))
    return merged


def _build_score(root: ET.Element, filename: str | None, limits: ImportLimits) -> Score:
    layout = _partwise(root)
    if len(layout) > limits.max_tracks:
        raise LimitExceeded(
            "This score has more parts than this service will process.",
            detail=f"{len(layout)} parts > max_tracks={limits.max_tracks}",
        )
    if not layout:
        raise EmptyScore("This score contains no music.", detail="no <part> elements")

    metas = _part_list(root, limits)
    tally = _Tally()
    budget = [limits.max_notes]
    used_ids: set[str] = set()
    parts: list[_PartData] = []
    for index, (part_id, measures) in enumerate(layout):
        if len(measures) > limits.max_bars:
            raise LimitExceeded(
                "This score has more bars than this service will process.",
                detail=f"part {index}: {len(measures)} measures > max_bars={limits.max_bars}",
            )
        part = _PartData(index=index, meta=metas.get(part_id, _PartMeta()))
        _PartWalker(part, tally, limits, budget, used_ids).walk(measures)
        parts.append(part)

    warnings: list[str] = []

    # --- measures: how long each one really is ----------------------------
    n_measures = max(len(p.measure_lengths) for p in parts)
    ragged = any(len(p.measure_lengths) != n_measures for p in parts)
    lengths = [
        max(
            (p.measure_lengths[i] for p in parts if i < len(p.measure_lengths)),
            default=Fraction(0),
        )
        for i in range(n_measures)
    ]

    meter_part = next((p for p in parts if p.meters), None)
    stated = meter_part.meters if meter_part else {}
    nominal: list[tuple[int, int]] = []
    current = stated[min(stated)] if stated else (4, 4)  # a late first <time> governs the opening
    for i in range(n_measures):
        current = stated.get(i, current)
        nominal.append(current)

    empty: list[int] = []
    for i, length in enumerate(lengths):
        if length == 0:  # drawn on the page but holds nothing: it still takes its bar
            lengths[i] = _bar_beats(nominal[i])
            empty.append(i + 1)

    pickup = Fraction(0)
    if n_measures > 1 and 0 < lengths[0] < _bar_beats(nominal[0]):
        pickup = lengths[0]

    meter_changes: list[MeterChange] = []
    irregular: list[int] = []
    unbarrable: list[int] = []
    in_force: tuple[int, int] | None = None
    for i, length in enumerate(lengths):
        effective = nominal[i]
        full = _bar_beats(effective)
        is_pickup = i == 0 and pickup > 0
        short_final_bar = i == n_measures - 1 and length < full
        if length != full and not is_pickup and not short_final_bar:
            fitted = _meter_for_length(length, effective[1])
            if fitted is None:
                unbarrable.append(i + 1)
            else:
                effective = fitted
                irregular.append(i + 1)
        if effective != in_force:
            meter_changes.append(MeterChange(i + 1, *effective))
            in_force = effective

    starts: list[Fraction] = []
    total = Fraction(0)
    for length in lengths:
        starts.append(total)
        total += length
        if total.denominator > _MAX_DENOMINATOR:
            raise MalformedFile(
                "This file's timing is not usable.",
                detail="measure start denominator exceeds the sane bound",
            )

    def absolute(measure: int, offset: Fraction) -> Fraction:
        return starts[measure] + offset

    # --- tempo -------------------------------------------------------------
    tempo_events = sorted(
        (absolute(measure, offset), part.index, order, bpm)
        for part in parts
        for measure, offset, order, bpm in part.tempos
    )
    by_beat: dict[Fraction, float] = {}
    for beat, _part, _order, bpm in tempo_events:
        by_beat[beat] = bpm  # later wins, as in Timeline
    if len(by_beat) > MAX_TEMPO_CHANGES:
        raise LimitExceeded(
            "This score has more tempo changes than this service will process.",
            detail=f"{len(by_beat)} tempo changes",
        )
    if not by_beat:
        by_beat[Fraction(0)] = 120.0
        warnings.append(
            "No tempo is stated in this score; 120 quarter notes per minute was assumed."
        )
    elif Fraction(0) not in by_beat:
        first = by_beat[min(by_beat)]
        by_beat[Fraction(0)] = first
        warnings.append(
            "The first tempo marking comes after the music starts; its tempo "
            f"({first:g} quarter notes per minute) was assumed from the beginning."
        )
    tempo_changes = [TempoChange(float(b), by_beat[b]) for b in sorted(by_beat)]

    # --- key ---------------------------------------------------------------
    key_part = next((p for p in parts if p.keys), None)
    key_changes = [
        KeyChange(float(absolute(measure, offset)), fifths, mode)
        for measure, offset, fifths, mode in (key_part.keys if key_part else [])
    ]

    try:
        timeline = Timeline(tempo_changes, meter_changes, key_changes, pickup_beats=float(pickup))
    except TimelineError as exc:
        raise LimitExceeded(
            "This score changes tempo, meter or key more often than this service "
            "will process.",
            detail=f"timeline: {exc}",
        ) from exc

    # --- notes -------------------------------------------------------------
    notes: list[Note] = []
    tracks: list[TrackInfo] = []
    end = total
    for part in parts:
        for raw in part.notes:
            raw.start = absolute(raw.measure, raw.offset)
        for marking in part.markings:
            marking.start = absolute(marking.measure, marking.offset)
        sounding = _merge_ties(sorted(part.notes, key=lambda n: (n.start, n.order)), tally)
        _apply_dynamics(sounding, part.markings)

        two_staves = part.max_staff >= 2
        if part.max_staff > 2:
            tally.extra_staff_parts.append(part.label)
        for raw in sounding:
            finish = raw.start + raw.length
            end = max(end, finish)
            beat = float(raw.start)
            onset = timeline.seconds_at(beat)
            notes.append(
                Note(
                    pitch=raw.pitch,
                    onset=onset,
                    duration=max(timeline.seconds_at(float(finish)) - onset, 1e-6),
                    staff=raw.staff if two_staves and raw.staff <= 2 else None,
                    bar=timeline.bar_at(beat),
                    voice=raw.voice,
                    id=raw.id,
                    velocity=raw.velocity,
                    beat=beat,
                    beats=float(raw.length),
                    track=part.index,
                    spelling=raw.spelling,
                    articulations=raw.articulations,
                    dynamic=raw.dynamic,
                    rolled=raw.rolled,
                )
            )
        pitches = [raw.pitch for raw in sounding]
        tracks.append(
            TrackInfo(
                index=part.index,
                name=part.meta.name or f"Part {part.index + 1}",
                program=part.meta.program,
                channel=part.meta.channel,
                is_percussion=part.uses_unpitched or part.meta.channel == 9,
                note_count=len(pitches),
                lowest_pitch=min(pitches, default=None),
                highest_pitch=max(pitches, default=None),
            )
        )
        if len(notes) > limits.max_notes:
            raise LimitExceeded(
                "This score has more notes than this service will process.",
                detail=f"max_notes={limits.max_notes}",
            )

    if not notes:
        raise EmptyScore(
            "This score contains no pitched notes."
            if tally.percussion
            else "This score contains no notes.",
            detail=f"percussion={tally.percussion} grace={tally.grace} cue={tally.cue}",
        )
    end_seconds = timeline.seconds_at(float(end))
    if end_seconds > limits.max_duration_seconds:
        raise LimitExceeded(
            "This piece is longer than this service will process.",
            detail=f"{end_seconds:.0f}s > max_duration_seconds={limits.max_duration_seconds}",
        )
    # Measures were counted on the way in, but a measure no meter can express
    # spreads over several timeline bars. The numbers notes carry are what counts.
    last_bar = max(n.bar for n in notes)
    if last_bar > limits.max_bars:
        raise LimitExceeded(
            "This score has more bars than this service will process.",
            detail=f"bar {last_bar} > max_bars={limits.max_bars}",
        )

    pedals = _pedal_spans(
        [[(absolute(m, o), order, kind) for m, o, order, kind in p.pedals] for p in parts],
        timeline,
        end,
    )

    warnings += _warnings(
        tally,
        time_stated=bool(stated),
        meters_disagree=any(p.meters and p.meters != stated for p in parts),
        meter_label=meter_part.label if meter_part else "",
        irregular=irregular,
        unbarrable=unbarrable,
        empty=empty,
        ragged=ragged,
    )
    title, composer = _title_and_composer(root, filename, limits)
    return Score(
        notes=notes,
        tempo_bpm=timeline.initial_bpm,
        title=title,
        timeline=timeline,
        pedals=pedals,
        tracks=tracks,
        composer=composer,
        source_format="musicxml",
        warnings=warnings,
    )


def _warnings(
    tally: _Tally,
    *,
    time_stated: bool,
    meters_disagree: bool,
    meter_label: str,
    irregular: list[int],
    unbarrable: list[int],
    empty: list[int],
    ragged: bool,
) -> list[str]:
    """Everything that could not be represented faithfully, in plain words."""
    out: list[str] = []
    if not time_stated:
        out.append("No time signature is stated in this score; 4/4 was assumed.")
    if tally.senza_misura:
        out.append(
            "This score has unmeasured (senza misura) music; 4/4 was assumed and each "
            "such measure keeps the length it was written with."
        )
    if tally.bad_time:
        out.append(
            f"{_were(tally.bad_time, 'time signature')} not understood and ignored."
        )
    if meters_disagree:
        out.append(
            f"The parts disagree about time signatures; those of {meter_label[:40]!r} were used."
        )
    if irregular:
        out.append(
            f"{_plural(len(irregular), 'measure')} did not match the time signature "
            f"(bar {_bars(irregular)}). Each keeps its written length, shown as a "
            "one-bar meter change."
        )
    if unbarrable:
        out.append(
            f"{_plural(len(unbarrable), 'measure')} had a length no time signature can "
            f"express (source measure {_bars(unbarrable)}); bar numbers after that point "
            "follow the time signature, not the source's barlines."
        )
    if empty:
        out.append(
            f"{_plural(len(empty), 'measure')} held no notes or rests in any part "
            f"(bar {_bars(empty)}); each was given a full bar of silence."
        )
    if ragged:
        out.append(
            "The parts do not all have the same number of measures; shorter parts are "
            "silent at the end."
        )
    if tally.jumps:
        kinds = sorted({_JUMP_LABELS[j] for j in tally.jumps})
        out.append(
            f"This score has repeats or jumps ({', '.join(kinds)}). They are not "
            "expanded: the music was imported once, straight through, as written."
        )
    if tally.bad_tempo:
        out.append(
            f"{_were(tally.bad_tempo, 'tempo marking')} not understood and ignored."
        )
    if tally.clamped_tempo:
        out.append(
            f"{_were(tally.clamped_tempo, 'tempo marking')} outside "
            f"{MIN_BPM:g}-{MAX_BPM:g} quarter notes per minute and clamped to that range."
        )
    if tally.metric_modulation:
        out.append(
            f"{_were(tally.metric_modulation, 'metric modulation')} (note = note) not "
            "applied; the tempo is unchanged there."
        )
    if tally.missing_divisions:
        out.append(
            "Notes appear before any <divisions>; one division per quarter note was assumed."
        )
    if tally.respelled_parts:
        names = ", ".join(repr(n[:40]) for n in tally.respelled_parts[:5])
        more = ", ..." if len(tally.respelled_parts) > 5 else ""
        out.append(
            f"Transposing part(s) {names}{more} were converted to sounding pitch; their "
            "written note spellings no longer apply and were dropped."
        )
    if tally.doubled_transposition:
        out.append("A part asks to be doubled an octave lower; the doubling was not added.")
    if tally.grace:
        out.append(
            f"{_were(tally.grace, 'grace note')} skipped: grace notes have no duration "
            "of their own."
        )
    if tally.cue:
        out.append(f"{_were(tally.cue, 'cue note')} skipped: cue notes belong to another part.")
    if tally.percussion:
        out.append(f"{_were(tally.percussion, 'unpitched percussion note')} left out.")
    if tally.microtonal:
        out.append(
            f"{_were(tally.microtonal, 'microtonal note')} rounded to the nearest "
            "semitone."
        )
    if tally.bad_pitch:
        out.append(
            f"{_were(tally.bad_pitch, 'note')} skipped: no usable pitch, or a pitch "
            "outside the MIDI range."
        )
    if tally.no_duration:
        out.append(
            f"{_were(tally.no_duration, 'duration')} unreadable; the notes or rests "
            "involved were skipped and timing near them may be off."
        )
    if tally.backup_underflow:
        out.append(
            f"{_were(tally.backup_underflow, '<backup>')} stopped at the barline instead "
            "of reaching back past the start of the measure."
        )
    loose_ties = tally.orphan_tie_stops + tally.unterminated_ties
    if loose_ties:
        out.append(
            f"{_plural(loose_ties, 'tie')} had no matching note at the other end; the "
            "notes involved keep their written lengths."
        )
    if tally.ornaments:
        out.append(
            f"{_were(tally.ornaments, 'ornament')} imported as the plain written note "
            "(trills, turns, mordents, tremolos and glissandi are not played out)."
        )
    if tally.harmonics:
        out.append(f"{_were(tally.harmonics, 'string harmonic')} imported at written pitch.")
    if tally.measure_repeat:
        out.append(
            "Measure-repeat or beat-repeat signs are present; only the notes actually "
            "written in the file were imported."
        )
    if tally.sostenuto:
        out.append("Sostenuto (middle) pedal markings were ignored.")
    if tally.extra_staff_parts:
        names = ", ".join(repr(n[:40]) for n in tally.extra_staff_parts[:5])
        out.append(
            f"Part(s) {names} have more than two staves; notes on staff 3 and beyond "
            "were imported without a staff."
        )
    if tally.odd_key:
        out.append("A key signature that is not a plain circle-of-fifths key was ignored.")
    if tally.modal_key:
        out.append(
            "A modal key signature was recorded as major with the same number of sharps "
            "or flats."
        )
    return out
