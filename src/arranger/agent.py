"""The agentic loop.

    summarise -> model writes a plan -> render -> verify
                     ^                              |
                     +--------- feedback -----------+
                        (bounded attempts, then escalate)

Three design decisions carry most of the weight here.

**The model never sees notes.** It sees a summary: bar count, chord per
section, melody range, texture density. A 4000-note score would be a huge
prompt and would tempt the model to reason note-by-note, which is exactly the
job the renderer does deterministically. Small input, small output, small
surface for error.

**Violations are summarised, not dumped.** Bohemian Rhapsody produces 2400+
violations. Pasting them would fill the context with near-identical lines and
bury the signal. The feedback builder aggregates by rule and shows the worst
few examples with numbers attached.

**Attempts are bounded and the best result is kept.** The loop stops after a
fixed budget. Crucially, it returns the best plan *ever seen*, not the last
one — models sometimes make things worse on a later attempt, and without this
the loop can hand back a regression after appearing to work.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .ir import Score, pitch_name
from .plan import ArrangementPlan, LHPattern, Section, simple_plan
from .profile import PlayerProfile
from .render import RenderError, detect_chords, extract_melody, last_bar, render
from .verify import Verdict, verify

DEFAULT_MODEL = "claude-sonnet-5"
MAX_ATTEMPTS = 4

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


# --- describing the problem to the model --------------------------------


def describe_score(source: Score, profile: PlayerProfile, sections: int = 8) -> str:
    """A compact description of the piece: what a musician would want to know.

    Divided into equal chunks rather than by musical phrase, because phrase
    detection is a hard problem and getting it wrong would mislead the model
    more than a neutral grid does. The model can group chunks into sections
    however it likes.
    """
    end = last_bar(source)
    melody = extract_melody(source)
    chords = detect_chords(source, melody)

    lines = [
        f"PIECE: {source.title}",
        f"Bars 1-{end}, {source.tempo_bpm:.0f} bpm, {len(source.notes)} notes in source.",
    ]

    if melody:
        lo, hi = min(n.pitch for n in melody), max(n.pitch for n in melody)
        lines.append(
            f"Melody spans {pitch_name(lo)}-{pitch_name(hi)} "
            f"({hi - lo} semitones)."
        )
        if hi - lo > profile.max_span:
            lines.append(
                "  NOTE: the melody is wider than one hand span, so it will need "
                "octave displacement at phrase boundaries, not a single global shift."
            )

    lines.append("")
    lines.append("PLAYER:")
    lines.append(
        f"  reach {profile.max_span} semitones (comfortable {profile.comfortable_span}), "
        f"{profile.max_notes_per_hand} fingers/hand, skill level {profile.skill_level}/10, "
        f"hand speed {profile.max_leap_rate:.0f} semitones/sec."
    )

    lines.append("")
    lines.append("HARMONY AND TEXTURE BY REGION:")
    width = max(1, end // sections)
    for start in range(1, end + 1, width):
        stop = min(start + width - 1, end)
        bars = range(start, stop + 1)
        region_chords = [chords[b] for b in bars if b in chords]
        names = []
        for root, quality in region_chords:
            name = PITCH_CLASSES[root] + ("m" if quality.startswith("min") else "")
            if not names or names[-1] != name:
                names.append(name)
        density = sum(
            1 for n in source.notes if n.bar is not None and start <= n.bar <= stop
        ) / max(1, len(list(bars)))
        mel_here = [n.pitch for n in melody if n.bar and start <= n.bar <= stop]
        mel_desc = (
            f"melody {pitch_name(min(mel_here))}-{pitch_name(max(mel_here))}"
            if mel_here else "no melody"
        )
        lines.append(
            f"  bars {start}-{stop}: {' '.join(names[:8]) or '-'} | "
            f"{density:.0f} notes/bar | {mel_desc}"
        )

    return "\n".join(lines)


def describe_verdict(verdict: Verdict, plan: ArrangementPlan, limit: int = 6) -> str:
    """Turn violations into feedback the model can act on.

    Aggregated by rule, with the worst offenders shown in full and mapped back
    to the *section* that produced them — because the model edits sections, so
    "bars 40-60 are the problem" is actionable in a way that a list of
    timestamps is not.
    """
    if verdict.playable:
        return "PLAYABLE. No hard violations."

    lines = [f"NOT PLAYABLE: {len(verdict.hard)} hard violations."]
    lines.append("")

    by_rule: dict[str, list] = {}
    for v in verdict.hard:
        by_rule.setdefault(str(v.rule), []).append(v)

    for rule, group in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        worst = sorted(group, key=lambda v: -(v.measured - v.limit))[:limit]
        lines.append(f"{rule}: {len(group)} occurrences")
        for v in worst:
            where = f"bar {v.bar}" if v.bar is not None else f"t={v.time:.1f}s"
            hand = f"{v.hand}H " if v.hand else ""
            lines.append(
                f"  {where}: {hand}measured {v.measured:.0f}, limit {v.limit:.0f}"
            )
        # Which section owns these bars? That's the field the model can edit.
        bars = [v.bar for v in group if v.bar is not None]
        if bars:
            owners = {
                i for i, s in enumerate(plan.sections)
                if any(s.start_bar <= b <= s.end_bar for b in bars)
            }
            if owners:
                lines.append(f"  -> sections {sorted(owners)} cover these bars")
        lines.append("")

    return "\n".join(lines)


# --- prompts -------------------------------------------------------------

SYSTEM_PROMPT = """\
You arrange music for solo piano, for one specific player whose physical \
limits are given to you.

