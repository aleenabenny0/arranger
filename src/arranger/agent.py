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

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

from . import engine
from .engine import ALGORITHM_VERSION, Cancelled, Candidate, evaluate_plan
from .fidelity import Fidelity, measure
from .ir import Score, pitch_name
from .musicianship import guidance_for_context, summarize_importance
from .plan import ArrangementPlan, LHPattern, Section, simple_plan
from .profile import PlayerProfile
from .repair import repair_prompt
from .render import RenderError, detect_chords, extract_melody, last_bar, render
from .verify import Verdict, verify

DEFAULT_MODEL = "claude-sonnet-5"
MAX_ATTEMPTS = 4

# An arrangement must be playable AND still be the song. Without the second
# condition the objective is maximised by deleting music, which is exactly
# what the first successful run did — see docs/build-log/m6-metric-gaming.md.
FIDELITY_FLOOR = 0.88

# How many violations one point of fidelity below the floor is worth. Sets the
# exchange rate between the two goals; without it "best" is ambiguous whenever
# one attempt is more playable and another is more faithful.
FIDELITY_WEIGHT = 60.0


def cost(hard: int, fidelity: Fidelity) -> float:
    """The single number the loop actually minimises. See `engine.cost`."""
    return engine.cost(hard, fidelity)


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


# US dollars per million tokens: (input, output). Cache reads are billed at a
# tenth of input and cache writes at 1.25x. Used only to enforce a spending
# cap, so a model missing from this table is priced as the most expensive one.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_FALLBACK_PRICE = (10.0, 50.0)


class TruncatedResponse(ValueError):
    """The model ran out of output tokens. Not a formatting mistake."""


class ModelRefused(ValueError):
    """The model declined the request. Feedback will not change that."""


class ProviderError(RuntimeError):
    """The model service failed. `retryable` says whether trying again can help."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def model_credentials_available() -> bool:
    """Is there anything the Anthropic SDK could authenticate with?

    An unset ANTHROPIC_API_KEY does not mean "no credentials": the SDK also
    accepts ANTHROPIC_AUTH_TOKEN and a stored login profile.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


