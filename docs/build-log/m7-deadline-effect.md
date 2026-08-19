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
