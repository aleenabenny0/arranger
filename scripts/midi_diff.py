"""Parse MIDI files with the C parser and the Python reader, and diff the results.

    python scripts/midi_diff.py --dump c/midi/build/midi_dump evals/corpus/*.mid
    python scripts/midi_diff.py --dump c/midi/build/midi_dump --generated 200 --mutations 300

Both parsers are meant to read exactly the same events from a file and to
refuse exactly the same malformed input. This script is how that claim is
checked: every file is parsed by `midi_dump` (c/midi) and by
`arranger.io.read_midi_raw`, and the two views are compared field by field.
Besides the files given on the command line it can build its own: well-formed
files written by the project's MIDI writer from synthetic scores, and mutants
of them (truncations and single-byte corruptions from a fixed seed), where the
two parsers must agree on whether the file is refused.

Exit status 0 when everything agrees, 1 on any difference, 2 on a usage or
setup problem. Every difference is printed with the file and the field.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.io import _text, read_midi_raw  # noqa: E402
from arranger.ir import Note, Score  # noqa: E402
from arranger.limits import DEFAULT_LIMITS, ImportLimits, ScoreImportError  # noqa: E402

# The C status names that correspond to each Python error class.
ERROR_CLASSES = {
    "MidiError": {"MIDI_E_NOT_MIDI", "MIDI_E_BAD_HEADER", "MIDI_E_TRUNCATED", "MIDI_E_BAD_VLQ", "MIDI_E_BAD_STATUS", "MIDI_E_NO_TRACKS"},
    "MidiUnsupported": {"MIDI_E_UNSUPPORTED_FORMAT", "MIDI_E_SMPTE"},
    "LimitExceeded": {"MIDI_E_LIMIT"},
}


def c_parse(dump: Path, path: Path, limits: ImportLimits) -> tuple[str, dict]:
    """Run midi_dump; returns ("ok", document) or ("error", document)."""
    command = [
        str(dump), str(path),
        "--max-bytes", str(limits.max_bytes), "--max-tracks", str(limits.max_tracks),
        "--max-notes", str(limits.max_notes), "--max-events", str(limits.max_events),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)  # noqa: S603 - our own binary
    if result.returncode not in (0, 1):
        raise RuntimeError(f"{dump} exited with {result.returncode} on {path}: {result.stderr.strip()}")
    document = json.loads(result.stdout)
    return ("error" if result.returncode == 1 else "ok"), document


def python_parse(data: bytes, limits: ImportLimits) -> tuple[str, dict | str]:
    try:
        return "ok", read_midi_raw(data, limits=limits)
    except ScoreImportError as error:
        return "error", type(error).__name__
    except Exception as error:  # noqa: BLE001 - a crash is a finding, reported below
        return "crash", f"{type(error).__name__}: {error}"


def normalise_c(document: dict) -> dict:
    """The C document in the Python reader's terms: names decoded and cleaned."""
    out = dict(document)
    tracks = []
    for track in document["tracks"]:
        t = dict(track)
        for key in ("name", "instrument"):
            raw = bytes(ord(ch) for ch in track[key])   # each byte was escaped as its own code point
            t[key] = _text(raw, DEFAULT_LIMITS.max_title_chars)
        t["notes"] = sorted(track["notes"])
        t["programs"] = sorted(track["programs"])
        tracks.append(t)
    out["tracks"] = tracks
    return out


def differences(c_doc: dict, py_doc: dict) -> list[str]:
    found: list[str] = []
    for key in ("format", "division", "declared_tracks", "truncated"):
        if c_doc[key] != py_doc[key]:
            found.append(f"{key}: C {c_doc[key]!r} vs Python {py_doc[key]!r}")
    if len(c_doc["tracks"]) != len(py_doc["tracks"]):
        found.append(f"track count: C {len(c_doc['tracks'])} vs Python {len(py_doc['tracks'])}")
        return found
    for index, (c_track, py_track) in enumerate(zip(c_doc["tracks"], py_doc["tracks"], strict=True)):
        for key in ("name", "instrument", "end_tick", "events", "unclosed", "programs", "notes", "tempos",
                    "time_signatures", "key_signatures", "pedal"):
            if c_track[key] != py_track[key]:
                c_value, py_value = c_track[key], py_track[key]
                if isinstance(c_value, list) and isinstance(py_value, list) and len(c_value) > 6:
                    first = next((i for i, (a, b) in enumerate(zip(c_value, py_value, strict=False)) if a != b), min(len(c_value), len(py_value)))
                    detail = f"{len(c_value)} vs {len(py_value)} items; first difference at index {first}: {c_value[first:first + 1]} vs {py_value[first:first + 1]}"
                else:
                    detail = f"C {c_value!r} vs Python {py_value!r}"
                found.append(f"track {index} {key}: {detail}")
    return found


