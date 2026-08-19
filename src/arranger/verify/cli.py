"""Command-line entry point.

    python -m arranger.verify.cli score.json --profile profiles/me.json

Exit codes matter — this is what the Claude Code hook keys off:
    0 = playable
    1 = hard violations found
    2 = could not run (bad input, bad profile)

An agent that can read an exit code does not need to parse anything to know
whether it succeeded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..ir import Score, Note
from ..profile import PlayerProfile, PRESETS
from .constraints import verify


def load_score(path: Path) -> Score:
    """Load a Score from JSON.

    MusicXML and MIDI loaders arrive at milestone 2 and land in arranger.io.
    JSON is the format the verifier's own tests use, so it stays supported
    forever as the debugging format.
    """
    data = json.loads(path.read_text())
    notes = [
        Note(
            pitch=n["pitch"], onset=float(n["onset"]), duration=float(n["duration"]),
            staff=n.get("staff"), bar=n.get("bar"), voice=n.get("voice", 1),
        )
        for n in data["notes"]
    ]
    return Score(
        notes=notes,
        tempo_bpm=float(data.get("tempo_bpm", 100.0)),
        title=data.get("title", path.stem),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arranger-verify")
    ap.add_argument("score", type=Path, help="score JSON file")
    ap.add_argument(
        "--profile", default="intermediate",
        help="path to a profile JSON, or a preset name: " + ", ".join(PRESETS),
    )
    ap.add_argument("--quiet", action="store_true", help="JSON only, no human summary")
    args = ap.parse_args(argv)

    try:
        profile = (
            PRESETS[args.profile]
            if args.profile in PRESETS
            else PlayerProfile.load(args.profile)
        )
        score = load_score(args.score)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    verdict = verify(score, profile)
    print(verdict.to_json())

    if not args.quiet:
        head = "PLAYABLE" if verdict.playable else "NOT PLAYABLE"
        print(
            f"\n{head}  —  {len(verdict.hard)} hard, {len(verdict.strain)} strain",
            file=sys.stderr,
        )
        for v in verdict.hard[:10]:
            where = f"bar {v.bar}" if v.bar is not None else f"t={v.time:.2f}s"
            print(f"  [{where}] {v.message}", file=sys.stderr)

    return 0 if verdict.playable else 1


if __name__ == "__main__":
    raise SystemExit(main())
