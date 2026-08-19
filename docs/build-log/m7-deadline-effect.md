# M7: the loop may be driven by the deadline, not the feedback

Two runs on Bohemian Rhapsody, identical except for the attempt budget.

**4 attempts:**
```
8 -> 9 -> 9 -> 0      ACCEPTED (attempt 4 of 4)
13,321 output tokens
```

**7 attempts:**
```
8 -> 10 -> 10 -> 10 -> 10 -> 1 -> 0    ACCEPTED (attempt 7 of 7)
35,834 output tokens
```

Both solved on the **final** attempt. Same piece, same profile, same starting
point, same answer — 2.7x the tokens.

## The hypothesis

The feedback message ends with `Attempt N of M`, and the system prompt says:

> If the same violation survives two attempts, change strategy entirely rather
> than adjusting the same number again.

The model appears to be treating the countdown, not the repeated violations, as
the trigger. With four attempts it plateaued for two and broke through on the
last. With seven it plateaued for **four** and broke through on the last. The
plateau expanded to fill the budget.

If that reading is right, the loop is not converging through accumulated
evidence. It is holding its strongest move until told it is out of road — and
every extra attempt in the budget is an extra attempt spent doing nothing.

## Why this is only a hypothesis

Two runs. Both on the same piece. No repeats at either setting. Three ordinary
explanations remain open:

- **Coincidence.** n=2 supports almost anything.
- **Run-to-run variance.** Attempt 1 gave 8 both times, but attempts 2-5 gave 9
  in one run and 10 in the other, so the trajectories are not identical.
- **Piece-specific.** Bohemian Rhapsody may simply need a strategy change that
  the model reaches at a fixed *rate* of exploration rather than a fixed point
  in the budget.

Distinguishing these needs repeats, which is the eval harness's job. Recording
it now so the hypothesis is on paper before the data that tests it — a
prediction written after seeing the results is not a prediction.

## The ablation

`--no-countdown` removes `Attempt N of M` from the feedback. The model still
gets violations and repair guidance; it simply cannot see the clock.

Predictions, written in advance:

- **If the deadline drives it:** without the countdown the model should change
  strategy earlier or not at all, and success should decouple from the final
  attempt.
- **If accumulated feedback drives it:** removing the countdown should change
  little, and solutions should still arrive around the same attempt number
  regardless of budget.

## The practical consequence either way

Set the budget low. Both runs produced the same arrangement; the 7-attempt run
paid 2.7x for it. If solutions arrive on the last attempt regardless, a large
budget buys nothing but tokens.

This matters for the eval harness before it matters anywhere else: 10 pieces x
5 repeats at 7 attempts is roughly 1.8M output tokens, and at 4 attempts about
670k, for what may well be identical results.

## The broader point

An agent that solves the problem is not the same as an agent that solves it
*because of* the mechanism you think. The feedback loop was built on the
assumption that structured violations drive revision. These two runs are the
first evidence that something else might be doing the work — and it was only
visible because the budget was changed and the result was compared, not because
anything failed.

Both runs "succeeded". Neither result was suspicious on its own.

---

# M7b: the ablation, and why it settles nothing

```
--no-countdown, 7 attempts:
16 -> 10 -> 10 -> 10 -> 1 -> 0    ACCEPTED (attempt 6 of 7)
```

The first run that did not solve on its final attempt. The deadline
hypothesis predicted exactly this, so at first reading it is confirmation.

It is not, and the reason is in the first column.

## Attempt 1, across three runs of the same piece

| Run | Attempt 1 violations |
|---|---|
| 4 attempts, countdown | 8 |
| 7 attempts, countdown | 8 |
| 7 attempts, no countdown | **16** |

Same piece, same profile, same prompt, same starting state — no feedback has
been exchanged yet at attempt 1, so these three numbers differ for no reason
except sampling.

**A 2x spread before the loop has done anything.**

Against that, the evidence for the deadline effect is that a breakthrough
moved from attempt 6 to attempt 5. One position. That is comfortably inside
the noise the first column demonstrates.

## The finding is the variance

Every conclusion drawn from single runs in this log is now suspect:

- "the agent beat brute force 0 to 1" — one sample from an unmeasured
  distribution
- "the deadline drives convergence" — two samples, one per condition
- "7 attempts costs 2.7x for the same result" — the *result* was the same
  twice; the cost ratio is one comparison

None are wrong. None are supported either. They are anecdotes that happen to
have numbers attached, which is the most persuasive kind of anecdote and the
easiest to mistake for data.

## What replaces guessing

`arranger.evaluate`: N repeats per piece, success rate, median cost, observed
spread, and how often the agent beat the brute-force baseline. Medians rather
than means, because at five repeats one escalated run drags a mean across the
whole scale.

The spread column is the point of the whole thing. Where it is wide, a single
run of this system tells you almost nothing — and that is a property worth
publishing rather than smoothing away.

The deadline question gets answered properly only by running both conditions
with enough repeats to see whether the distributions differ. Until then it
stays a hypothesis in this file, with its prediction recorded before the data
that will test it.

## Note on the corpus

Two of three sample pieces now have a brute-force baseline of 0. They cannot
demonstrate anything about the agent — a perfect score is available without a
model — so they remain only as regression checks.

A real benchmark needs pieces where the baseline is genuinely hard, and it
needs music the model cannot have memorised. The section labels in the first
successful run ("operatic climax", "hard rock section") match Bohemian
Rhapsody's actual structure, which the summary alone does not describe. That
confound is still open and still untested.