class ClaudeModel:
    """Talks to the Anthropic API.

    **max_tokens must fit the largest plan.** A plan with a dozen sections and
    written rationales runs long, and running out of room fails in two
    different disguises: cut off mid-JSON it looks like a syntax error, and cut
    off *before* the JSON starts it looks like the model ignored instructions
    and wrote prose. The first live run lost two attempts to this and they
    appeared to be two unrelated problems.

    The conversation grows by one exchange per repair attempt and every
    request resends all of it, so the request asks for automatic prompt
    caching: attempt N reads attempts 1..N-1 from the cache instead of paying
    for them again.

    Thinking is left at the model's default (adaptive on current models).
    `temperature` and `budget_tokens` are not sent: current models reject both.
    """

    def __init__(
        self,
        model: str | None = None,
        max_tokens: int = 16000,
        *,
        timeout: float = 120.0,
        effort: str | None = None,
    ):
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "The anthropic package is not installed. Run:\n"
                "    pip install anthropic"
            ) from None
        if not model_credentials_available():
            raise RuntimeError(
                "No Anthropic credentials found. Set ANTHROPIC_API_KEY (create a key at "
                "console.anthropic.com) or log in with `ant auth login`."
            )
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ARRANGER_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.effort = effort or os.environ.get("ARRANGER_MODEL_EFFORT") or None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0

    @property
    def cost_usd(self) -> float:
        price_in, price_out = MODEL_PRICES.get(self.model, _FALLBACK_PRICE)
        return (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * 1.25
            + self.cache_read_tokens * price_in * 0.1
            + self.output_tokens * price_out
        ) / 1_000_000

    def __call__(self, messages: list[dict], *, timeout: float | None = None,
                 max_tokens: int | None = None) -> str:
        anthropic = self._anthropic
        limit = min(self.max_tokens, max_tokens) if max_tokens else self.max_tokens
        request: dict = {
            "model": self.model,
            "max_tokens": limit,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        try:
            response = self.client.with_options(timeout=timeout or self.timeout).messages.create(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderError(f"the model service rejected the credentials ({type(exc).__name__})",
                                retryable=False) from exc
        except (anthropic.BadRequestError, anthropic.NotFoundError) as exc:
            raise ProviderError(f"the model service rejected the request ({type(exc).__name__})",
                                retryable=False) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("the model service is rate limiting this account", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"the model service returned HTTP {exc.status_code}",
                                retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:   # includes timeouts
            raise ProviderError(f"could not reach the model service ({type(exc).__name__})",
                                retryable=True) from exc

        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            raise ModelRefused("the model declined this request")
        text = "".join(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "max_tokens":
            # Say plainly what happened. "Expecting ',' delimiter" sends the
            # model looking for a syntax error that does not exist.
            raise TruncatedResponse(
                f"response hit the {limit}-token limit and was cut "
                "off mid-plan. Use at most 6 sections, omit the reductions "
                "list, and keep notes to one short sentence."
            )
        return text


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
    fidelity: dict | None = None
    cost: float | None = None
    origin: str = "model"


@dataclass(frozen=True)
class RepairBudget:
    """Every way the loop is allowed to stop. None of them is optional in production."""

    max_attempts: int = MAX_ATTEMPTS
    max_seconds: float = 180.0
    max_cost_usd: float | None = None        # None: no spending cap (tests, scripted models)
    max_response_chars: int = 200_000
    patience: int = 2                        # valid attempts in a row that fail to improve the best
    max_repeated_plans: int = 1              # identical valid plans tolerated before stopping
    max_provider_failures: int = 2
    # A hosted service should not pay a model to improve something already
    # acceptable. A caller that hands over a model explicitly usually wants
    # its judgement, so the default is to ask.
    skip_model_when_draft_accepted: bool = False


@dataclass
class RunResult:
    title: str
    baseline_hard: int          # violations in the untouched source
    best_hard: int | None
    best_plan: dict | None
    playable: bool
    best_fidelity: dict | None = None
    best_cost: float | None = None
    accepted: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    escalated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    # Where the returned plan came from: "deterministic", "local_repair" or "model".
    best_origin: str | None = None
    # The deterministic draft, scored before any model was asked. It is the
    # candidate every model attempt has to beat.
    draft: Attempt | None = None
    stop_reason: str = ""
    seconds: float = 0.0
    cost_usd: float = 0.0
    model: str | None = None
    algorithm_version: str = ALGORITHM_VERSION

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)


def _parse_plan(text: str, max_chars: int = 200_000) -> ArrangementPlan:
    """Extract a plan from model output.

    Models sometimes wrap JSON in markdown fences despite instructions. Strip
    them rather than failing the attempt - a formatting slip is not a planning
    mistake, and burning a retry on it wastes the budget.
    """
    if len(text) > max_chars:
        raise ValueError(f"response is {len(text)} characters; the limit is {max_chars}")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    return ArrangementPlan.from_dict(json.loads(text[start : end + 1]))


def _fingerprint(plan: ArrangementPlan) -> str:
    """Identity of what a plan *does*. Titles, labels and notes do not render."""
    data = json.loads(plan.to_json())
    for key in ("title", "notes", "reductions"):
        data.pop(key, None)
    for section in data.get("sections", []):
        section.pop("label", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _attempt_from(candidate: Candidate, number: int, seconds: float = 0.0) -> Attempt:
    return Attempt(
        number=number, plan=json.loads(candidate.plan.to_json()), hard=candidate.hard,
        strain=len(candidate.verdict.strain), fidelity=asdict(candidate.fidelity),
        cost=round(candidate.cost, 2), seconds=seconds, origin=candidate.origin,
    )


def arrange(
    source: Score,
    profile: PlayerProfile,
    model=None,
    max_attempts: int = MAX_ATTEMPTS,
    verbose: bool = True,
    countdown: bool = True,
    *,
    budget: RepairBudget | None = None,
    progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RunResult:
    """Arrange `source` for `profile`. With no model, do it deterministically.

    The deterministic draft is scored first and is the best candidate until
    something beats it. A model can only ever improve the result: a worse,
    malformed, repeated, refused or never-delivered plan leaves the draft
    standing. The loop stops on the first of: accepted, attempts spent, time
    spent, money spent, no progress, a repeated plan, the provider failing, or
    the caller cancelling. `RunResult.stop_reason` says which.

    `countdown` controls whether feedback tells the model which attempt it is
    on and how many remain. It exists as a switch because two runs solved on
    their final attempt - the 4th of 4, then the 6th-7th of 7 - which suggests
    the deadline, not the accumulated feedback, is what triggers the strategy
    change. Turning it off is the ablation that tests this.
    """
    budget = budget or RepairBudget(max_attempts=max_attempts)
    started = time.monotonic()
    deadline = started + budget.max_seconds

    def report(fraction: float, stage: str) -> None:
        if progress is not None:
            progress(min(max(fraction, 0.0), 1.0), stage)

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise Cancelled()

    baseline = verify(source, profile)
    result = RunResult(
        title=source.title, baseline_hard=len(baseline.hard), best_hard=None,
        best_plan=None, playable=False, model=getattr(model, "model", None),
    )

    # --- candidate zero: no model, no cost -------------------------------
    best: Candidate = engine.arrange_deterministic(
        source, profile, deadline=deadline, should_cancel=should_cancel,
        progress=(lambda f, stage: report(0.45 * f, stage)) if model is not None else report,
    )
    check_cancel()
    result.draft = _attempt_from(best, 0, time.monotonic() - started)
    if verbose:
        print(f"  draft: {best.hard} hard, {best.fidelity.summary()} [{best.origin}]")

    def finish(reason: str) -> RunResult:
        result.stop_reason = reason
        result.best_hard = best.hard
        result.best_plan = json.loads(best.plan.to_json())
        result.best_fidelity = asdict(best.fidelity)
        result.best_cost = round(best.cost, 2)
        result.best_origin = best.origin
        result.playable = best.verdict.playable
        result.accepted = best.accepted
        result.escalated = not best.accepted
        result.input_tokens = getattr(model, "input_tokens", 0)
        result.output_tokens = getattr(model, "output_tokens", 0)
        result.cost_usd = round(float(getattr(model, "cost_usd", 0.0) or 0.0), 6)
        result.seconds = round(time.monotonic() - started, 3)
        report(1.0, "Done")
        if verbose:
            state = "ACCEPTED" if result.accepted else "escalated"
            print(f"  {state} ({reason}): {best.hard} hard, cost {best.cost:.2f}, from {best.origin}")
            if not result.playable:
                print("  remaining:", best.verdict.summary())
        return result

    if model is None:
        return finish("accepted" if best.accepted else "no_model")
    if best.accepted and budget.skip_model_when_draft_accepted:
        return finish("draft_accepted")

    # --- the model loop ----------------------------------------------------
    summary = describe_score(source, profile)
    guidance_text = "\n".join(
        f"- {item.title}: {item.summary}" for item in guidance_for_context(summary)
    )
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"{summary}\n\n"
                f"{summarize_importance(source)}\n\n"
                f"ARRANGING GUIDANCE RETRIEVED FOR THIS SCORE:\n{guidance_text}\n\n"
                f"The unarranged source has {len(baseline.hard)} hard violations.\n\n"
                "Start from this deterministic draft, then revise it only where "
                "the music or playability constraints require it. It currently has "
                f"{best.hard} hard violations and fidelity {best.fidelity.score():.2f}; "
                "a plan that does no better will be discarded in its favour:\n"
                f"{best.plan.to_json()}\n\n"
                "Write an ArrangementPlan covering every bar."
            ),
        }
    ]

    seen: dict[str, int] = {_fingerprint(best.plan): 0}
    stale = repeats = provider_failures = 0
    accepts_kwargs = _accepts_call_options(model)
    end = last_bar(source)

    for attempt_no in range(1, budget.max_attempts + 1):
        check_cancel()
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            return finish("time_budget")
        spent = float(getattr(model, "cost_usd", 0.0) or 0.0)
        if budget.max_cost_usd is not None and spent >= budget.max_cost_usd:
            return finish("cost_budget")
        report(0.45 + 0.5 * (attempt_no - 1) / budget.max_attempts,
               f"Asking the model for a better plan ({attempt_no} of {budget.max_attempts})")

        attempt_started = time.monotonic()
        attempt = Attempt(number=attempt_no, plan=None, hard=None, strain=None)
        raw = ""  # bound before the try: the error path reads it

        try:
            if accepts_kwargs:
                options: dict = {"timeout": max(5.0, min(remaining, 120.0))}
                if budget.max_cost_usd is not None:
                    _, price_out = MODEL_PRICES.get(getattr(model, "model", ""), _FALLBACK_PRICE)
                    affordable = int((budget.max_cost_usd - spent) * 1_000_000 / price_out)
                    options["max_tokens"] = max(1024, affordable)
                raw = model(messages, **options)
            else:
                raw = model(messages)
            plan = _parse_plan(raw, budget.max_response_chars)
            if problems := plan.validate_for_source(end, skill_level=profile.skill_level):
                raise ValueError("; ".join(problems))
            candidate = evaluate_plan(plan, source, profile, origin="model")

            attempt.plan = json.loads(plan.to_json())
            attempt.hard = candidate.hard
            attempt.strain = len(candidate.verdict.strain)
            attempt.fidelity = asdict(candidate.fidelity)
            attempt.cost = round(candidate.cost, 2)
            if verbose:
                print(f"  attempt {attempt_no}: {candidate.hard} hard, {candidate.fidelity.summary()}")

            # Keep the best result, not the most recent one. A later attempt
            # can be worse, and without this the loop can return a regression
            # after appearing to make progress. Ranked by cost, not violations
            # alone - otherwise an emptier arrangement always looks better.
            improved = candidate.better_than(best)
            if improved:
                best = candidate
                stale = 0
            else:
                stale += 1

            fingerprint = _fingerprint(plan)
            repeated = fingerprint in seen
            seen.setdefault(fingerprint, attempt_no)
            if repeated:
                repeats += 1

            attempt.seconds = time.monotonic() - attempt_started
            result.attempts.append(attempt)

            if candidate.accepted:
                return finish("accepted")
            if repeated and repeats > budget.max_repeated_plans:
                return finish("repeated_plan")
            if stale >= budget.patience and best.origin != "model":
                # The model has had `patience` valid tries and never beaten the
                # free candidate. Further attempts are paying for nothing.
                return finish("no_improvement")
            if stale >= budget.patience + 1:
                return finish("no_improvement")

            feedback = describe_verdict(candidate.verdict, plan)
            if candidate.verdict.playable:
                name, value = candidate.fidelity.weakest()
                feedback = (
                    f"Playable, but too much of the piece is gone: "
                    f"{candidate.fidelity.summary()}. Required: {FIDELITY_FLOOR:.2f}. "
                    f"The weakest part is {name} at {value:.0%}.\n"
                    "Restore the left hand where you removed it "
                    "(lh_voices=0) and solve the leaps another way."
                )
            else:
                feedback += (
                    f"\nFIDELITY: {candidate.fidelity.summary()} "
                    f"(must end at or above {FIDELITY_FLOOR:.2f}).\n"
                    "Setting lh_voices=0 removes violations by removing the "
                    "music and will fail this check.\n"
                )
            if repeated:
                feedback = (
                    f"That plan renders identically to the one from attempt {seen[fingerprint]}. "
                    "Resubmitting it cannot help; change a field that affects the notes.\n" + feedback
                )
            if not improved:
                feedback += (
                    f"\nThe best plan so far still has {best.hard} hard violations and cost "
                    f"{best.cost:.2f}; this one did not beat it.\n"
                )
            targeted_repairs = repair_prompt(candidate.verdict, plan)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{feedback}\n{targeted_repairs}\n{REPAIR_GUIDANCE}\n"
                        + (f"Attempt {attempt_no} of {budget.max_attempts}. " if countdown else "")
                        + "Revise the plan and return the complete JSON."
                    ),
                }
            )
            continue

        except ProviderError as exc:
            provider_failures += 1
            attempt.error = f"ProviderError: {exc}"
            attempt.seconds = time.monotonic() - attempt_started
            result.attempts.append(attempt)
            if verbose:
                print(f"  attempt {attempt_no}: provider failure - {exc}")
            if not exc.retryable or provider_failures >= budget.max_provider_failures:
                return finish("provider_failure")
            continue   # nothing to tell the model; it never answered

        except ModelRefused as exc:
            attempt.error = f"ModelRefused: {exc}"
            attempt.seconds = time.monotonic() - attempt_started
            result.attempts.append(attempt)
            return finish("model_refused")

        except (ValueError, RenderError, json.JSONDecodeError) as exc:
            # A malformed plan is feedback, not a crash. Tell the model what
            # broke and let it use the next attempt to fix it.
            attempt.error = f"{type(exc).__name__}: {str(exc)[:1500]}"
            if verbose:
                print(f"  attempt {attempt_no}: rejected - {attempt.error}")
            messages.append({"role": "assistant", "content": raw[:20_000] or "(no response)"})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That plan was rejected: {attempt.error}\n"
                        "Return corrected JSON matching the schema exactly."
                    ),
                }
            )
            attempt.seconds = time.monotonic() - attempt_started
            result.attempts.append(attempt)

    return finish("attempts_exhausted")


