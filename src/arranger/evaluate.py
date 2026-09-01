"""Run the agent repeatedly and report what actually happens.

    python -m arranger.evaluate --repeats 5 --attempts 4

Exists because single runs cannot support conclusions. Three runs on the same
piece with identical inputs produced 8, 8, and 16 violations on their first
attempt — a 2x spread before any feedback existed. Every number from a single
run sits inside that spread.

What gets reported: success rate, median and spread of attempts, cost, and
tokens. Median rather than mean because a single escalated run drags a mean
across the whole scale, and with five repeats the mean is not describing a
distribution so much as an accident.

The brute-force baseline is recomputed for every piece and printed alongside.
An agent that cannot beat an exhaustive search over 54 fixed plans has not
earned its tokens, and that comparison should be impossible to look away from.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.midi import read_midi
from .agent import ClaudeModel, RunResult, arrange, brute_force_baseline
from .profile import PlayerProfile


@dataclass
class PieceResult:
    name: str
    source_hard: int
    baseline_cost: float
    baseline_hard: int
    runs: list[RunResult] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for r in self.runs if r.accepted)

    @property
    def success_rate(self) -> float:
        return self.successes / len(self.runs) if self.runs else 0.0

    def attempts_used(self) -> list[int]:
        return [len(r.attempts) for r in self.runs if r.accepted]

    def costs(self) -> list[float]:
        return [r.best_cost for r in self.runs if r.best_cost is not None]

    def beat_baseline(self) -> int:
        return sum(
            1 for r in self.runs
            if r.best_cost is not None and r.best_cost < self.baseline_cost
        )

    def row(self) -> str:
        costs = self.costs()
        attempts = self.attempts_used()
        spread = (
            f"{min(costs):.1f}-{max(costs):.1f}" if len(costs) > 1
            else (f"{costs[0]:.1f}" if costs else "-")
        )
        median_cost = f"{statistics.median(costs):.2f}" if costs else "-"
        median_attempts = f"{statistics.median(attempts):.0f}" if attempts else "-"
        return (
            f"| {self.name} | {self.source_hard} | {self.baseline_cost:.2f} | "
            f"{median_cost} | {spread} | {self.successes}/{len(self.runs)} | "
            f"{median_attempts} | {self.beat_baseline()}/{len(self.runs)} |"
        )


def evaluate(
    midi_paths: list[Path],
    profile: PlayerProfile,
    repeats: int = 5,
    attempts: int = 4,
    model_name: str | None = None,
    countdown: bool = True,
) -> list[PieceResult]:
    results = []
    for path in midi_paths:
        source = read_midi(path)
        baseline = brute_force_baseline(source, profile)
        from .verify import verify

        piece = PieceResult(
            name=source.title,
            source_hard=len(verify(source, profile).hard),
            baseline_cost=baseline[0],
            baseline_hard=baseline[1],
        )
        print(f"\n{piece.name}  (baseline cost {piece.baseline_cost:.2f})")

        for i in range(1, repeats + 1):
            # A fresh client per run so token counts are per-run rather than
            # cumulative, and so one run's conversation cannot leak into the
            # next through a shared object.
            model = ClaudeModel(model_name) if model_name else ClaudeModel()
            print(f"  run {i}/{repeats}:", end=" ", flush=True)
            run = arrange(
                source, profile, model, max_attempts=attempts,
                verbose=False, countdown=countdown,
            )
            piece.runs.append(run)
            print(
                f"{'accepted' if run.accepted else 'escalated':9} "
                f"cost {run.best_cost}, {len(run.attempts)} attempts, "
                f"{run.output_tokens} out"
            )
        results.append(piece)
    return results


def scorecard(results: list[PieceResult], repeats: int, attempts: int,
              countdown: bool) -> str:
    lines = [
        "# Scorecard",
        "",
        f"{repeats} repeats per piece, {attempts} attempts per run, "
        f"countdown {'on' if countdown else 'off'}.",
        "",
        "Cost = hard violations + a penalty for falling below the fidelity",
        "floor. Lower is better; 0 means playable with the music intact.",
        "",
        "| piece | source | baseline | median | spread | accepted | attempts | beat baseline |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [p.row() for p in results]

    total_runs = sum(len(p.runs) for p in results)
    total_ok = sum(p.successes for p in results)
    total_beat = sum(p.beat_baseline() for p in results)
    tokens = sum(r.output_tokens for p in results for r in p.runs)

    lines += [
        "",
        f"**{total_ok}/{total_runs} runs accepted.** "
        f"**{total_beat}/{total_runs} beat the brute-force baseline.** "
        f"{tokens:,} output tokens.",
        "",
        "## Reading this",
        "",
        "The spread column matters more than the median. Where it is wide, a",
        "single run of this system tells you very little, and any claim resting",
        "on one run should be treated as anecdote.",
        "",
        "Pieces whose baseline cost is already 0 cannot demonstrate anything",
        "about the agent: a perfect score is available without a model. They are",
        "kept as regression checks, not as evidence.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arranger-evaluate")
    ap.add_argument("midi", nargs="*", type=Path, help="defaults to samples/*.mid")
    ap.add_argument("--profile", default="profiles/me.json")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-countdown", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("SCORECARD.md"))
    ap.add_argument("--raw", type=Path, help="write every run log here as JSON")
    args = ap.parse_args(argv)

    paths = args.midi or sorted(Path("samples").glob("*.mid"))
    if not paths:
        print("no MIDI files found; put some in samples/ or pass paths")
        return 2

    profile = PlayerProfile.load(args.profile)
    results = evaluate(
        paths, profile, args.repeats, args.attempts, args.model,
        countdown=not args.no_countdown,
    )

    report = scorecard(results, args.repeats, args.attempts, not args.no_countdown)
    args.out.write_text(report + "\n")
    print("\n" + report)
    print(f"\nwritten to {args.out}")

    if args.raw:
        args.raw.write_text(json.dumps(
            {p.name: [json.loads(r.to_json()) for r in p.runs] for p in results},
            indent=2,
        ) + "\n")
        print(f"raw run logs: {args.raw}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
