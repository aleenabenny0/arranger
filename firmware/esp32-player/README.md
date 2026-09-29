# ESP32 player

A one-voice sounder for arrangements: an ESP32 drives a piezo from PWM while
the host streams the notes over UART. It exists to show the whole path from a
MIDI file to a sound on a board, with the parts that can be tested without
hardware tested that way.

```
scripts/send_to_esp32.py ──UART frames──▶ main/main.c ──▶ components/frameproto (parse)
        (host keeps time)                           └──▶ components/voice (which note sounds)
                                                    └──▶ LEDC PWM on the piezo pin, LED on
```

| Path | What it is | Tested |
|---|---|---|
| `components/frameproto/` | The framed protocol: CRC-8, encoder, byte-by-byte parser that resyncs after noise. Plain C. | `host/test_frameproto.c` (10 cases); the same golden bytes in `tests/test_esp32_sender.py` |
| `components/voice/` | The held-note set and the rule for a monophonic sounder (the highest note sounds); the frequency table. Plain C. | `host/test_voice.c` (5 cases, the table checked against equal temperament) |
| `main/main.c` | UART receive task, LEDC tone, activity LED, ACKs, release of held notes when the host goes quiet. ESP-IDF only. | Compiled in CI with ESP-IDF 5.2 (`esp32-build` job); not run on a board in CI |
| `host/` | CMake project that builds the two components with Unity (from `c/midi/tests/unity`) and runs them under CTest, with sanitizers in CI. | `firmware-host` job |
| `../../scripts/send_to_esp32.py` | Reads a MIDI, MusicXML or score JSON file with the project's readers and streams timed frames; `--dry-run` prints them. | `tests/test_esp32_sender.py` |

## Protocol

UART, 115200 baud, 8N1. Every frame, in both directions:

```
0xA5 0x5A | type | length | payload[length] | crc8
```

`length` is 0 to 32. The CRC is CRC-8 with polynomial 0x07, initial value 0,
no reflection and no final XOR, over type, length and payload; the check value
for `"123456789"` is `0xF4`. A receiver drops back to waiting for the sync
bytes on a bad length or CRC, so the link recovers after noise.

| Type | Direction | Payload | Meaning |
|---|---|---|---|
| `0x01 NOTE_ON` | host → device | pitch, velocity | hold a note; velocity 0 releases it, as in MIDI |
| `0x02 NOTE_OFF` | host → device | pitch | release a note |
| `0x03 ALL_OFF` | host → device | none | release everything |
| `0x04 PING` | host → device | none | answered with PONG |
| `0x05 TONE_TEST` | host → device | pitch, duration ms (little-endian u16) | sound one note for a while: a wiring check |
| `0x81 ACK` | device → host | type acknowledged, status | status 0 done, 1 bad payload, 2 unknown type |
| `0x84 PONG` | device → host | protocol version, firmware major, firmware minor | |
| `0x7F ERROR` | device → host | code | 1 bad CRC, 2 bad length |

Held notes are released by the device if no frame arrives for
`PLAYER_IDLE_SILENCE_MS` (5 s), so a host that crashes mid-piece does not
leave the piezo screaming.

## Wiring

| Part | Connection |
|---|---|
| Passive piezo sounder or small speaker | one lead to GPIO 25 through a 220 Ω to 1 kΩ resistor, the other lead to GND |
| Activity LED | GPIO 2 (the onboard LED on most ESP32 dev boards); an external LED needs a series resistor |
| Host link | the board's USB serial bridge (UART0); nothing to wire |

The pins and baud rate are `idf.py menuconfig` options under "Arranger
player" (`main/Kconfig.projbuild`). An active buzzer (one that makes its own
tone) will not follow the pitch; use a passive one.

## Building and flashing

Not done on this project's development machine: there is no ESP-IDF install
and no board here. CI proves the firmware compiles and links with ESP-IDF 5.2
and keeps the `.bin` files as the `esp32-player-firmware` artifact. To flash:

```bash
# once: install ESP-IDF 5.x (https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/)
cd firmware/esp32-player
idf.py set-target esp32
idf.py build
idf.py -p COM3 flash monitor        # /dev/ttyUSB0 on Linux; Ctrl-] leaves the monitor
```

Then, from the repository root, with pyserial installed (`pip install -e ".[esp32]"`):

```bash
python scripts/send_to_esp32.py --port COM3 --tone 69                          # A4 for a second: is it wired?
python scripts/send_to_esp32.py evals/corpus/bach-invention-04-bwv775.mid --port COM3 --melody-only
python scripts/send_to_esp32.py PIECE.mid --dry-run                            # the frames, no board needed
```

The monitor and the host script cannot share the port; leave the monitor
before playing. Boot messages on UART0 are text and the host parser skips
them.

## What is not here

- No playback on the device itself: the host keeps time, so the piece stops
  if the host does. A device-side note queue would be the next step.
- One voice. Chords become their highest note.
- No test on real hardware in CI. The build job uses Espressif's toolchain
  image; a board would need a self-hosted runner.