You produce an ArrangementPlan as JSON. You never write notes, pitches, or \
notation — a deterministic renderer turns your plan into a score, and a \
verifier then checks whether that score is physically playable. Your plan is \
the only thing you control.

SCHEMA:
{
  "title": str,
  "target_skill": int (1-10),
  "sections": [
    {
      "start_bar": int, "end_bar": int,
      "lh_pattern": "block"|"pedal_tone"|"broken_octave"|"arpeggio"|"alberti"|"walking",
      "melody_shift": int (semitones, usually 0 or -12 or +12),
      "lh_octave": int (2=low, 3=standard, 4=high),
      "lh_voices": int (0-5; 0 means melody only, 1 is a single bass note),
      "melody_fold_window": int (0 = off, else 7-24),
      "label": str
    }
  ],
  "reductions": [
    {"kind": "doubling"|"inner_voice"|"bass_movement"|"harmonic_colour"|"countermelody",
     "start_bar": int, "end_bar": int, "rationale": str}
  ],
  "notes": str
}

RULES:
- Sections must not overlap and should cover every bar.
- The melody is never dropped or thinned.
- Left-hand pattern by difficulty: pedal_tone and block are easiest; walking \
and arpeggio are moderate; alberti needs evenness; broken_octave is hardest \
because the hand must cross an octave repeatedly at tempo.
- Fewer lh_voices is easier and thinner. lh_voices=1 is a single bass note.
- Do not exceed the player's skill level by more than one.
- melody_fold_window folds melody notes straying outside a window that many \
semitones wide back in by octaves. It is the only lever that fixes leaps and \
spans WITHIN a section; melody_shift moves a whole section uniformly and \
cannot. Use it wherever a region's melody is wider than the player's reach. \
Around 12-16 usually works; below 9 flattens the tune.
- Use at most 10 sections. Long plans get truncated and waste the attempt.

Respond with JSON only. No prose, no markdown fences, no explanation outside \
the "notes" field.

Keep it compact. Every "rationale" and the "notes" field must be one short \
sentence. Long plans get truncated mid-JSON and the attempt is wasted.
"""

REPAIR_GUIDANCE = """\
FIXES BY VIOLATION TYPE:
- hand_span or leap_infeasible in the RIGHT hand: this is the melody. Set \
melody_fold_window on the affected sections (try 12). Splitting into more \
sections does NOT help — melody_shift moves a section uniformly and cannot \
fix a leap inside it.
- hand_span in the LEFT hand: reduce lh_voices, or lower lh_octave.
- hand_polyphony: reduce lh_voices.
- leap_infeasible: switch to a pattern that keeps the hand still \
(pedal_tone, block). broken_octave and arpeggio cause leaps by design.
- total_polyphony: reduce lh_voices.
- range: adjust melody_shift or lh_octave for the affected section only.

