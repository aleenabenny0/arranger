"""Engrave a printable PDF with LilyPond.

Two halves. `to_lilypond` turns a `NotatedScore` into LilyPond source; it is a
pure function and is tested without LilyPond installed. `LilyPondEngraver`
runs the real program on that source.

Safety. LilyPond source is a programming language: a `.ly` file can run
Scheme, read files and write files. So no user-supplied LilyPond ever reaches
the binary. The source is generated here, from numbers and from two strings
(title, composer) that are escaped into string literals, where nothing is
evaluated. The subprocess gets no shell, a scrubbed environment, a private
temporary directory, a wall-clock timeout, cooperative cancellation, and a cap
on how much output it may produce.

Stdlib only; LilyPond itself is an external program.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

from ..ir import Score
from ..limits import LimitExceeded, ScoreImportError
from ..notation import NEvent, NMeasure, NotatedScore, NVoice, notate

LILYPOND_VERSION_STATEMENT = "2.24.0"
MAX_ENGRAVE_MEASURES = 1500
MAX_PDF_BYTES = 40 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120.0


class EngravingError(ScoreImportError):
    code = "engraving_failed"


class EngraverUnavailable(EngravingError):
    code = "engraver_unavailable"


class EngravingCancelled(EngravingError):
    code = "engraving_cancelled"


class EngravingTimeout(EngravingError):
    code = "engraving_timeout"


# --- source generation -----------------------------------------------------

_TYPE_NUMBER = {
    "breve": "\\breve", "whole": "1", "half": "2", "quarter": "4", "eighth": "8",
    "16th": "16", "32nd": "32", "64th": "64",
}
_ARTICULATION = {
    "staccato": "-.", "staccatissimo": "-!", "accent": "->", "marcato": "-^",
    "tenuto": "--", "fermata": "\\fermata",
}
_DYNAMICS = {"ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "fp"}
_MAJOR_TONIC = {
    -7: ("c", -1), -6: ("g", -1), -5: ("d", -1), -4: ("a", -1), -3: ("e", -1), -2: ("b", -1),
    -1: ("f", 0), 0: ("c", 0), 1: ("g", 0), 2: ("d", 0), 3: ("a", 0), 4: ("e", 0), 5: ("b", 0),
    6: ("f", 1), 7: ("c", 1),
}
_MINOR_TONIC = {
    -7: ("a", -1), -6: ("e", -1), -5: ("b", -1), -4: ("f", 0), -3: ("c", 0), -2: ("g", 0),
    -1: ("d", 0), 0: ("a", 0), 1: ("e", 0), 2: ("b", 0), 3: ("f", 1), 4: ("c", 1), 5: ("g", 1),
    6: ("d", 1), 7: ("a", 1),
}


def _name(step: str, alter: int) -> str:
    suffix = {-2: "eses", -1: "es", 0: "", 1: "is", 2: "isis"}.get(alter, "")
    return step.lower() + suffix


def _pitch(step: str, alter: int, octave: int) -> str:
    marks = octave - 3
    return _name(step, alter) + ("'" * marks if marks > 0 else "," * -marks)


def quote(text: str) -> str:
    """A LilyPond string literal. Nothing inside one is evaluated."""
    cleaned = "".join(ch for ch in text if ch.isprintable())[:200]
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _duration(ev: NEvent) -> str:
    return _TYPE_NUMBER.get(ev.note_type, "4") + "." * ev.dots


def _multiplier(length: Fraction) -> str:
    whole = length / 4
    return f"1*{whole.numerator}/{whole.denominator}"


def _event(ev: NEvent, m: NMeasure, marks: str) -> str:
    if ev.whole_bar_rest:
        return f"R{_multiplier(m.length)}"
    if ev.is_rest:
        return ("s" if ev.hidden else "r") + _duration(ev)
    if len(ev.pitches) == 1:
        p = ev.pitches[0]
        body = _pitch(p.step, p.alter, p.octave) + _duration(ev) + ("~" if p.tie_start else "")
    else:
        inner = " ".join(
            _pitch(p.step, p.alter, p.octave) + ("~" if p.tie_start else "") for p in ev.pitches
        )
        body = f"<{inner}>{_duration(ev)}"
    body += "".join(_ARTICULATION[a] for a in ev.articulations if a in _ARTICULATION)
    if ev.rolled and len(ev.pitches) > 1:
        body += "\\arpeggio"
    if ev.dynamic in _DYNAMICS:
        body += f"\\{ev.dynamic}"
    return body + marks


def _voice(
    voice: NVoice, m: NMeasure, pedal_marks: dict[Fraction, str], flush: bool = False
) -> str:
    """Render one voice. Pedal marks due at or before a note are hung on it.

    With `flush`, whatever is still pending when the last note of the bar is
    reached goes on that note. It is used for the final bar, where a release
    that falls on the closing barline has no later note to sit on.
    """
    last_note = max((i for i, ev in enumerate(voice.events) if not ev.is_rest), default=-1)
    parts: list[str] = []
    for index, ev in enumerate(voice.events):
        marks = ""
        if pedal_marks and not ev.is_rest:
            everything = flush and index == last_note
            due = [offset for offset in pedal_marks if everything or offset <= ev.start]
            for offset in sorted(due):
                marks += pedal_marks.pop(offset)
        if ev.tuplet_start and ev.tuplet:
            parts.append(f"\\tuplet {ev.tuplet[0]}/{ev.tuplet[1]} {{")
        parts.append(_event(ev, m, marks))
        if ev.tuplet_stop and ev.tuplet:
            parts.append("}")
    return " ".join(parts)


def _partial(length: Fraction) -> str:
    sixty_fourths = length * 16
    if sixty_fourths.denominator != 1:
        return ""
    return f"\\partial 64*{sixty_fourths.numerator} "


def _staff(notated: NotatedScore, staff: int) -> str:
    lines: list[str] = []
    pedal_down = False
    carried = ""
    for m in notated.measures:
        prefix = ""
        if m.show_key or m.number == notated.measures[0].number:
            tonic, alter = (_MINOR_TONIC if m.mode == "minor" else _MAJOR_TONIC)[m.fifths]
            prefix += f"\\key {_name(tonic, alter)} \\{m.mode} "
        if m.show_time or m.number == notated.measures[0].number:
            prefix += f"\\time {m.numerator}/{m.denominator} "
        if m.is_pickup:
            prefix += _partial(m.length)
        if staff == 1 and m.tempo_bpm is not None:
            prefix += f"\\tempo 4 = {round(m.tempo_bpm)} "

        pedal_marks: dict[Fraction, str] = {}
        if staff == 2:
            if carried:
                pedal_marks[Fraction(0)] = carried
                carried = ""
            changes = sorted(
                [(o, "off") for o in m.pedal_stops] + [(o, "on") for o in m.pedal_starts],
                key=lambda c: (c[0], c[1] == "on"),
            )
            for offset, kind in changes:
                if kind == "on" and not pedal_down:
                    pedal_marks[offset] = pedal_marks.get(offset, "") + "\\sustainOn"
                    pedal_down = True
                elif kind == "off" and pedal_down:
                    pedal_marks[offset] = pedal_marks.get(offset, "") + "\\sustainOff"
                    pedal_down = False

        final = m is notated.measures[-1]
        voices = m.staves.get(staff, [])
        if len(voices) <= 1:
            body = _voice(voices[0], m, pedal_marks, final) if voices else f"R{_multiplier(m.length)}"
        else:
            rendered = [_voice(voices[0], m, pedal_marks, final)]
            rendered += [_voice(v, m, {}) for v in voices[1:]]
            body = "<< { " + " } \\\\ { ".join(rendered) + " } >>"
        # A mark with no later note in this bar (a release on the barline, or a
        # bar of rests) moves to the first note of the next bar.
        carried = "".join(pedal_marks[o] for o in sorted(pedal_marks))
        lines.append(f"  {prefix}{body} | % {m.number}")
    return "\n".join(lines)


def to_lilypond(notated: NotatedScore, *, paper: str = "a4") -> str:
    """LilyPond source for a two-staff piano score."""
    if paper not in ("a4", "letter"):
        paper = "a4"
    if len(notated.measures) > MAX_ENGRAVE_MEASURES:
        raise LimitExceeded(
            "This piece is too long to engrave as one PDF.",
            detail=f"{len(notated.measures)} > {MAX_ENGRAVE_MEASURES} measures",
        )
    header = [f"  title = {quote(notated.title or 'untitled')}"]
    if notated.composer:
        header.append(f"  composer = {quote(notated.composer)}")
    header.append("  tagline = ##f")
    return "\n".join(
        [
            f'\\version "{LILYPOND_VERSION_STATEMENT}"',
            f'#(set-default-paper-size "{paper}")',
            "\\header {", *header, "}",
            "upper = {", "  \\clef treble", _staff(notated, 1), '  \\bar "|."', "}",
            "lower = {", "  \\clef bass", "  \\set Staff.pedalSustainStyle = #'bracket",
            _staff(notated, 2), '  \\bar "|."', "}",
            "\\score {",
            '  \\new PianoStaff \\with { instrumentName = "" } <<',
            '    \\new Staff = "upper" \\upper',
            '    \\new Staff = "lower" \\lower',
            "  >>",
            "  \\layout { }",
            "}",
            "",
        ]
    )


# --- running LilyPond ------------------------------------------------------


@dataclass(frozen=True)
class EngraverStatus:
    available: bool
    executable: str | None
    version: str | None
    detail: str


def find_lilypond(explicit: str | None = None) -> str | None:
    """Locate the binary: explicit path, then LILYPOND_PATH, then PATH, then the dev cache."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("LILYPOND_PATH"):
        candidates.append(os.environ["LILYPOND_PATH"])
    on_path = shutil.which("lilypond")
    if on_path:
        candidates.append(on_path)
    root = Path(__file__).resolve().parents[3]
    exe = "lilypond.exe" if sys.platform == "win32" else "lilypond"
    candidates.extend(str(p) for p in sorted(root.glob(f".cache/tools/lilypond-*/bin/{exe}"), reverse=True))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def _child_env(workdir: Path) -> dict[str, str]:
    """Only what LilyPond needs. No application secrets leak into the child."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "FONTCONFIG_PATH",
            "FONTCONFIG_FILE", "XDG_CACHE_HOME")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["HOME"] = str(workdir)
    env.setdefault("LANG", "C.UTF-8")
    return env


def _limit_resources() -> None:  # pragma: no cover - POSIX only, runs in the child
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PDF_BYTES * 2, MAX_PDF_BYTES * 2))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    except (ValueError, OSError):
        pass


class LilyPondEngraver:
    """Runs LilyPond. Construct once; `status()` is cheap after the first call."""

    def __init__(self, executable: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self._explicit = executable
        self.timeout = timeout
        self._status: EngraverStatus | None = None

    def status(self) -> EngraverStatus:
        if self._status is not None:
            return self._status
        exe = find_lilypond(self._explicit)
        if exe is None:
            self._status = EngraverStatus(
                False, None, None,
                "LilyPond was not found. Install it (https://lilypond.org) or set LILYPOND_PATH.",
            )
            return self._status
        try:
            done = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=20, check=False,
                env=_child_env(Path(tempfile.gettempdir())),
            )
            first = (done.stdout or done.stderr).splitlines()[0] if (done.stdout or done.stderr) else ""
            version = first.replace("GNU LilyPond", "").strip() or None
            ok = done.returncode == 0 and version is not None
            self._status = EngraverStatus(ok, exe, version, "ok" if ok else "LilyPond did not run.")
        except (OSError, subprocess.SubprocessError) as exc:
            self._status = EngraverStatus(False, exe, None, f"LilyPond did not run: {type(exc).__name__}")
        return self._status

    def engrave_source(
        self, source: str, *, should_cancel: Callable[[], bool] | None = None
    ) -> bytes:
        status = self.status()
        if not status.available or status.executable is None:
            raise EngraverUnavailable("PDF engraving is not available on this server.", detail=status.detail)

        with tempfile.TemporaryDirectory(prefix="arranger-ly-") as tmp:
            workdir = Path(tmp)
            (workdir / "score.ly").write_text(source, encoding="utf-8")
            command = [
                status.executable, "--pdf", "--loglevel=ERROR", "-dno-point-and-click",
                "-o", "score", "score.ly",
            ]
            kwargs: dict = {}
            if sys.platform != "win32":
                kwargs["preexec_fn"] = _limit_resources
                kwargs["start_new_session"] = True
            else:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            log_path = workdir / "lilypond.log"
            with open(log_path, "wb") as log:
                process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    command, cwd=workdir, env=_child_env(workdir), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, **kwargs,
                )
                deadline = time.monotonic() + self.timeout
                try:
                    while process.poll() is None:
                        if should_cancel is not None and should_cancel():
                            raise EngravingCancelled("Engraving was cancelled.")
                        if time.monotonic() > deadline:
                            raise EngravingTimeout(
                                "Engraving took too long and was stopped.",
                                detail=f"timeout={self.timeout}s",
                            )
                        time.sleep(0.05)
                finally:
                    if process.poll() is None:
                        process.kill()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:  # pragma: no cover
                            pass

            output = workdir / "score.pdf"
            log_text = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            if process.returncode != 0 or not output.is_file():
                raise EngravingError(
                    "The score could not be engraved.",
                    detail=f"exit={process.returncode} log={log_text}",
                )
            size = output.stat().st_size
            if size > MAX_PDF_BYTES:
                raise LimitExceeded("The engraved PDF is too large.", detail=f"{size} bytes")
            data = output.read_bytes()
        if not data.startswith(b"%PDF"):
            raise EngravingError("The score could not be engraved.", detail="output is not a PDF")
        return data

    def engrave(
        self,
        notated: NotatedScore,
        *,
        paper: str = "a4",
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        return self.engrave_source(to_lilypond(notated, paper=paper), should_cancel=should_cancel)

    def engrave_score(
        self, score: Score, *, paper: str = "a4", should_cancel: Callable[[], bool] | None = None
    ) -> tuple[bytes, list[str]]:
        notated = notate(score)
        return self.engrave(notated, paper=paper, should_cancel=should_cancel), notated.warnings
