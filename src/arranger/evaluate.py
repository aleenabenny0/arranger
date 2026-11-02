"""Measure the arranger against exhaustive search, and report what actually happens.

    python -m arranger.evaluate                       # the corpus, deterministic engine, no network
    python -m arranger.evaluate --model claude-sonnet-5 --repeats 5 --attempts 4

Two contestants are measured against the brute-force baseline on every piece:

- the deterministic engine (`engine.arrange_deterministic`), which costs
  nothing and needs no credentials;
- the model loop (`agent.arrange` with a model), only when `--model` is given.

The baseline is the best whole-piece plan found by trying every figure, voice
count and fold window (`agent.brute_force_candidate`). Two comparisons are
reported for each contestant:

- **on cost**, strictly: the pre-registered protocol in
  `docs/build-log/eval-protocol.md`. A win is `cost < baseline cost`. Ties do
  not count. On music where a uniform plan already reaches cost 0 nothing can
  win this, and the protocol says so.
- **on quality**: `engine.quality_key`, the ordering every search in this
  system uses. Cost first, then tempo, then strain, then fidelity above the
  floor. This is where per-passage judgement shows: two plans at cost 0 are
  not equal if one tires the player less or keeps more of the piece.

Exists because single runs cannot support conclusions. Three runs on the same
piece with identical inputs produced 8, 8, and 16 violations on their first
attempt, a 2x spread before any feedback existed. The deterministic engine is
run once per piece because it is deterministic; the model is run `--repeats`
times and summarised by median.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .adapters.midi import read_midi
from .agent import RunResult, arrange, brute_force_candidate
from .engine import Candidate, QualityKey, arrange_deterministic, quality_key
from .ir import Score
from .profile import PlayerProfile
from .render import last_bar
from .verify import verify

CORPUS_DIR = Path("evals") / "corpus"

# The protocol's minimum: fewer qualifying pieces than this and the model
# verdict is "insufficient data", never a pass or a fail.
MIN_VERDICT_POOL = 3


@dataclass
class Contestant:
    """One arrangement, reduced to what the two comparisons need."""

    cost: float
    hard: int
    strain: int
    fidelity: float
    tempo_scale: float
    sections: int
    accepted: bool
    seconds: float
    plan: str                 # a short description: "block/v1/f0" or "7 sections"
    origin: str = ""

    @property
    def quality(self) -> QualityKey:
        return quality_key(self.cost, self.tempo_scale, self.strain, self.fidelity)

    @classmethod
    def from_candidate(cls, candidate: Candidate, seconds: float) -> "Contestant":
        sections = candidate.plan.sections
        if len(sections) == 1:
            s = sections[0]
            plan = f"{s.lh_pattern.value}/v{s.lh_voices}/f{s.melody_fold_window}"
        else:
            plan = f"{len(sections)} sections"
        if candidate.plan.tempo_scale < 1.0:
            plan += f" @{candidate.plan.tempo_scale:.0%}"
        return cls(
            cost=round(candidate.cost, 4), hard=candidate.hard, strain=candidate.strain,
            fidelity=round(candidate.fidelity.score(), 4), tempo_scale=candidate.plan.tempo_scale,
            sections=len(sections), accepted=candidate.accepted, seconds=round(seconds, 2),
            plan=plan, origin=candidate.origin,
        )

    @classmethod
    def from_run(cls, run: RunResult) -> "Contestant":
        plan = run.best_plan or {}
        sections = plan.get("sections") or []
        tempo = float(plan.get("tempo_scale", 1.0) or 1.0)
        fidelity = run.best_fidelity or {}
        from .fidelity import Fidelity

        score = Fidelity(**fidelity).score() if fidelity else 0.0
        if len(sections) == 1:
            s = sections[0]
            label = f"{s.get('lh_pattern')}/v{s.get('lh_voices')}/f{s.get('melody_fold_window', 0)}"
        else:
            label = f"{len(sections)} sections"
        if tempo < 1.0:
            label += f" @{tempo:.0%}"
        return cls(
            cost=float(run.best_cost or 0.0), hard=int(run.best_hard or 0),
            strain=int(run.best_strain or 0), fidelity=round(score, 4), tempo_scale=tempo,
            sections=len(sections), accepted=run.accepted, seconds=run.seconds, plan=label,
            origin=run.best_origin or "",
        )


def outcome(contestant: Contestant, baseline: Contestant) -> tuple[str, str]:
    """(on cost, on quality), each 'win' | 'tie' | 'loss' against the baseline."""
    if contestant.cost < baseline.cost:
        on_cost = "win"
    elif contestant.cost > baseline.cost:
        on_cost = "loss"
    else:
        on_cost = "tie"
    if contestant.quality < baseline.quality:
        on_quality = "win"
    elif contestant.quality > baseline.quality:
        on_quality = "loss"
    else:
        on_quality = "tie"
    return on_cost, on_quality


@dataclass
class PieceResult:
    name: str
    notes: int
    bars: int
    source_hard: int
    baseline: Contestant                 # the brute force this piece is judged against
    baseline_all_patterns: Contestant | None = None   # the protocol's unfiltered search, when different
    engine: Contestant | None = None
    runs: list[RunResult] = field(default_factory=list)
    model_contestants: list[Contestant] = field(default_factory=list)
    error: str | None = None

    # --- protocol fields: cost only, strict ------------------------------------

    @property
    def qualifies(self) -> bool:
        """Only a piece brute force cannot fully solve can show a win on cost."""
        return self.baseline.cost > 0

    @property
    def successes(self) -> int:
        return sum(1 for r in self.runs if r.accepted)

    def costs(self) -> list[float]:
        return [r.best_cost for r in self.runs if r.best_cost is not None]

    def attempts_used(self) -> list[int]:
        return [len(r.attempts) for r in self.runs if r.accepted]

    def beat_baseline(self) -> int:
        """Model runs that beat the baseline on cost, strictly. The protocol's count."""
        return sum(1 for c in self.model_contestants if c.cost < self.baseline.cost)

    def model_wins_piece(self) -> bool:
        """A strict majority of repeats beat the baseline on cost."""
        return bool(self.runs) and self.beat_baseline() * 2 > len(self.runs)

    def model_typically_loses(self) -> bool:
        costs = self.costs()
        return bool(costs) and statistics.median(costs) >= self.baseline.cost

    # --- quality comparisons ------------------------------------------------

    def engine_outcome(self) -> tuple[str, str]:
        assert self.engine is not None
        return outcome(self.engine, self.baseline)

    def model_outcomes(self) -> list[tuple[str, str]]:
        return [outcome(c, self.baseline) for c in self.model_contestants]

    def beat_baseline_on_quality(self) -> int:
        return sum(1 for _, q in self.model_outcomes() if q == "win")

    # --- rows ----------------------------------------------------------------

    def engine_row(self) -> str:
        if self.error:
            return f"| {self.name} | {self.notes} | - | error: {self.error} | | | | | |"
        assert self.engine is not None
        on_cost, on_quality = self.engine_outcome()
        b, e = self.baseline, self.engine
        return (
            f"| {self.name} | {self.notes} | {b.cost:.2f} / {b.strain} / {b.fidelity:.3f} ({b.plan}) | "
            f"{e.cost:.2f} / {e.strain} / {e.fidelity:.3f} ({e.plan}) | "
            f"{on_cost} | {on_quality} | {b.seconds:.1f}s | {e.seconds:.1f}s |"
        )

    def model_row(self) -> str:
        costs = self.costs()
        attempts = self.attempts_used()
        spread = (
            f"{min(costs):.1f}-{max(costs):.1f}" if len(costs) > 1
            else (f"{costs[0]:.1f}" if costs else "-")
        )
        median_cost = f"{statistics.median(costs):.2f}" if costs else "-"
        median_attempts = f"{statistics.median(attempts):.0f}" if attempts else "-"
        n = len(self.runs)
        return (
            f"| {self.name} | {self.source_hard} | {self.baseline.cost:.2f} | "
            f"{median_cost} | {spread} | {self.successes}/{n} | "
            f"{median_attempts} | {self.beat_baseline()}/{n} | {self.beat_baseline_on_quality()}/{n} |"
        )