If the same violation survives two attempts, change strategy entirely rather \
than adjusting the same number again. Trying a fourth variation of an \
approach that has failed three times is the most common way these loops waste \
their budget.
"""


# --- model clients -------------------------------------------------------


class ClaudeModel:
    """Talks to the Anthropic API.

    **max_tokens must fit the largest plan.** A plan with a dozen sections and
    written rationales runs long, and running out of room fails in two
    different disguises: cut off mid-JSON it looks like a syntax error, and cut
    off *before* the JSON starts it looks like the model ignored instructions
    and wrote prose. The first live run lost two attempts to this and they
    appeared to be two unrelated problems.

    Response prefill would prevent the prose case structurally, but not every
    model supports it, and once the token ceiling is right it is unnecessary —
    the parser already tolerates prose and fences around a complete object.
    """

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 16000):
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "The anthropic package is not installed. Run:\n"
                "    pip install anthropic"
            ) from None
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Create a key at "
                "console.anthropic.com, then set it in your environment."
            )
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens
        self.input_tokens = 0
        self.output_tokens = 0

    def __call__(self, messages: list[dict]) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        text = "".join(b.text for b in response.content if b.type == "text")

        if response.stop_reason == "max_tokens":
            # Say plainly what happened. "Expecting ',' delimiter" sends the
            # model looking for a syntax error that does not exist.
            raise TruncatedResponse(
                f"response hit the {self.max_tokens}-token limit and was cut "
                "off mid-plan. Use at most 6 sections, omit the reductions "
                "list, and keep notes to one short sentence."
            )
        return text


class TruncatedResponse(ValueError):
    """The model ran out of output tokens. Not a formatting mistake."""


class ScriptedModel:
    """A stand-in that returns pre-written plans. No API, no cost.

    Exists so the loop itself can be tested — bounded retries, best-so-far
    tracking, feedback formatting, escalation — without a network call. Loop
    bugs and model quality are different problems and should be debuggable
    separately.
    """

    def __init__(self, plans: list[dict]):
        self.plans = plans
        self.calls = 0
        self.input_tokens = self.output_tokens = 0

    def __call__(self, messages: list[dict]) -> str:
        plan = self.plans[min(self.calls, len(self.plans) - 1)]
        self.calls += 1
        return json.dumps(plan)


# --- the loop ------------------------------------------------------------


@dataclass
class Attempt:
    number: int
    plan: dict | None
    hard: int | None
    strain: int | None
    error: str | None = None
    seconds: float = 0.0


@dataclass
class RunResult:
    title: str
    baseline_hard: int          # violations in the untouched source
    best_hard: int | None
    best_plan: dict | None
    playable: bool
    attempts: list[Attempt] = field(default_factory=list)
    escalated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)


def _parse_plan(text: str) -> ArrangementPlan:
    """Extract a plan from model output.

    Models sometimes wrap JSON in markdown fences despite instructions. Strip
    them rather than failing the attempt — a formatting slip is not a planning
    mistake, and burning a retry on it wastes the budget.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    return ArrangementPlan.from_dict(json.loads(text[start : end + 1]))


