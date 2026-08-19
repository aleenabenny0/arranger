# M4: the agentic loop

```
summarise -> model writes a plan -> render -> verify
                 ^                              |
                 +--------- feedback -----------+
                    (bounded attempts, then escalate)
```

53 tests across three suites. The agent tests run offline against a scripted
model — no API key, no cost, deterministic.

## Baselines to beat

Brute force over all 18 single-section plans (6 patterns x 3 voicings):

| File | Source | Brute force | Best plan |
|---|---|---|---|
| Bohemian Rhapsody | 2468 | **41** | walking, 1 voice |
| Für Elise | 17 | **11** | block, 1 voice |
| Unravel | 3 | **0** | block, 1 voice |

These are recorded *before* running the model, deliberately. A baseline
chosen after seeing the agent's results is not a baseline.

The agent cannot win by searching harder — brute force already searched
everything expressible as one section. It has to win by doing something brute
force cannot express: splitting the piece into sections with different
treatments, which requires knowing where the music changes.

## Three decisions that shape the loop

### The model never sees notes

It receives a summary: bar count, tempo, chord progression per region, texture
density, melody range. Bohemian Rhapsody's 4089 notes become about 20 lines.

Sending the notes would be a large prompt and would invite the model to reason
note-by-note — which is the renderer's job, done deterministically. Small
input, small output, small surface for error.

The summary divides the piece into equal chunks rather than musical phrases.
Phrase detection is a hard problem, and a wrong phrase boundary would mislead
the model more than a neutral grid does. The model groups chunks itself.

### Violations are summarised, and mapped to sections

Bohemian Rhapsody can produce 2400+ violations. Pasting them would fill the
context with near-identical lines and bury the signal.

Feedback aggregates by rule, shows the six worst offenders with their measured
and limit values, and — the part that matters — names which *section index*
covers the offending bars. The model edits sections. Feedback in timestamps is
not actionable no matter how precise it is.

### The best attempt is kept, not the last one

Models sometimes make things worse on a later attempt. Returning the most
recent plan means the loop can appear to make progress and then hand back a
regression.

`test_a_worse_later_attempt_does_not_overwrite_a_better_one` pins this: a
scripted model returns a good plan, then two bad ones, and the result must
still be the good one.

## Malformed output is feedback, not a crash

Three failure modes, all handled as ordinary feedback:

- **Markdown fences** around the JSON — stripped. A formatting slip is not a
  planning mistake and must not cost a retry.
- **Prose around the JSON** — the object is extracted from between the first
  `{` and last `}`.
- **Invalid plan** (overlapping sections, unknown pattern name) — the
  validation error is handed back verbatim. Error messages enumerate the valid
  options, because that message is the model's only guidance.

Only a response containing no JSON at all consumes an attempt without
producing a plan.

## A bug the loop exposed

Building the feedback formatter surfaced a renderer bug that the earlier tests
had missed: the right hand was reported as needing 8 fingers. The right hand
is *only the melody* — a single line cannot need 8 fingers.

Cause: `extract_melody` kept each note's original duration. Those durations
are sustained, often by pedal, so consecutive melody notes overlapped. An
overlapping "line" reads to the verifier as a hand holding six notes across
two octaves.

Fix: clip each melody note where the next one begins. A melodic line is
monophonic by definition.

Effect: Für Elise 12 → 11, Bohemian Rhapsody 42 → 41.

Worth noting how it was found. No test caught this, and no violation count
looked wrong. It surfaced because the feedback text was written to be read by
a human as well as a model, and "RH measured 8, limit 5" is obviously absurd
when you know the right hand holds one line. Formatting output for
readability found a bug that the metrics hid.

## The scripted model

`ScriptedModel` returns pre-written plans in sequence. Every agent test uses
it.

This keeps loop bugs and model-quality problems separate. Testing the loop
against a live model means every debugging run costs money, takes seconds, and
returns different results — so a flaky loop and a flaky model become
indistinguishable. With a scripted model the loop's behaviour is deterministic
and free, and the live model can then be evaluated on its own terms.

## What is not done

- No live-model results yet. The next run is the first one that costs money.
- No eval harness: single runs, no repeats, so no variance measurement.
- The agent has never been asked to produce *multiple* sections, which is the
  one thing that could beat brute force.
