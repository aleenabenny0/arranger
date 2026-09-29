# C MIDI parser

A bounded Standard MIDI File parser in portable C11, one source file and no
dependencies. It reads the same events as the Python reader in
`src/arranger/io.py` (notes, tempo, time and key signatures, sustain pedal,
program changes, track names), refuses the same malformed input, and applies
the same limits, and `scripts/midi_diff.py` proves it by parsing files with
both and comparing.

```bash
cmake -S c/midi -B c/midi/build -DCMAKE_BUILD_TYPE=Debug -DMIDI_SANITIZE=ON   # GCC or Clang
cmake --build c/midi/build
ctest --test-dir c/midi/build --output-on-failure
c/midi/build/midi_dump evals/corpus/bach-invention-04-bwv775.mid | head -c 400
python scripts/midi_diff.py --dump c/midi/build/midi_dump --generated 200 --mutations 500 evals/corpus/*.mid
```

On Windows with a portable MinGW-w64 GCC and CMake, add `-G Ninja
-DCMAKE_C_COMPILER=gcc` and leave the sanitizers off (they need Linux or
macOS); CI runs the sanitized build on Ubuntu.

## What it handles

Formats 0, 1 and 2; variable-length quantities; running status (channel
messages only, as real files expect); note-on with velocity 0 as note-off;
retriggered pitches closed oldest first; notes still held at the end of a
track closed there and counted; tempo, time signature and key signature meta
events read by their shape and ignored when malformed; track and instrument
names (the first of each, raw bytes); sustain pedal (controller 64); the first
program change per channel; sysex, aftertouch, channel pressure and pitch
bend skipped; unknown chunk types ignored; a chunk that claims more bytes than
the file holds cut at the end of the file and flagged.

## What it refuses, and how

Every read is checked against the buffer, so no input can make the parser read
outside it; that is what the sanitized test run and the corruption tests are
for. `midi_parse` returns a `midi_status` and a sentence in `midi_file.detail`:

| Status | When |
|---|---|
| `MIDI_E_NOT_MIDI` | no `MThd` header |
| `MIDI_E_BAD_HEADER` | header shorter than six bytes, or a division of zero |
| `MIDI_E_UNSUPPORTED_FORMAT`, `MIDI_E_SMPTE` | format 3 or higher; SMPTE time code |
| `MIDI_E_TRUNCATED` | the data ends inside a header, an event or a payload |
| `MIDI_E_BAD_VLQ` | a variable-length quantity longer than four bytes |
| `MIDI_E_BAD_STATUS` | a data byte before any status byte, or an unknown status byte |
| `MIDI_E_NO_TRACKS` | no `MTrk` chunk |
| `MIDI_E_LIMIT` | more bytes, tracks, chunks, events or notes than `midi_limits` allows |
| `MIDI_E_NOMEM` | an output array could not be allocated |

The default limits are the Python reader's `ImportLimits`: 8 MiB, 64 tracks,
60,000 notes per track, 600,000 events per file.

## Layout

| Path | What it is |
|---|---|
| `include/midi.h` | The public API: `midi_parse`, `midi_free`, `midi_read_vlq`, the structs and limits. |
| `src/midi.c` | The parser. Every rule has a twin in `src/arranger/io.py`. |
| `src/midi_dump.c` | `midi_dump FILE [--max-... N]`: the parsed file as JSON, exit 1 with `{"error": ...}` on a parse error. |
| `tests/test_midi.c` | Unity tests: 31 cases, every malformed case next to a well-formed neighbour that must still parse, plus every prefix and every single-byte corruption of a file. |
| `tests/unity/` | Unity 2.6.1 (MIT), vendored; also used by the firmware's host tests. |

## The cross-language diff

`scripts/midi_diff.py` runs `midi_dump` and `arranger.io.read_midi_raw` on
each file and compares format, division, declared tracks, the truncation flag,
and per track the names, end tick, event and unclosed counts, programs, notes,
tempos, time and key signatures and pedal events. On files both refuse it
checks that the C status maps to the Python error class. With `--generated N`
it adds N pieces written by the project's MIDI writer from a fixed seed, and
with `--mutations N` it adds N truncations, bit flips, inserted and dropped
bytes, where the two parsers must still agree. CI runs it over the corpus with
300 generated files and 1000 mutations.