def arrange(
    source: Score,
    profile: PlayerProfile,
    model=None,
    max_attempts: int = MAX_ATTEMPTS,
    verbose: bool = True,
) -> RunResult:
    """Run the loop until the arrangement is playable or the budget runs out."""
    model = model or ClaudeModel()
    baseline = verify(source, profile)
    result = RunResult(
        title=source.title,
        baseline_hard=len(baseline.hard),
        best_hard=None,
        best_plan=None,
        playable=False,
    )

    summary = describe_score(source, profile)
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"{summary}\n\n"
                f"The unarranged source has {len(baseline.hard)} hard violations.\n\n"
                "Write an ArrangementPlan covering every bar."
            ),
        }
    ]

    best_verdict: Verdict | None = None

    for attempt_no in range(1, max_attempts + 1):
        started = time.time()
        attempt = Attempt(number=attempt_no, plan=None, hard=None, strain=None)
        raw = ""  # bound before the try: the error path reads it

        try:
            raw = model(messages)
            plan = _parse_plan(raw)
            arranged = render(plan, source)
            verdict = verify(arranged, profile)

            attempt.plan = json.loads(plan.to_json())
            attempt.hard = len(verdict.hard)
            attempt.strain = len(verdict.strain)

            # Keep the best result, not the most recent one. A later attempt
            # can be worse, and without this the loop can return a regression
            # after appearing to make progress.
            if result.best_hard is None or len(verdict.hard) < result.best_hard:
                result.best_hard = len(verdict.hard)
                result.best_plan = attempt.plan
                best_verdict = verdict

            if verbose:
                print(f"  attempt {attempt_no}: {len(verdict.hard)} hard violations")

            if verdict.playable:
                result.playable = True
                attempt.seconds = time.time() - started
                result.attempts.append(attempt)
                break

            feedback = describe_verdict(verdict, plan)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{feedback}\n{REPAIR_GUIDANCE}\n"
                        f"Attempt {attempt_no} of {max_attempts}. "
                        "Revise the plan and return the complete JSON."
                    ),
                }
            )

        except (ValueError, RenderError, json.JSONDecodeError) as exc:
            # A malformed plan is feedback, not a crash. Tell the model what
            # broke and let it use the next attempt to fix it.
            attempt.error = f"{type(exc).__name__}: {exc}"
            if verbose:
                print(f"  attempt {attempt_no}: rejected - {attempt.error}")
            messages.append({"role": "assistant", "content": raw or "(no response)"})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That plan was rejected: {attempt.error}\n"
                        "Return corrected JSON matching the schema exactly."
                    ),
                }
            )

        attempt.seconds = time.time() - started
        result.attempts.append(attempt)

    result.escalated = not result.playable
    result.input_tokens = getattr(model, "input_tokens", 0)
    result.output_tokens = getattr(model, "output_tokens", 0)

    if verbose:
        if result.playable:
            print(f"  PLAYABLE after {len(result.attempts)} attempt(s)")
        else:
            print(
                f"  escalated: best was {result.best_hard} hard violations "
                f"(source had {result.baseline_hard})"
            )
        if best_verdict and not result.playable:
            print("  remaining:", best_verdict.summary())

    return result


def brute_force_baseline(source: Score, profile: PlayerProfile) -> tuple[int, str, int, int]:
    """The score to beat: best single-section plan, found by exhaustive search.

    An agent that cannot beat this is not earning its cost. Reported alongside
    every run so improvement is measured against a real alternative rather
    than against doing nothing.
    """
    end = last_bar(source)
    best = None
    for pattern in LHPattern:
        for voices in (1, 2, 3):
            for fold in (0, 12, 16):
                plan = ArrangementPlan(
                    title="brute-force",
                    sections=[Section(1, end, pattern, lh_voices=voices,
                                      melody_fold_window=fold)],
                )
                hard = len(verify(render(plan, source), profile).hard)
                if best is None or hard < best[0]:
                    best = (hard, str(pattern), voices, fold)
    assert best is not None
    return best


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .io import read_midi

    ap = argparse.ArgumentParser(prog="arranger-agent")
    ap.add_argument("midi", type=Path)
    ap.add_argument("--profile", default="profiles/me.json")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--attempts", type=int, default=MAX_ATTEMPTS)
    ap.add_argument("--out", type=Path, help="write the run log here")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="use a scripted model; no API calls, no cost",
    )
    args = ap.parse_args(argv)

    profile = PlayerProfile.load(args.profile)
    source = read_midi(args.midi)
    end = last_bar(source)

    print(f"{source.title}: {len(source.notes)} notes, {end} bars")
    baseline = brute_force_baseline(source, profile)
    print(
        f"brute-force baseline: {baseline[0]} hard "
        f"({baseline[1]}, {baseline[2]} voices, fold {baseline[3]})"
    )

    if args.dry_run:
        model = ScriptedModel([
            asdict(simple_plan(end, LHPattern.BROKEN_OCTAVE)),
            asdict(simple_plan(end, LHPattern.BLOCK)),
            asdict(simple_plan(end, LHPattern.PEDAL_TONE)),
        ])
        print("dry run: scripted model, no API calls")
    else:
        model = ClaudeModel(args.model)

    result = arrange(source, profile, model, max_attempts=args.attempts)

    if result.best_hard is not None:
        print(
            f"\nsource {result.baseline_hard} -> agent {result.best_hard} "
            f"(brute force {baseline[0]})"
        )
    if result.output_tokens:
        print(f"tokens: {result.input_tokens} in, {result.output_tokens} out")

    if args.out:
        args.out.write_text(result.to_json() + "\n")
        print(f"run log: {args.out}")

    return 0 if result.playable else 1


if __name__ == "__main__":
    raise SystemExit(main())
