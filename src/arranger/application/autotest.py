"""Find licensed music online and test the arranger against it, unattended.

    arranger-autotest                                   # the corpus, against the committed baseline
    arranger-autotest --discover mutopia --page 7 --limit 10
    arranger-autotest --files some/*.mid --snippets 16 --profiles preset:beginner,profiles/me.json

For every piece, whole and in snippets of a few bars:

1. **read it** with the bounded reader. A refusal is a recorded result, never
   a crash; a crash is a finding.
2. **check the invariants** the rest of the system relies on: the MIDI
   writer round-trips the piece, the score JSON does too, the engine's plan
   validates against the piece, the arrangement re-imports from MusicXML with
   the same notes, the verifier gives the same answer twice, and the melody
   is kept (CLAUDE.md: recall under 0.9 is a failed arrangement). On
   snippets the engine is also run twice and must produce the same plan.
3. **hold the contest**: brute force over every whole-piece figure against
   the deterministic engine, and against the model when one is given, judged
   on `engine.quality_key`. The engine losing is a bug.
4. **compare with the committed baseline** (`evals/autotest/baseline.json`):
   a piece the engine used to win and now ties, a cost that went up, an
   invariant that used to hold, are regressions and fail the run.

Snippets exist because a hard passage inside an easy piece is invisible in
the whole-piece numbers, and because a 16-bar window is cheap enough to run
every check on. Pieces and snippets that brute force cannot fully solve are
listed as candidates for the evaluation pool, where the model has something
to prove.

Nothing here commits music to the repository. Discovered files are cached
under a git-ignored directory with their provenance in `index.json`, and the
report records title, composer, licence and source page for each.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters.midi_writer import write_midi
from ..adapters.musicxml_reader import read_musicxml_bytes
from ..adapters.musicxml_writer import write_musicxml
from ..adapters.mutopia import CatalogueError, MutopiaCatalogue, PAGE_SIZE
from ..adapters.score_json import score_from_dict, score_to_dict
from ..agent import arrange, brute_force_candidate
from ..engine import ALGORITHM_VERSION, Candidate, arrange_deterministic
from ..evaluate import Contestant, outcome
from ..io import read_midi_bytes
from ..ir import PedalSpan, Score
from ..limits import DEFAULT_LIMITS, ImportLimits, ScoreImportError
from ..profile import PRESETS, PlayerProfile
from ..render import last_bar
from ..timeline import MeterChange, TempoChange, Timeline
from ..verify import verify

ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = ROOT / "evals" / "corpus"
DISCOVERED_DIR = ROOT / "evals" / "discovered"
BASELINE_PATH = ROOT / "evals" / "autotest" / "baseline.json"

MELODY_RECALL_FLOOR = 0.9
ONSET_TOLERANCE_SECONDS = 0.02


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- what one run produces -------------------------------------------------


@dataclass
class CaseSpec:
    """Where a piece came from. Everything the report needs to credit it."""

    id: str
    title: str
    origin: str                 # "corpus" | "discovered" | "file"
    sha256: str = ""
    license: str = ""
    source_url: str = ""
    composer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Invariant:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Contest:
    profile: str
    baseline: Contestant
    engine: Contestant
    on_cost: str
    on_quality: str
    model: Contestant | None = None
    model_on_cost: str | None = None
    model_on_quality: str | None = None

    @property
    def engine_lost(self) -> bool:
        return self.on_quality == "loss"

    @property
    def resists_brute_force(self) -> bool:
        return self.baseline.cost > 0


@dataclass
class CaseResult:
    spec: CaseSpec
    notes: int = 0
    bars: int = 0
    seconds: float = 0.0
    error: str | None = None
    invariants: list[Invariant] = field(default_factory=list)
    contests: list[Contest] = field(default_factory=list)
    snippets: list["CaseResult"] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        """The bounded reader declined the file with a typed error: a result, not a failure."""
        return bool(self.error) and self.error.startswith("refused:")

    def failures(self) -> list[str]:
        """Everything about this case that should fail a run."""
        problems = []
        if self.error and not self.refused:
            problems.append(f"{self.spec.id}: {self.error}")
        problems += [f"{self.spec.id}: invariant {i.name} failed: {i.detail}" for i in self.invariants if not i.ok]
        problems += [
            f"{self.spec.id}: engine lost to brute force on quality for {c.profile} "
            f"(engine {c.engine.plan} {c.engine.cost:.2f}/{c.engine.strain}/{c.engine.fidelity:.3f} vs "
            f"brute force {c.baseline.plan} {c.baseline.cost:.2f}/{c.baseline.strain}/{c.baseline.fidelity:.3f})"
            for c in self.contests if c.engine_lost
        ]
        for snippet in self.snippets:
            problems += snippet.failures()
        return problems

    @property
    def ok(self) -> bool:
        return not self.failures()

    def to_dict(self) -> dict:
        return {
            "spec": self.spec.to_dict(), "notes": self.notes, "bars": self.bars, "seconds": self.seconds,
            "error": self.error,
            "invariants": [asdict(i) for i in self.invariants],
            "contests": [_contest_to_dict(c) for c in self.contests],
            "snippets": [s.to_dict() for s in self.snippets],
        }


def _contest_to_dict(contest: Contest) -> dict:
    return {
        "profile": contest.profile,
        "baseline": asdict(contest.baseline), "engine": asdict(contest.engine),
        "on_cost": contest.on_cost, "on_quality": contest.on_quality,
        "model": asdict(contest.model) if contest.model else None,
        "model_on_cost": contest.model_on_cost, "model_on_quality": contest.model_on_quality,
    }


# --- reading and cutting ----------------------------------------------------


def read_case(data: bytes, filename: str, *, limits: ImportLimits = DEFAULT_LIMITS) -> Score:
    """Whichever bounded reader the bytes call for. Raises ScoreImportError."""
    if data[:4] == b"MThd":
        return read_midi_bytes(data, filename=filename, limits=limits)
    return read_musicxml_bytes(data, filename=filename, limits=limits)


def snippet(score: Score, start_bar: int, end_bar: int) -> Score:
    """Bars `start_bar`..`end_bar` of `score` as a piece of its own, starting at bar 1.

    Notes that begin inside the window are kept, clipped to the window's end.
    With a timeline the cut is exact and tempo and meter changes inside the
    window are carried over; without one, the notes' own bar numbers decide
    membership and the earliest onset in the window becomes time zero.
    """
    if start_bar < 1 or end_bar < start_bar:
        raise ValueError("a snippet needs 1 <= start_bar <= end_bar")
    timeline = score.timeline
    if timeline is not None:
        t0 = timeline.seconds_at(timeline.bar_start(start_bar))
        t1 = timeline.seconds_at(timeline.bar_start(end_bar + 1))
        b0 = timeline.bar_start(start_bar)
        chosen = [n for n in score.notes if t0 - 1e-6 <= n.onset < t1 - 1e-6]
    else:
        t1 = None
        chosen = [n for n in score.notes if n.bar is not None and start_bar <= n.bar <= end_bar]
        t0 = min((n.onset for n in chosen), default=0.0)
        b0 = None
    shift = start_bar - 1
    notes = []
    for n in chosen:
        duration = n.duration if t1 is None else max(1e-3, min(n.duration, t1 - n.onset))
        notes.append(_replace(
            n, onset=round(n.onset - t0, 6), duration=round(duration, 6),
            bar=None if n.bar is None else n.bar - shift,
            beat=None if n.beat is None or b0 is None else round(n.beat - b0, 6),
        ))
    pedals = []
    end_time = t1 if t1 is not None else max((n.offset for n in notes), default=0.0) + t0
    for p in score.pedals:
        if p.end <= t0 or p.start >= end_time:
            continue
        pedals.append(PedalSpan(max(0.0, p.start - t0), min(end_time, p.end) - t0))
    new_timeline = None
    if timeline is not None:
        tempos = [TempoChange(0.0, timeline.bpm_at(b0))]
        tempos += [TempoChange(t.beat - b0, t.bpm) for t in timeline.tempos
                   if b0 < t.beat < timeline.bar_start(end_bar + 1)]
        num, den = timeline.meter_at_bar(start_bar)
        meters = [MeterChange(1, num, den)]
        meters += [MeterChange(m.bar - shift, m.numerator, m.denominator) for m in timeline.meters
                   if start_bar < m.bar <= end_bar]
        new_timeline = Timeline(tempos, meters)
    return Score(
        notes=notes, tempo_bpm=score.tempo_bpm if new_timeline is None else new_timeline.initial_bpm,
        title=f"{score.title} bars {start_bar}-{end_bar}", timeline=new_timeline, pedals=pedals,
        tracks=list(score.tracks), composer=score.composer, source_format=score.source_format,
    )


def _replace(note, **changes):
    from dataclasses import replace

    return replace(note, **changes)


def snippet_windows(score: Score, *, bars: int = 16, max_count: int = 3) -> list[tuple[int, int]]:
    """Evenly spaced windows of `bars` bars; none when the piece is not longer than one."""
    total = last_bar(score)
    if bars <= 0 or max_count <= 0 or total <= bars:
        return []
    count = min(max_count, total // bars)
    if count <= 1:
        return [(1, bars)]
    windows = []
    for i in range(count):
        start = 1 + round(i * (total - bars) / (count - 1))
        window = (start, start + bars - 1)
        if window not in windows:
            windows.append(window)
    return windows


# --- the checks -------------------------------------------------------------


def _pitches(score: Score) -> list[int]:
    return sorted(n.pitch for n in score.notes)


_MERGED = re.compile(r"(\d+) repeated note\(s\) fell on the same position")


def declared_merges(warnings: list[str]) -> int:
    """How many notes the notation step says it merged into a neighbour."""
    return sum(int(m.group(1)) for m in (_MERGED.search(w) for w in warnings) if m)


def check_invariants(
    source: Score, candidate: Candidate, profile: PlayerProfile, *, reproducibility: bool = False,
) -> list[Invariant]:
    """The properties every piece must satisfy, on the real thing."""
    results: list[Invariant] = []

    try:
        back = read_midi_bytes(write_midi(source))
        same_pitches = _pitches(back) == _pitches(source)
        drift = 0.0
        if same_pitches:
            ours = sorted(source.notes, key=lambda n: (n.onset, n.pitch))
            theirs = sorted(back.notes, key=lambda n: (n.onset, n.pitch))
            drift = max((abs(a.onset - b.onset) for a, b in zip(ours, theirs, strict=True)), default=0.0)
        ok = same_pitches and drift <= ONSET_TOLERANCE_SECONDS
        results.append(Invariant(
            "midi_round_trip", ok,
            f"{len(source.notes)} notes -> {len(back.notes)}, max onset drift {drift * 1000:.1f} ms",
        ))
    except Exception as exc:  # noqa: BLE001 - a crash here is the finding
        results.append(Invariant("midi_round_trip", False, f"{type(exc).__name__}: {exc}"[:300]))

    try:
        again = score_from_dict(score_to_dict(source))
        ok = _pitches(again) == _pitches(source) and len(again.notes) == len(source.notes)
        results.append(Invariant("score_json_round_trip", ok, f"{len(again.notes)} notes"))
    except Exception as exc:  # noqa: BLE001
        results.append(Invariant("score_json_round_trip", False, f"{type(exc).__name__}: {exc}"[:300]))

    problems = candidate.plan.validate_for_source(last_bar(source), skill_level=profile.skill_level)
    results.append(Invariant("plan_valid_for_source", not problems, "; ".join(problems)[:300]))

    try:
        xml, warnings = write_musicxml(candidate.arranged)
        reimported = read_musicxml_bytes(xml, filename="arranged.musicxml")
        ours = Counter((n.pitch, n.staff) for n in candidate.arranged.notes)
        theirs = Counter((n.pitch, n.staff) for n in reimported.notes)
        missing = sum((ours - theirs).values())
        extra = sum((theirs - ours).values())
        # Notation is allowed to lose a note only when it says so: a repeated
        # note that quantisation put on its neighbour's position is declared
        # in the writer's warnings. An undeclared loss, or a note from
        # nowhere, is a bug in a writer or a reader.
        declared = declared_merges(warnings)
        ok = extra == 0 and missing <= declared
        results.append(Invariant(
            "musicxml_round_trip", ok,
            f"{len(candidate.arranged.notes)} notes -> {len(reimported.notes)}; {missing} missing, "
            f"{declared} declared merged, {extra} extra; {len(warnings)} writer warning(s)"
            + (f"; first: {warnings[0]}" if warnings and not ok else ""),
        ))
    except Exception as exc:  # noqa: BLE001
        results.append(Invariant("musicxml_round_trip", False, f"{type(exc).__name__}: {exc}"[:300]))

    first = verify(candidate.arranged, profile)
    second = verify(candidate.arranged, profile)
    if "unknown" in (str(first.solver_status), str(second.solver_status)):
        results.append(Invariant("verify_deterministic", True, "search cut short; not compared"))
    else:
        same = first.summary() == second.summary() and first.playable == second.playable
        results.append(Invariant("verify_deterministic", same, f"{first.summary()} vs {second.summary()}" if not same else ""))

    if candidate.accepted:
        recall = candidate.fidelity.melodic_recall
        results.append(Invariant("melody_kept", recall >= MELODY_RECALL_FLOOR, f"melodic recall {recall:.3f}"))
    else:
        results.append(Invariant("melody_kept", True, "arrangement not accepted; not judged"))

    if reproducibility:
        repeat = arrange_deterministic(source, profile)
        same = repeat.plan.to_json() == candidate.plan.to_json()
        results.append(Invariant("engine_reproducible", same, "" if same else "a second run produced a different plan"))

    return results


def contest(
    source: Score,
    profile: PlayerProfile,
    *,
    deadline_seconds: float | None = None,
    legal_only: bool = True,
    model_factory: Callable[[], object] | None = None,
    attempts: int = 4,
) -> tuple[Contest, Candidate]:
    """Brute force vs the engine (vs the model) on one piece for one profile."""
    started = time.monotonic()
    deadline = None if deadline_seconds is None else started + deadline_seconds
    baseline = brute_force_candidate(source, profile, legal_only=legal_only, deadline=deadline)
    baseline_side = Contestant.from_candidate(baseline, time.monotonic() - started)

    started = time.monotonic()
    deadline = None if deadline_seconds is None else started + deadline_seconds
    engine = arrange_deterministic(source, profile, deadline=deadline)
    engine_side = Contestant.from_candidate(engine, time.monotonic() - started)
    on_cost, on_quality = outcome(engine_side, baseline_side)
    result = Contest(profile.name, baseline_side, engine_side, on_cost, on_quality)

    if model_factory is not None:
        run = arrange(source, profile, model_factory(), max_attempts=attempts, verbose=False)
        result.model = Contestant.from_run(run)
        result.model_on_cost, result.model_on_quality = outcome(result.model, baseline_side)
    return result, engine


def test_piece(
    spec: CaseSpec,
    data: bytes,
    profiles: list[PlayerProfile],
    *,
    snippet_bars: int = 16,
    max_snippets: int = 3,
    deadline_seconds: float | None = None,
    legal_only: bool = True,
    model_factory: Callable[[], object] | None = None,
    attempts: int = 4,
    limits: ImportLimits = DEFAULT_LIMITS,
    check_reproducibility: bool = False,
    log: Callable[[str], None] | None = None,
) -> CaseResult:
    """Everything above, on one piece and its snippets."""
    say = log or (lambda _line: None)
    started = time.monotonic()
    result = CaseResult(spec=spec)
    if not spec.sha256:
        spec.sha256 = sha256_of(data)
    try:
        source = read_case(data, spec.id, limits=limits)
    except ScoreImportError as exc:
        result.error = f"refused: {exc.code}: {exc.public}"
        result.seconds = round(time.monotonic() - started, 2)
        say(f"{spec.id}: {result.error}")
        return result
    except Exception as exc:  # noqa: BLE001 - the reader crashing on a real file is the finding
        result.error = f"reader crashed: {type(exc).__name__}: {exc}"[:300]
        result.seconds = round(time.monotonic() - started, 2)
        say(f"{spec.id}: {result.error}")
        return result

    result.notes, result.bars = len(source.notes), last_bar(source)
    say(f"{spec.id}: {result.notes} notes, {result.bars} bars")
    for profile in profiles:
        try:
            held, engine = contest(
                source, profile, deadline_seconds=deadline_seconds, legal_only=legal_only,
                model_factory=model_factory, attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            result.error = f"contest crashed for {profile.name}: {type(exc).__name__}: {exc}"[:300]
            say(f"  {result.error}")
            continue
        result.contests.append(held)
        if profile is profiles[0]:
            result.invariants = check_invariants(source, engine, profile, reproducibility=check_reproducibility)
        say(f"  {profile.name}: engine {held.engine.plan} {held.engine.cost:.2f}/{held.engine.strain}/"
            f"{held.engine.fidelity:.3f} vs brute force {held.baseline.plan} {held.baseline.cost:.2f}/"
            f"{held.baseline.strain}/{held.baseline.fidelity:.3f}: {held.on_quality} on quality")
    for name, detail in ((i.name, i.detail) for i in result.invariants if not i.ok):
        say(f"  INVARIANT {name} FAILED: {detail}")

    for start, end in snippet_windows(source, bars=snippet_bars, max_count=max_snippets):
        piece = snippet(source, start, end)
        if len(piece.notes) < 4:
            continue
        sub_spec = CaseSpec(
            id=f"{spec.id}#bars-{start}-{end}", title=f"{spec.title}, bars {start}-{end}", origin=spec.origin,
            sha256=spec.sha256, license=spec.license, source_url=spec.source_url, composer=spec.composer,
        )
        sub = CaseResult(spec=sub_spec, notes=len(piece.notes), bars=last_bar(piece))
        sub_started = time.monotonic()
        for profile in profiles:
            try:
                held, engine = contest(piece, profile, deadline_seconds=deadline_seconds, legal_only=legal_only)
            except Exception as exc:  # noqa: BLE001
                sub.error = f"contest crashed for {profile.name}: {type(exc).__name__}: {exc}"[:300]
                continue
            sub.contests.append(held)
            if profile is profiles[0]:
                sub.invariants = check_invariants(piece, engine, profile, reproducibility=True)
        sub.seconds = round(time.monotonic() - sub_started, 2)
        result.snippets.append(sub)
        state = "ok" if sub.ok else "FAILED"
        say(f"  bars {start}-{end}: {state}")
    result.seconds = round(time.monotonic() - started, 2)
    return result


# --- the baseline ------------------------------------------------------------


def baseline_from_results(results: list[CaseResult]) -> dict:
    """What a later run is held to. Keyed by content hash, then by profile."""
    cases: dict[str, dict] = {}

    def record(case: CaseResult) -> None:
        if case.error or not case.spec.sha256:
            return
        key = case.spec.id if "#" in case.spec.id else case.spec.sha256
        entry = cases.setdefault(key, {"id": case.spec.id, "title": case.spec.title, "contests": {}, "invariants": {}})
        for c in case.contests:
            entry["contests"][c.profile] = {
                "engine_cost": c.engine.cost, "engine_strain": c.engine.strain,
                "engine_fidelity": c.engine.fidelity, "engine_tempo": c.engine.tempo_scale,
                "baseline_cost": c.baseline.cost, "on_quality": c.on_quality,
            }
        entry["invariants"] = {i.name: i.ok for i in case.invariants}
        for s in case.snippets:
            record(s)

    for case in results:
        record(case)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "recorded": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "cases": cases,
    }


_RANK = {"win": 0, "tie": 1, "loss": 2}


def compare_with_baseline(results: list[CaseResult], baseline: dict) -> list[str]:
    """Regressions against a recorded baseline. An unknown case is not one."""
    regressions: list[str] = []
    cases = baseline.get("cases", {})

    def check(case: CaseResult) -> None:
        if case.error:
            return
        key = case.spec.id if "#" in case.spec.id else case.spec.sha256
        recorded = cases.get(key)
        if recorded is None:
            return
        for c in case.contests:
            past = recorded.get("contests", {}).get(c.profile)
            if past is None:
                continue
            if c.engine.cost > past["engine_cost"] + 1e-9:
                regressions.append(
                    f"{case.spec.id} ({c.profile}): engine cost {c.engine.cost:.2f} was {past['engine_cost']:.2f}"
                )
            elif c.engine.cost == past["engine_cost"]:
                now = (-c.engine.tempo_scale, c.engine.strain, -c.engine.fidelity)
                then = (-past["engine_tempo"], past["engine_strain"], -round(past["engine_fidelity"], 3))
                if now > then:
                    regressions.append(
                        f"{case.spec.id} ({c.profile}): engine quality fell: tempo {c.engine.tempo_scale:.0%}, "
                        f"{c.engine.strain} strain, fidelity {c.engine.fidelity:.3f} "
                        f"(was {past['engine_tempo']:.0%}, {past['engine_strain']}, {past['engine_fidelity']:.3f})"
                    )
            if _RANK.get(c.on_quality, 2) > _RANK.get(past.get("on_quality", "win"), 0):
                regressions.append(
                    f"{case.spec.id} ({c.profile}): against brute force the engine now has a "
                    f"{c.on_quality}, it had a {past.get('on_quality')}"
                )
        for i in case.invariants:
            if recorded.get("invariants", {}).get(i.name) is True and not i.ok:
                regressions.append(f"{case.spec.id}: invariant {i.name} held before and fails now: {i.detail}")
        for s in case.snippets:
            check(s)

    for case in results:
        check(case)
    return regressions


# --- the report ----------------------------------------------------------------


def candidates_for_pool(results: list[CaseResult]) -> list[dict]:
    """Pieces and snippets brute force cannot fully solve: where a model has something to prove."""
    found = []

    def walk(case: CaseResult) -> None:
        for c in case.contests:
            if c.resists_brute_force:
                found.append({
                    "id": case.spec.id, "title": case.spec.title, "profile": c.profile,
                    "brute_force_cost": c.baseline.cost, "engine_cost": c.engine.cost,
                    "license": case.spec.license, "source_url": case.spec.source_url, "sha256": case.spec.sha256,
                })
        for s in case.snippets:
            walk(s)

    for case in results:
        walk(case)
    return found


def render_report(results: list[CaseResult], regressions: list[str], *, title: str = "Autotest") -> str:
    whole = [r for r in results]
    all_cases = []
    for r in results:
        all_cases.append(r)
        all_cases.extend(r.snippets)
    contests = [c for case in all_cases for c in case.contests]
    outcomes = [c.on_quality for c in contests]
    failures = [f for case in results for f in case.failures()]
    lines = [
        f"# {title}",
        "",
        f"Engine `{ALGORITHM_VERSION}`. {len(whole)} piece(s), {sum(len(r.snippets) for r in whole)} snippet(s), "
        f"{len(contests)} contest(s).",
        "",
        f"**Engine vs brute force on quality: {outcomes.count('win')} wins, {outcomes.count('tie')} ties, "
        f"{outcomes.count('loss')} losses.** "
        + ("The engine never lost." if contests and not outcomes.count("loss") else ""),
        "",
        f"**{len(failures)} failure(s), {len(regressions)} regression(s).**",
        "",
    ]
    if failures:
        lines += ["## Failures", ""] + [f"- {f}" for f in failures] + [""]
    if regressions:
        lines += ["## Regressions against the baseline", ""] + [f"- {r}" for r in regressions] + [""]
    lines += [
        "## Pieces",
        "",
        "| piece | source | notes | bars | profile | brute force cost / strain / fidelity (plan) | "
        "engine cost / strain / fidelity (plan) | on quality | model | checks | time |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for case in all_cases:
        provenance = case.spec.license or "-"
        if case.spec.source_url:
            provenance = f"[{provenance}]({case.spec.source_url})"
        checks = "refused" if case.error and case.error.startswith("refused") else (
            "crash" if case.error else f"{sum(1 for i in case.invariants if i.ok)}/{len(case.invariants)} ok"
        )
        if not case.contests:
            lines.append(f"| {case.spec.id} | {provenance} | {case.notes} | {case.bars} | - | - | - | - | - | {checks} | {case.seconds:.0f}s |")
            continue
        for c in case.contests:
            model = "-"
            if c.model is not None:
                model = f"{c.model.cost:.2f}/{c.model.strain}/{c.model.fidelity:.3f} ({c.model_on_quality})"
            lines.append(
                f"| {case.spec.id} | {provenance} | {case.notes} | {case.bars} | {c.profile} | "
                f"{c.baseline.cost:.2f} / {c.baseline.strain} / {c.baseline.fidelity:.3f} ({c.baseline.plan}) | "
                f"{c.engine.cost:.2f} / {c.engine.strain} / {c.engine.fidelity:.3f} ({c.engine.plan}) | "
                f"{c.on_quality} | {model} | {checks} | {case.seconds:.0f}s |"
            )
    pool = candidates_for_pool(results)
    if pool:
        lines += ["", "## Resists brute force", "", "Candidates for the evaluation pool: a uniform plan leaves a cost here.", ""]
        for item in pool:
            lines.append(
                f"- {item['id']} ({item['profile']}): brute force {item['brute_force_cost']:.2f}, "
                f"engine {item['engine_cost']:.2f}; {item['license'] or 'licence unknown'}"
            )
    lines += [
        "",
        "## Reading this",
        "",
        "Quality ranks cost, then tempo, then strain, then fidelity above the floor",
        "(`arranger.engine.quality_key`). A loss means a single whole-piece figure",
        "would have served this player better than the engine's plan: an engine bug.",
        "Invariants are properties of the code on real music; a failed one is a bug",
        "in the reader, a writer, the verifier or the renderer, with the file to",
        "reproduce it named in the row.",
    ]
    return "\n".join(lines)


# --- sources ---------------------------------------------------------------------


def corpus_cases(corpus_dir: Path | None = None) -> list[tuple[CaseSpec, bytes]]:
    """Every corpus piece present on disk, credited from the manifest."""
    corpus_dir = CORPUS_DIR if corpus_dir is None else corpus_dir
    manifest_path = corpus_dir / "manifest.json"
    credits: dict[str, dict] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        credits = {p["id"]: p for p in manifest.get("pieces", [])}
    cases = []
    for path in sorted(corpus_dir.glob("*.mid")):
        entry = credits.get(path.stem, {})
        data = path.read_bytes()
        cases.append((CaseSpec(
            id=path.stem, title=entry.get("title", path.stem), origin="corpus", sha256=sha256_of(data),
            license=entry.get("license", ""), source_url=entry.get("source_page", ""),
            composer=entry.get("composer", ""),
        ), data))
    return cases


def file_cases(paths: list[Path]) -> list[tuple[CaseSpec, bytes]]:
    cases = []
    for path in paths:
        data = path.read_bytes()
        cases.append((CaseSpec(id=path.stem, title=path.stem, origin="file", sha256=sha256_of(data)), data))
    return cases


def discover_mutopia(
    catalogue: MutopiaCatalogue,
    *,
    instrument: str = "Piano",
    page: int = 0,
    limit: int = 10,
    cache_dir: Path = DISCOVERED_DIR,
    log: Callable[[str], None] | None = None,
) -> list[tuple[CaseSpec, bytes]]:
    """Pieces from one page of the catalogue onwards, downloaded if their licence allows.

    The cache keeps every file by its slug with provenance in `index.json`,
    so a piece is fetched once; a page past the end wraps to the first.
    """
    say = log or (lambda _line: None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path = cache_dir / "index.json"
    index: dict[str, dict] = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))

    start_at = max(0, page) * PAGE_SIZE
    found = list(catalogue.iter_search(instrument=instrument, start_at=start_at, max_pages=max(1, (limit + PAGE_SIZE - 1) // PAGE_SIZE)))
    if not found and start_at:
        say(f"page {page} is past the end of the {instrument} listing; starting from the first page")
        found = list(catalogue.iter_search(instrument=instrument, start_at=0, max_pages=max(1, (limit + PAGE_SIZE - 1) // PAGE_SIZE)))

    cases: list[tuple[CaseSpec, bytes]] = []
    for piece in found:
        if len(cases) >= limit:
            break
        if not piece.permitted:
            say(f"skipping {piece.title}: licence '{piece.license or piece.license_anchor}' is not permitted")
            continue
        if not piece.midi_url:
            say(f"skipping {piece.title}: no MIDI file")
            continue
        path = cache_dir / f"{piece.slug}.mid"
        entry = index.get(piece.slug)
        if path.exists() and entry and sha256_of(path.read_bytes()) == entry.get("sha256"):
            data = path.read_bytes()
        else:
            try:
                data = catalogue.download(piece)
            except CatalogueError as exc:
                say(f"skipping {piece.title}: {exc}")
                continue
            path.write_bytes(data)
            entry = {**piece.to_dict(), "sha256": sha256_of(data), "bytes": len(data),
                     "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            index[piece.slug] = entry
            index_path.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        say(f"{piece.slug}: {piece.title} by {piece.composer}, {piece.license}")
        cases.append((CaseSpec(
            id=piece.slug, title=piece.title, origin="discovered", sha256=entry["sha256"],
            license=piece.license, source_url=piece.source_page, composer=piece.composer,
        ), data))
    return cases


def load_profiles(specs: list[str]) -> list[PlayerProfile]:
    """`preset:beginner` or a path to a profile JSON file, for each spec."""
    profiles = []
    for spec in specs:
        if spec.startswith("preset:"):
            name = spec.split(":", 1)[1]
            if name not in PRESETS:
                raise ValueError(f"unknown preset '{name}'; presets are {', '.join(PRESETS)}")
            profiles.append(PRESETS[name])
        else:
            profiles.append(PlayerProfile.load(spec))
    return profiles


def default_page() -> int:
    """Walk the catalogue a page per scheduled run, without any stored state."""
    run_number = os.environ.get("GITHUB_RUN_NUMBER")
    if run_number and run_number.isdigit():
        return int(run_number)
    return dt.date.today().isocalendar().week


def run(
    cases: list[tuple[CaseSpec, bytes]],
    profiles: list[PlayerProfile],
    *,
    snippet_bars: int = 16,
    max_snippets: int = 3,
    deadline_seconds: float | None = 120.0,
    legal_only: bool = True,
    model_factory: Callable[[], object] | None = None,
    attempts: int = 4,
    check_reproducibility: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[CaseResult]:
    return [
        test_piece(
            spec, data, profiles, snippet_bars=snippet_bars, max_snippets=max_snippets,
            deadline_seconds=deadline_seconds, legal_only=legal_only, model_factory=model_factory,
            attempts=attempts, check_reproducibility=check_reproducibility, log=log,
        )
        for spec, data in cases
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arranger-autotest", description=__doc__.split("\n\n")[0])
    source = ap.add_argument_group("what to test")
    source.add_argument("--files", nargs="*", type=Path, default=[], help="MIDI or MusicXML files")
    source.add_argument("--corpus", action="store_true", help="the pieces in evals/corpus (the default when nothing else is given)")
    source.add_argument("--discover", choices=["mutopia"], help="find pieces on a website")
    source.add_argument("--instrument", default="Piano", help="catalogue instrument filter; empty for all")
    source.add_argument("--page", type=int, default=None, help="catalogue page to start from; default: the CI run number or the ISO week")
    source.add_argument("--limit", type=int, default=10, help="how many discovered pieces to test")
    source.add_argument("--cache-dir", type=Path, default=DISCOVERED_DIR)
    how = ap.add_argument_group("how to test")
    how.add_argument("--profiles", default="profiles/me.json", help="comma-separated: a profile file, or preset:<name>")
    how.add_argument("--snippets", type=int, default=16, help="snippet length in bars; 0 disables snippets")
    how.add_argument("--max-snippets", type=int, default=3)
    how.add_argument("--deadline", type=float, default=120.0, help="seconds per contestant per piece")
    how.add_argument("--all-patterns", action="store_true", help="let brute force use patterns above the player's skill")
    how.add_argument("--model", default=None, help="also run the model loop with this model (costs money)")
    how.add_argument("--attempts", type=int, default=4)
    how.add_argument("--check-reproducibility", action="store_true", help="run the engine twice on whole pieces too")
    out = ap.add_argument_group("results")
    out.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    out.add_argument("--update-baseline", action="store_true", help="write this run as the new baseline")
    out.add_argument("--no-baseline", action="store_true", help="do not compare with a baseline")
    out.add_argument("--report", type=Path, help="write the Markdown report here")
    out.add_argument("--json", type=Path, help="write every result here as JSON")
    out.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    say = (lambda _line: None) if args.quiet else print
    try:
        profiles = load_profiles([p.strip() for p in args.profiles.split(",") if p.strip()])
    except (ValueError, OSError) as exc:
        print(f"profiles: {exc}", file=sys.stderr)
        return 2

    cases: list[tuple[CaseSpec, bytes]] = []
    if args.files:
        cases += file_cases(args.files)
    if args.corpus or not (args.files or args.discover):
        found = corpus_cases()
        if not found:
            print("no corpus files on disk; run `python fetch_corpus.py`", file=sys.stderr)
        cases += found
    if args.discover == "mutopia":
        page = args.page if args.page is not None else default_page()
        say(f"discovering on Mutopia: instrument '{args.instrument}', page {page}, up to {args.limit} pieces")
        try:
            cases += discover_mutopia(
                MutopiaCatalogue(), instrument=args.instrument, page=page, limit=args.limit,
                cache_dir=args.cache_dir, log=say,
            )
        except CatalogueError as exc:
            print(f"discovery failed: {exc}", file=sys.stderr)
            return 2
    if not cases:
        print("nothing to test", file=sys.stderr)
        return 2

    model_factory = None
    if args.model:
        from ..agent import ClaudeModel

        model_factory = lambda: ClaudeModel(args.model)  # noqa: E731

    results = run(
        cases, profiles, snippet_bars=args.snippets, max_snippets=args.max_snippets,
        deadline_seconds=args.deadline, legal_only=not args.all_patterns, model_factory=model_factory,
        attempts=args.attempts, check_reproducibility=args.check_reproducibility, log=say,
    )

    regressions: list[str] = []
    if not args.no_baseline and args.baseline.exists() and not args.update_baseline:
        regressions = compare_with_baseline(results, json.loads(args.baseline.read_text(encoding="utf-8")))
    if args.update_baseline:
        fresh = baseline_from_results(results)
        if args.baseline.exists():
            # Merge: a run over a few files updates their cases and keeps the
            # rest, so the committed baseline need not be regenerated whole.
            existing = json.loads(args.baseline.read_text(encoding="utf-8"))
            merged_cases = {**existing.get("cases", {}), **fresh["cases"]}
            fresh = {**existing, **fresh, "cases": merged_cases}
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(fresh, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        say(f"baseline written to {args.baseline} ({len(fresh['cases'])} cases)")

    report = render_report(results, regressions)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n", encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "algorithm_version": ALGORITHM_VERSION, "regressions": regressions,
            "results": [r.to_dict() for r in results], "pool_candidates": candidates_for_pool(results),
        }, indent=1) + "\n", encoding="utf-8")
    if not args.quiet:
        print()
        print(report)

    failures = [f for r in results for f in r.failures()]
    return 1 if failures or regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
