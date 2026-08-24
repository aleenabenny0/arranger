# Scorecard

1 repeats per piece, 4 attempts per run, countdown on.

Cost = hard violations + a penalty for falling below the fidelity
floor. Lower is better; 0 means playable with the music intact.

| piece | source | baseline | median | spread | accepted | attempts | beat baseline |
|---|---|---|---|---|---|---|---|
| Fur Elise | 11 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| Queen - Bohemian Rhapsody | 2435 | 1.00 | 5.32 | 5.3 | 0/1 | - | 0/1 |
| Tokyo Ghoul - Unravel | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |

**2/3 runs accepted.** **0/3 beat the brute-force baseline.** 10,328 output tokens.

## Reading this

The spread column matters more than the median. Where it is wide, a
single run of this system tells you very little, and any claim resting
on one run should be treated as anecdote.

Pieces whose baseline cost is already 0 cannot demonstrate anything
about the agent: a perfect score is available without a model. They are
kept as regression checks, not as evidence.