def measure_piece(
    source: Score,
    profile: PlayerProfile,
    *,
    name: str | None = None,
    legal_only: bool = True,
    deadline_seconds: float | None = None,
    model_factory: Callable[[], object] | None = None,
    repeats: int = 1,
    attempts: int = 4,
    countdown: bool = True,
    log: Callable[[str], None] | None = None,
) -> PieceResult:
    """Brute force, the engine and (optionally) the model on one piece."""
    say = log or (lambda _line: None)
    started = time.monotonic()
    deadline = None if deadline_seconds is None else started + deadline_seconds
    baseline_candidate = brute_force_candidate(source, profile, legal_only=legal_only, deadline=deadline)
    baseline = Contestant.from_candidate(baseline_candidate, time.monotonic() - started)
    piece = PieceResult(
        name=name or source.title, notes=len(source.notes), bars=last_bar(source),
        source_hard=len(verify(source, profile).hard), baseline=baseline,
    )
    if legal_only:
        started = time.monotonic()
        unfiltered = brute_force_candidate(source, profile, legal_only=False, deadline=deadline)
        if unfiltered.plan.sections[0].lh_pattern != baseline_candidate.plan.sections[0].lh_pattern \
                or unfiltered.cost != baseline_candidate.cost:
            piece.baseline_all_patterns = Contestant.from_candidate(unfiltered, time.monotonic() - started)
    say(f"{piece.name}: brute force {baseline.plan} cost {baseline.cost:.2f}, "
        f"{baseline.strain} strain, fidelity {baseline.fidelity:.3f}")

    started = time.monotonic()
    engine_deadline = None if deadline_seconds is None else started + deadline_seconds
    candidate = arrange_deterministic(source, profile, deadline=engine_deadline)
    piece.engine = Contestant.from_candidate(candidate, time.monotonic() - started)
    on_cost, on_quality = piece.engine_outcome()
    say(f"  engine: {piece.engine.plan} cost {piece.engine.cost:.2f}, {piece.engine.strain} strain, "
        f"fidelity {piece.engine.fidelity:.3f}: {on_cost} on cost, {on_quality} on quality")

    if model_factory is not None:
        for i in range(1, repeats + 1):
            # A fresh client per run so token counts are per-run rather than
            # cumulative, and so one run's conversation cannot leak into the
            # next through a shared object.
            model = model_factory()
            run = arrange(
                source, profile, model, max_attempts=attempts,
                verbose=False, countdown=countdown,
            )
            piece.runs.append(run)
            contestant = Contestant.from_run(run)
            piece.model_contestants.append(contestant)
            c, q = outcome(contestant, baseline)
            say(f"  model run {i}/{repeats}: {'accepted' if run.accepted else 'escalated'} "
                f"cost {run.best_cost}, {len(run.attempts)} attempts, {run.output_tokens} out: "
                f"{c} on cost, {q} on quality, from {run.best_origin}")
    return piece