def compare(dump: Path, path: Path, data: bytes, limits: ImportLimits) -> list[str]:
    c_kind, c_doc = c_parse(dump, path, limits)
    py_kind, py_doc = python_parse(data, limits)
    if py_kind == "crash":
        return [f"Python crashed: {py_doc}"]
    if c_kind != py_kind:
        c_what = c_doc.get("error", "parsed") if isinstance(c_doc, dict) else c_doc
        return [f"C {c_kind} ({c_what}) vs Python {py_kind} ({py_doc if py_kind == 'error' else 'parsed'})"]
    if c_kind == "error":
        expected = ERROR_CLASSES.get(str(py_doc), set())
        if c_doc["error"] not in expected:
            return [f"error kind: C {c_doc['error']} vs Python {py_doc}"]
        return []
    assert isinstance(py_doc, dict)
    return differences(normalise_c(c_doc), py_doc)


# --- generated files ---------------------------------------------------------


def synthetic_score(rng: random.Random, index: int) -> Score:
    """A small two-hand piece with tempo and meter changes, from a fixed seed."""
    bars = rng.randint(2, 12)
    beats_per_bar = rng.choice([3, 4, 6])
    notes: list[Note] = []
    tempo = rng.choice([60.0, 92.0, 120.0, 144.0])
    for bar in range(bars):
        for beat in range(beats_per_bar):
            onset = (bar * beats_per_bar + beat) * 60.0 / tempo
            length = 60.0 / tempo * rng.choice([0.5, 1.0, 1.5])
            notes.append(Note(pitch=rng.randint(60, 84), onset=onset, duration=length, staff=1, velocity=rng.randint(40, 110)))
            if beat % 2 == 0:
                notes.append(Note(pitch=rng.randint(36, 59), onset=onset, duration=length * 2, staff=2, velocity=rng.randint(40, 100)))
            if rng.random() < 0.2:  # a doubled note: the same pitch retriggered
                notes.append(Note(pitch=notes[-1].pitch, onset=onset + length / 2, duration=length / 2, staff=2, velocity=64))
    return Score(notes=notes, tempo_bpm=tempo, title=f"Generated {index}")


def generated_files(count: int, seed: int) -> list[tuple[str, bytes]]:
    rng = random.Random(seed)
    return [(f"generated-{i}.mid", write_midi(synthetic_score(rng, i))) for i in range(count)]


def mutants(originals: list[tuple[str, bytes]], count: int, seed: int) -> list[tuple[str, bytes]]:
    """Truncations and single-byte corruptions of the originals, from a fixed seed."""
    rng = random.Random(seed)
    out: list[tuple[str, bytes]] = []
    for _ in range(count):
        name, data = originals[rng.randrange(len(originals))]
        kind = rng.choice(["cut", "flip", "flip", "insert", "drop"])
        if kind == "cut" and len(data) > 1:
            at = rng.randrange(1, len(data))
            out.append((f"{name}.cut{at}", data[:at]))
        elif kind == "flip":
            at = rng.randrange(len(data))
            out.append((f"{name}.flip{at}", data[:at] + bytes([data[at] ^ rng.randrange(1, 256)]) + data[at + 1:]))
        elif kind == "insert":
            at = rng.randrange(len(data))
            out.append((f"{name}.insert{at}", data[:at] + bytes([rng.randrange(256)]) + data[at:]))
        else:
            at = rng.randrange(len(data))
            out.append((f"{name}.drop{at}", data[:at] + data[at + 1:]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("files", nargs="*", type=Path, help="MIDI files to compare")
    parser.add_argument("--dump", type=Path, required=True, help="the built c/midi midi_dump executable")
    parser.add_argument("--generated", type=int, default=0, help="also compare this many files written by the MIDI writer")
    parser.add_argument("--mutations", type=int, default=0, help="also compare this many corrupted variants of the inputs")
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--max-events", type=int, default=DEFAULT_LIMITS.max_events)
    parser.add_argument("--max-notes", type=int, default=DEFAULT_LIMITS.max_notes)
    args = parser.parse_args(argv)

    dump = args.dump
    if not dump.exists() and dump.with_suffix(".exe").exists():
        dump = dump.with_suffix(".exe")
    if not dump.exists():
        print(f"midi_dump not found at {dump}; build c/midi first", file=sys.stderr)
        return 2
    limits = ImportLimits(max_events=args.max_events, max_notes=args.max_notes)

    inputs: list[tuple[str, bytes]] = []
    for path in args.files:
        inputs.append((str(path), path.read_bytes()))
    inputs.extend(generated_files(args.generated, args.seed))
    if not inputs:
        print("nothing to compare: give files, or --generated N", file=sys.stderr)
        return 2
    cases = list(inputs) + mutants(inputs, args.mutations, args.seed + 1)

    failures = 0
    agreed_ok = agreed_error = 0
    with tempfile.TemporaryDirectory() as tmp:
        for index, (name, data) in enumerate(cases):
            path = Path(tmp) / f"case-{index}.mid"
            path.write_bytes(data)
            found = compare(dump, path, data, limits)
            if found:
                failures += 1
                print(f"DIFFER {name}")
                for line in found:
                    print(f"    {line}")
            else:
                if python_parse(data, limits)[0] == "ok":
                    agreed_ok += 1
                else:
                    agreed_error += 1
    print(f"{len(cases)} files: {agreed_ok} parsed identically, {agreed_error} refused identically, {failures} differ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
