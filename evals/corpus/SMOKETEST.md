> **This is not a real evaluation.** No Claude API calls were made — no
> `ANTHROPIC_API_KEY` was available in the environment this was generated
> in. `arranger.agent.ScriptedModel` stood in for the real model, replaying
> the same 3 fixed plans (`broken_octave`, `block`, `pedal_tone`) it uses
> for `--dry-run`, regardless of feedback. This file exists to confirm
> `arranger.evaluate`'s aggregation/scorecard code runs correctly against
> the new 20-piece corpus shape — nothing here says anything about whether
> the real agent is good. See the main chat report for what this run
> actually found (short version: `baseline` is 0.00 for all 20 pieces,
> which is itself the interesting result — see
> `evals/corpus/README.md#brute-force-baseline-is-0-for-every-piece-here`).

# Scorecard (scripted model, harness smoke test)

1 repeats per piece, 4 attempts per run, countdown on.

Cost = hard violations + a penalty for falling below the fidelity
floor. Lower is better; 0 means playable with the music intact.

| piece | source | baseline | median | spread | accepted | attempts | beat baseline |
|---|---|---|---|---|---|---|---|
| andre-sonatine-op34-1 | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| bach-invention-04-bwv775 | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| burgmuller-op100-01-la-candeur | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |
| burgmuller-op100-02-arabesque | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| burgmuller-op100-05-innocence | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| burgmuller-op100-08-la-gracieuse | 1 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| burgmuller-op100-10-tendre-fleur | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| burgmuller-op100-12-ladieu | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| burgmuller-op100-15-ballade | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| chopin-prelude-op28-20 | 3 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |
| satie-gymnopedie-2 | 66 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |
| tchaikovsky-op39-01-morning-prayer | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |
| tchaikovsky-op39-16-old-french-song | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| schumann-op15-07-traumerei | 49 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| mozart-k545-1-allegro | 0 | 0.00 | 3.00 | 3.0 | 0/1 | - | 0/1 |
| beethoven-fur-elise-woo59 | 0 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| brahms-waltz-op39-15 | 18 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |
| schubert-impromptu-d899-3 | 34 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |
| joplin-solace | 198 | 0.00 | 0.00 | 0.0 | 1/1 | 2 | 0/1 |
| field-nocturne-5-h37 | 7 | 0.00 | 0.00 | 0.0 | 1/1 | 1 | 0/1 |

**19/20 runs accepted.** **0/20 beat the brute-force baseline.** 0 output tokens.

## Reading this

The spread column matters more than the median. Where it is wide, a
single run of this system tells you very little, and any claim resting
on one run should be treated as anecdote.

Pieces whose baseline cost is already 0 cannot demonstrate anything
about the agent: a perfect score is available without a model. They are
kept as regression checks, not as evidence.