def evaluate(
    midi_paths: list[Path],
    profile: PlayerProfile,
    repeats: int = 5,
    attempts: int = 4,
    model_name: str | None = None,
    countdown: bool = True,
    *,
    legal_only: bool = True,
    deadline_seconds: float | None = None,
    log: Callable[[str], None] | None = print,
) -> list[PieceResult]:
    """Every piece through `measure_piece`. A model is only built when named."""
    model_factory = None
    if model_name:
        from .agent import ClaudeModel

        model_factory = lambda: ClaudeModel(model_name)  # noqa: E731
    results = []
    for path in midi_paths:
        try:
            source = read_midi(path)
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not end the run
            results.append(PieceResult(
                name=path.stem, notes=0, bars=0, source_hard=0,
                baseline=Contestant(0, 0, 0, 0.0, 1.0, 0, False, 0.0, "-"),
                error=f"{type(exc).__name__}: {exc}"[:200],
            ))
            continue
        results.append(measure_piece(
            source, profile, name=path.stem, legal_only=legal_only,
            deadline_seconds=deadline_seconds, model_factory=model_factory,
            repeats=repeats, attempts=attempts, countdown=countdown, log=log,
        ))
    return results


def tally(outcomes: list[str]) -> str:
    return (
        f"{outcomes.count('win')} wins, {outcomes.count('tie')} ties, "
        f"{outcomes.count('loss')} losses"
    )