def _accepts_call_options(model) -> bool:
    """Does this model client take `timeout` / `max_tokens` per call?"""
    import inspect

    try:
        parameters = inspect.signature(model.__call__).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )


def brute_force_baseline(source: Score, profile: PlayerProfile) -> tuple[float, int, str, int, int]:
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
                arranged = render(plan, source)
                hard = len(verify(arranged, profile).hard)
                c = cost(hard, measure(source, arranged))
                if best is None or c < best[0]:
                    best = (c, hard, str(pattern), voices, fold)
    assert best is not None
    return best


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .adapters.midi import read_midi

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
    ap.add_argument(
        "--no-countdown", action="store_true",
        help="hide the attempt number from feedback (ablation)",
    )
    args = ap.parse_args(argv)

    profile = PlayerProfile.load(args.profile)
    source = read_midi(args.midi)
    end = last_bar(source)

    print(f"{source.title}: {len(source.notes)} notes, {end} bars")
    baseline = brute_force_baseline(source, profile)
    print(
        f"brute-force baseline: {baseline[1]} hard, cost {baseline[0]:.2f} "
        f"({baseline[2]}, {baseline[3]} voices, fold {baseline[4]})"
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

    result = arrange(
        source, profile, model, max_attempts=args.attempts,
        countdown=not args.no_countdown,
    )

    if result.best_hard is not None:
        print(
            f"\nsource {result.baseline_hard} -> agent {result.best_hard} hard, "
            f"cost {result.best_cost} (brute force {baseline[1]} hard, "
            f"cost {baseline[0]:.2f})"
        )
    if result.output_tokens:
        print(f"tokens: {result.input_tokens} in, {result.output_tokens} out")

    if args.out:
        args.out.write_text(result.to_json() + "\n")
        print(f"run log: {args.out}")

    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
