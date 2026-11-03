"""Musical time: tempo, meter and key maps, and the conversions between them.

A piece of music has two clocks. *Score time* is measured in quarter-note
beats and is what notation cares about: bar 14, beat 3. *Performed time* is
measured in seconds and is what hands care about: can I get there in 80ms.
The verifier works in seconds; notation export works in beats; this module is
the single place that converts between them.

Dependency-free, like the rest of the domain core.

Bars are numbered from 1. If the piece starts with a pickup (anacrusis), the
pickup *is* bar 1 and is shorter than the meter says. That keeps every bar
number a positive integer, which the plan schema and the verifier rely on.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

# Guard rails. A timeline is built from untrusted files; these caps keep a
# hostile file from turning a lookup table into a memory bomb.
MAX_TEMPO_CHANGES = 20_000
MAX_METER_CHANGES = 5_000
MAX_KEY_CHANGES = 5_000
MIN_BPM = 5.0
MAX_BPM = 1000.0


class TimelineError(ValueError):
    """The timing data is internally inconsistent or out of bounds."""


@dataclass(frozen=True, slots=True)
class TempoChange:
    beat: float  # quarter-note beats from the start
    bpm: float   # quarter notes per minute


@dataclass(frozen=True, slots=True)
class MeterChange:
    bar: int           # first bar this meter applies to (1-based)
    numerator: int
    denominator: int

    @property
    def bar_beats(self) -> float:
        """Length of one bar in quarter-note beats."""
        return self.numerator * 4.0 / self.denominator


@dataclass(frozen=True, slots=True)
class KeyChange:
    beat: float
    fifths: int          # -7..7, negative = flats
    mode: str = "major"  # "major" | "minor"


@dataclass(frozen=True, slots=True)
class _MeterSegment:
    first_bar: int
    start_beat: float
    bar_beats: float
    numerator: int
    denominator: int


class Timeline:
    """Immutable tempo/meter/key maps with O(log n) lookups."""

    __slots__ = (
        "tempos", "meters", "keys", "pickup_beats",
        "_tempo_beats", "_tempo_seconds", "_segments", "_segment_beats",
        "_segment_bars", "_key_beats",
    )

    def __init__(
        self,
        tempos: list[TempoChange] | tuple[TempoChange, ...] = (),
        meters: list[MeterChange] | tuple[MeterChange, ...] = (),
        keys: list[KeyChange] | tuple[KeyChange, ...] = (),
        pickup_beats: float = 0.0,
    ):
        if len(tempos) > MAX_TEMPO_CHANGES:
            raise TimelineError("too many tempo changes")
        if len(meters) > MAX_METER_CHANGES:
            raise TimelineError("too many meter changes")
        if len(keys) > MAX_KEY_CHANGES:
            raise TimelineError("too many key changes")

        self.tempos = self._clean_tempos(tempos)
        self.meters = self._clean_meters(meters)
        self.keys = self._clean_keys(keys)

        first_bar_beats = self.meters[0].bar_beats
        if pickup_beats < 0 or pickup_beats >= first_bar_beats:
            # A "pickup" as long as a full bar is just a bar.
            pickup_beats = 0.0
        self.pickup_beats = float(pickup_beats)

        # Tempo lookup: cumulative seconds at each tempo change.
        self._tempo_beats = [t.beat for t in self.tempos]
        seconds = [0.0]
        for prev, cur in zip(self.tempos, self.tempos[1:], strict=False):
            seconds.append(seconds[-1] + (cur.beat - prev.beat) * 60.0 / prev.bpm)
        self._tempo_seconds = seconds

        # Meter lookup: contiguous runs of equal-length bars. With a pickup,
        # bar 1 is its own short segment and the first meter's full bars begin
        # at bar 2.
        changes: dict[int, MeterChange] = {}
        first_full_bar = 2 if self.pickup_beats > 0 else 1
        for meter in self.meters:
            changes[max(meter.bar, first_full_bar)] = meter  # later wins

        segments: list[_MeterSegment] = []
        if self.pickup_beats > 0:
            m0 = self.meters[0]
            segments.append(
                _MeterSegment(1, 0.0, self.pickup_beats, m0.numerator, m0.denominator)
            )
        for bar in sorted(changes):
            meter = changes[bar]
            if segments:
                prev = segments[-1]
                beat = prev.start_beat + (bar - prev.first_bar) * prev.bar_beats
            else:
                beat = 0.0
            segments.append(
                _MeterSegment(bar, beat, meter.bar_beats, meter.numerator, meter.denominator)
            )
        self._segments = segments
        self._segment_beats = [s.start_beat for s in segments]
        self._segment_bars = [s.first_bar for s in segments]
        self._key_beats = [k.beat for k in self.keys]

    # --- construction helpers -------------------------------------------

    @staticmethod
    def _clean_tempos(tempos) -> tuple[TempoChange, ...]:
        cleaned: dict[float, float] = {}
        for t in tempos:
            bpm = float(t.bpm)
            beat = max(0.0, float(t.beat))
            if not (bpm == bpm) or bpm <= 0:  # NaN or non-positive
                continue
            cleaned[beat] = min(max(bpm, MIN_BPM), MAX_BPM)  # later wins
        if 0.0 not in cleaned:
            first = cleaned[min(cleaned)] if cleaned else 120.0
            # A file whose first tempo event arrives late still has *some*
            # tempo before it. MIDI says 120; if the file only ever states one
            # tempo, that one is the better guess.
            cleaned[0.0] = first if len(cleaned) == 1 else 120.0
        return tuple(TempoChange(b, cleaned[b]) for b in sorted(cleaned))

    @staticmethod
    def _clean_meters(meters) -> tuple[MeterChange, ...]:
        cleaned: dict[int, MeterChange] = {}
        for m in meters:
            num, den = int(m.numerator), int(m.denominator)
            if num < 1 or num > 64 or den not in (1, 2, 4, 8, 16, 32, 64):
                continue
            bar = max(1, int(m.bar))
            cleaned[bar] = MeterChange(bar, num, den)
        if not cleaned:
            cleaned[1] = MeterChange(1, 4, 4)
        first = min(cleaned)
        if first != 1:
            cleaned[1] = MeterChange(1, cleaned[first].numerator, cleaned[first].denominator)
        ordered = [cleaned[b] for b in sorted(cleaned)]
        # Drop no-op changes so segments stay minimal.
        out = [ordered[0]]
        for m in ordered[1:]:
            if (m.numerator, m.denominator) != (out[-1].numerator, out[-1].denominator):
                out.append(m)
        return tuple(out)

    @staticmethod
    def _clean_keys(keys) -> tuple[KeyChange, ...]:
        cleaned: dict[float, KeyChange] = {}
        for k in keys:
            fifths = int(k.fifths)
            if not -7 <= fifths <= 7:
                continue
            mode = k.mode if k.mode in ("major", "minor") else "major"
            beat = max(0.0, float(k.beat))
            cleaned[beat] = KeyChange(beat, fifths, mode)
        return tuple(cleaned[b] for b in sorted(cleaned))

    @classmethod
    def constant(
        cls, bpm: float = 120.0, numerator: int = 4, denominator: int = 4
    ) -> "Timeline":
        return cls([TempoChange(0.0, bpm)], [MeterChange(1, numerator, denominator)])

    # --- tempo: beats <-> seconds ---------------------------------------

    def seconds_at(self, beat: float) -> float:
        i = max(0, bisect_right(self._tempo_beats, beat) - 1)
        return self._tempo_seconds[i] + (beat - self._tempo_beats[i]) * 60.0 / self.tempos[i].bpm

    def beat_at(self, seconds: float) -> float:
        i = max(0, bisect_right(self._tempo_seconds, seconds) - 1)
        return self._tempo_beats[i] + (seconds - self._tempo_seconds[i]) * self.tempos[i].bpm / 60.0

    def bpm_at(self, beat: float) -> float:
        i = max(0, bisect_right(self._tempo_beats, beat) - 1)
        return self.tempos[i].bpm

    @property
    def initial_bpm(self) -> float:
        return self.tempos[0].bpm

    @property
    def has_tempo_changes(self) -> bool:
        return len(self.tempos) > 1

    # --- meter: beats <-> bars ------------------------------------------

    def _segment_for_beat(self, beat: float) -> _MeterSegment:
        return self._segments[max(0, bisect_right(self._segment_beats, beat) - 1)]

    def _segment_for_bar(self, bar: int) -> _MeterSegment:
        return self._segments[max(0, bisect_right(self._segment_bars, bar) - 1)]

    def bar_at(self, beat: float, eps: float = 1e-6) -> int:
        """Bar number containing `beat`.

        `eps` absorbs float fuzz so a note written exactly on a barline lands
        in the bar it begins, not the tail of the one before.
        """
        i = max(0, bisect_right(self._segment_beats, beat + eps) - 1)
        seg = self._segments[i]
        if seg.bar_beats <= 0:
            return seg.first_bar
        index = int((beat + eps - seg.start_beat) // seg.bar_beats)
        # The last bar of a segment is the one before the next segment's first.
        if i + 1 < len(self._segments):
            index = min(index, self._segments[i + 1].first_bar - seg.first_bar - 1)
        return seg.first_bar + max(0, index)

    def bar_start(self, bar: int) -> float:
        """Beat at which `bar` begins."""
        bar = max(1, bar)
        seg = self._segment_for_bar(bar)
        return seg.start_beat + (bar - seg.first_bar) * seg.bar_beats

    def bar_beats(self, bar: int) -> float:
        """Length of `bar` in quarter-note beats."""
        return self._segment_for_bar(max(1, bar)).bar_beats

    def meter_at_bar(self, bar: int) -> tuple[int, int]:
        seg = self._segment_for_bar(max(1, bar))
        return seg.numerator, seg.denominator

    def position(self, beat: float) -> tuple[int, float]:
        """(bar, beat offset within that bar)."""
        bar = self.bar_at(beat)
        return bar, beat - self.bar_start(bar)

    def bar_seconds(self, bar: int) -> tuple[float, float]:
        """(start, end) of `bar` in performed seconds."""
        start = self.bar_start(bar)
        return self.seconds_at(start), self.seconds_at(start + self.bar_beats(bar))

    @property
    def has_meter_changes(self) -> bool:
        return len(self.meters) > 1

    # --- key -------------------------------------------------------------

    def key_at(self, beat: float) -> KeyChange | None:
        if not self.keys:
            return None
        i = bisect_right(self._key_beats, beat + 1e-6) - 1
        return self.keys[max(0, i)]

    # --- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "tempos": [{"beat": t.beat, "bpm": t.bpm} for t in self.tempos],
            "meters": [
                {"bar": m.bar, "numerator": m.numerator, "denominator": m.denominator}
                for m in self.meters
            ],
            "keys": [{"beat": k.beat, "fifths": k.fifths, "mode": k.mode} for k in self.keys],
            "pickup_beats": self.pickup_beats,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Timeline":
        if not isinstance(data, dict):
            raise TimelineError("timeline must be an object")
        try:
            return cls(
                tempos=[TempoChange(float(t["beat"]), float(t["bpm"])) for t in data.get("tempos", [])],
                meters=[
                    MeterChange(int(m["bar"]), int(m["numerator"]), int(m["denominator"]))
                    for m in data.get("meters", [])
                ],
                keys=[
                    KeyChange(float(k["beat"]), int(k["fifths"]), str(k.get("mode", "major")))
                    for k in data.get("keys", [])
                ],
                pickup_beats=float(data.get("pickup_beats", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineError(f"malformed timeline: {exc}") from exc

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Timeline) and self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return (
            f"Timeline(tempos={len(self.tempos)}, meters={len(self.meters)}, "
            f"keys={len(self.keys)}, pickup_beats={self.pickup_beats})"
        )