def protocol_verdict(results: list[PieceResult], min_pool: int = MIN_VERDICT_POOL) -> tuple[str, list[str]]:
    """PASS / FAIL / INSUFFICIENT DATA for the model, per the pre-registered protocol."""
    pool = [p for p in results if p.error is None and p.qualifies and p.runs]
    lines: list[str] = []
    if len(pool) < min_pool:
        lines.append(
            f"{len(pool)} qualifying piece(s) with model runs; the protocol needs at least "
            f"{min_pool} before it will say PASS or FAIL."
        )
        return "INSUFFICIENT DATA", lines
    wins = [p for p in pool if p.model_wins_piece()]
    for p in pool:
        state = "wins" if p.model_wins_piece() else "does not win"
        lines.append(f"- {p.name}: {p.beat_baseline()}/{len(p.runs)} runs beat the baseline on cost; the model {state}")
        if p.model_typically_loses():
            lines.append("  - **loses to brute force**: the median run cost is at or above the baseline")
    verdict = "PASS" if len(wins) * 2 > len(pool) else "FAIL"
    lines.append(f"{len(wins)} of {len(pool)} qualifying pieces won; a strict majority is needed.")
    return verdict, lines


def scorecard(
    results: list[PieceResult],
    repeats: int,
    attempts: int,
    countdown: bool,
    *,
    legal_only: bool = True,
    profile_name: str = "",
) -> str:
    measured = [p for p in results if p.error is None and p.engine is not None]
    engine_cost = [p.engine_outcome()[0] for p in measured]
    engine_quality = [p.engine_outcome()[1] for p in measured]
    lines = [
        "# Scorecard",
        "",
        f"{len(results)} piece(s), profile `{profile_name or 'unnamed'}`. Brute force searches every "
        f"{'skill-legal ' if legal_only else ''}whole-piece figure at 1-3 voices and 3 fold windows.",
        "",
        "Cost = hard violations + a penalty for falling below the fidelity floor;",
        "lower is better and 0 means playable with the music intact. Quality ranks",
        "cost, then tempo, then strain, then fidelity above the floor, in that order",
        "(`arranger.engine.quality_key`). A win on quality at equal cost means the",
        "arrangement plays with less strain, or keeps more of the piece, than the",
        "best uniform plan.",
        "",
        "## Deterministic engine vs brute force",
        "",
        "| piece | notes | brute force cost / strain / fidelity (plan) | engine cost / strain / fidelity (plan) | on cost | on quality | brute force time | engine time |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [p.engine_row() for p in results]
    lines += [
        "",
        f"**On cost: {tally(engine_cost)}.** **On quality: {tally(engine_quality)}.**",
    ]
    losses = [p.name for p in measured if p.engine_outcome()[1] == "loss"]
    if measured and not losses:
        lines.append("")
        lines.append("The engine never lost to brute force on quality.")
    elif losses:
        lines.append("")
        lines.append("The engine lost on quality on: " + ", ".join(losses) + ". Those are engine bugs to fix.")
    unfiltered = [p for p in measured if p.baseline_all_patterns is not None]
    if unfiltered:
        lines += [
            "",
            "Pieces where searching every pattern, including ones above this player's skill, "
            "found a different baseline (the protocol's unfiltered number):",
            "",
        ]
        for p in unfiltered:
            u = p.baseline_all_patterns
            assert u is not None
            lines.append(f"- {p.name}: {u.plan} cost {u.cost:.2f}, {u.strain} strain, fidelity {u.fidelity:.3f}")

    with_runs = [p for p in results if p.runs]
    if with_runs:
        total_runs = sum(len(p.runs) for p in with_runs)
        total_ok = sum(p.successes for p in with_runs)
        total_beat = sum(p.beat_baseline() for p in with_runs)
        total_quality = sum(p.beat_baseline_on_quality() for p in with_runs)
        tokens = sum(r.output_tokens for p in with_runs for r in p.runs)
        model_name = next((r.model for p in with_runs for r in p.runs if r.model), None)
        verdict, detail = protocol_verdict(results)
        lines += [
            "",
            f"## Model loop vs brute force ({model_name or 'model'})",
            "",
            f"{repeats} repeats per piece, {attempts} attempts per run, "
            f"countdown {'on' if countdown else 'off'}.",
            "",
            "| piece | source | baseline | median | spread | accepted | attempts | beat baseline on cost | on quality |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        lines += [p.model_row() for p in with_runs]
        lines += [
            "",
            f"**{total_ok}/{total_runs} runs accepted.** "
            f"**{total_beat}/{total_runs} beat the brute-force baseline on cost; "
            f"{total_quality}/{total_runs} on quality.** "
            f"{tokens:,} output tokens.",
            "",
            f"**Protocol verdict: {verdict}** (`docs/build-log/eval-protocol.md`; cost only, strict, "
            f"qualifying pieces only)",
            "",
            *detail,
        ]

    lines += ["", "## Reading this", ""]
    if with_runs:
        lines += [
            "The spread column matters more than the median. Where it is wide, a",
            "single run of this system tells you very little, and any claim resting",
            "on one run should be treated as anecdote.",
            "",
        ]
    lines += [
        "Pieces whose baseline cost is already 0 cannot demonstrate anything",
        "on cost: a perfect score is available without a model. They can still",
        "be won on quality, which is the comparison that shows musical judgement.",
        "The deterministic engine is run once per piece because it is",
        "deterministic; its numbers are exact, not a sample.",
    ]
    return "\n".join(lines)


def results_to_json(results: list[PieceResult]) -> dict:
    payload = []
    for p in results:
        item = {
            "name": p.name, "notes": p.notes, "bars": p.bars, "source_hard": p.source_hard,
            "error": p.error,
            "baseline": asdict(p.baseline),
            "baseline_all_patterns": asdict(p.baseline_all_patterns) if p.baseline_all_patterns else None,
            "engine": asdict(p.engine) if p.engine else None,
            "engine_outcome": list(p.engine_outcome()) if p.engine and p.error is None else None,
            "model": [asdict(c) for c in p.model_contestants],
            "model_outcomes": [list(o) for o in p.model_outcomes()],
            "runs": [json.loads(r.to_json()) for r in p.runs],
        }
        payload.append(item)
    return {"pieces": payload}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arranger-evaluate", description=__doc__.split("\n\n")[0])
    ap.add_argument("midi", nargs="*", type=Path, help=f"defaults to {CORPUS_DIR}/*.mid")
    ap.add_argument("--profile", default="profiles/me.json")
    ap.add_argument("--repeats", type=int, default=5, help="model runs per piece (with --model)")
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--model", default=None,
                    help="run the model loop with this model; without it nothing leaves the machine")
    ap.add_argument("--no-countdown", action="store_true")
    ap.add_argument("--all-patterns", action="store_true",
                    help="let brute force use patterns above the player's skill (the protocol's original search)")
    ap.add_argument("--deadline", type=float, default=None,
                    help="seconds allowed per contestant per piece; default unbounded")
    ap.add_argument("--out", type=Path, default=Path("SCORECARD.md"))
    ap.add_argument("--json", type=Path, help="write every number here as JSON")
    ap.add_argument("--raw", type=Path, help="write every model run log here as JSON")
    args = ap.parse_args(argv)

    paths = args.midi or sorted(CORPUS_DIR.glob("*.mid"))
    if not paths:
        print("no MIDI files found; run `python fetch_corpus.py` or pass paths")
        return 2

    profile = PlayerProfile.load(args.profile)
    results = evaluate(
        paths, profile, args.repeats, args.attempts, args.model,
        countdown=not args.no_countdown, legal_only=not args.all_patterns,
        deadline_seconds=args.deadline,
    )

    report = scorecard(
        results, args.repeats, args.attempts, not args.no_countdown,
        legal_only=not args.all_patterns, profile_name=profile.name,
    )
    args.out.write_text(report + "\n", encoding="utf-8")
    print("\n" + report)
    print(f"\nwritten to {args.out}")

    if args.json:
        args.json.write_text(json.dumps(results_to_json(results), indent=2) + "\n", encoding="utf-8")
        print(f"numbers: {args.json}")
    if args.raw:
        args.raw.write_text(json.dumps(
            {p.name: [json.loads(r.to_json()) for r in p.runs] for p in results},
            indent=2,
        ) + "\n", encoding="utf-8")
        print(f"raw run logs: {args.raw}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
