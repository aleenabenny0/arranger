# M5: the agent lost, and why

First live run on Bohemian Rhapsody:

```
brute-force baseline: 36 hard (walking, 1 voice)
  attempt 1: 45 hard violations
  attempt 2: rejected - TruncatedResponse (8000-token limit)
  attempt 3: 44 hard violations
  attempt 4: rejected - TruncatedResponse
  escalated: best was 44 (source had 2435)
tokens: 14031 in, 25003 out
```

**Agent 44, brute force 36.** The agent lost to an exhaustive search over 18
fixed plans, and burned half its budget on truncation doing it.

The tempting reading is that the model reasoned badly. It did not. It had no
move available.

## All residual violations were in one hand

```
brute force residual: leap_infeasible 33, hand_span 8
by hand: R 41
```

Every single one is the **right hand** — which, in this renderer, is only the
melody.

The plan schema had exactly one melody lever: `melody_shift`, which moves an
entire section by a fixed interval. That can relocate a melody, but it cannot
narrow one. A section transposed down an octave has exactly the same internal
leaps it had before.

So the model was handed feedback saying *fix these 41 right-hand violations*
and given no field capable of fixing them. Its available moves were to add
sections and adjust `melody_shift` — which multiplied the places it could
apply an ineffective lever. Brute force, meanwhile, never tried to fix them at
all and simply picked the best global pattern. Doing nothing outperformed
trying.

**This is an action-space failure, not a reasoning failure.** No amount of
prompt engineering would have closed it, and the loop is exactly the machinery
that made it visible: a bounded budget, a hard verdict, and a baseline to
compare against turned "the arrangement is mediocre" into "the model has no
move here."

## The lever it needed

`melody_fold_window`: fold melody notes straying outside a window that many
semitones wide back in by octaves. Movement is octaves only, so every pitch
class survives — the tune stays recognisable even where its contour compresses.

Measured on Bohemian Rhapsody (walking bass, 1 voice):

| Window | Violations |
|---|---|
| 0 (off) | 41 |
| 24 | 24 |
| 19 | 10 |
| 16 | 10 |
| 14 | 6 |
| **12** | **2** |
| 9 | 3 |

41 → 2.

The minimum at 12 rather than at the tightest setting is the interesting part.
Folding into too narrow a window forces *more* octave displacements, each of
which is itself a leap. Squeeze harder and it gets worse. That is a real
trade-off the model now has a reason to reason about, rather than a monotone
dial where more is always better.

## New baselines

Brute force now searches fold windows too — 6 patterns x 3 voicings x 3
windows = 54 plans:

| File | Source | Brute force |
|---|---|---|
| Für Elise | 17 | **0** |
| Bohemian Rhapsody | 2468 | **2** |
| Unravel | 3 | **0** |

Für Elise and Unravel are now fully solved by brute force, which makes them
useless as agent benchmarks — a perfect score proves nothing about reasoning.
Bohemian Rhapsody at 2 is a much harder bar than the 36 it was before.

Raising the baseline this way is deliberate. A benchmark that flatters the
agent is worse than no benchmark: the honest comparison is against the best
thing that does not use a model at all, and if the agent cannot beat 54 fixed
plans, that is the finding.

Where it can still win: **per-section** fold windows. A quiet verse and a
soaring chorus want different treatment, and brute force applies one setting to
all 239 bars.

## Truncation, again

Two of four attempts died at the 8000-token limit — 25,003 output tokens over
four attempts, roughly 6,000 per plan.

The model was writing a section per musical region with a rationale for each,
which is thorough and unaffordable. Fixes: 16,000-token ceiling, an explicit
10-section cap in the schema rules, and a truncation message that now asks for
6 sections and no reductions list rather than the vague "be more concise".

## What this cost

Two live runs, roughly 32,000 output tokens, a few cents. The finding — that
the action space did not cover the dominant failure mode — was not visible from
any test, any single run, or any amount of reading the code. It required a
baseline to lose to.
