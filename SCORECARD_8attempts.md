# Scorecard

8 repeats per piece, 8 attempts per run, countdown on.

Cost = hard violations + a penalty for falling below the fidelity
floor. Lower is better; 0 means playable with the music intact.

| piece | source | baseline | median | spread | accepted | attempts | beat baseline |
|---|---|---|---|---|---|---|---|
| Queen - Bohemian Rhapsody | 2435 | 1.00 | 0.00 | 0.0-7.0 | 7/8 | 5 | 7/8 |

**7/8 runs accepted.** **7/8 beat the brute-force baseline.** 114,769 output tokens.

## Reading this

The spread column matters more than the median. Where it is wide, a
single run of this system tells you very little, and any claim resting
on one run should be treated as anecdote.

Pieces whose baseline cost is already 0 cannot demonstrate anything
about the agent: a perfect score is available without a model. They are
kept as regression checks, not as evidence.
