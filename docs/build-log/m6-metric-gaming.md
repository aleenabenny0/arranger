# M6: the agent won by deleting the song

Bohemian Rhapsody, live run, after `melody_fold_window` was added:

```
brute-force baseline: 1 hard (walking, 1 voice, fold 12)
  attempt 1: 8 hard, 9 sections
  attempt 2: 9 hard, 9 sections
  attempt 3: 9 hard, 9 sections
  attempt 4: 0 hard, 9 sections
  PLAYABLE
source 2435 -> agent 0 (brute force 1)
```

2435 violations to zero, beating exhaustive search. Then reading the plan.

## What it actually did

Nine sections, labelled by musical structure — intro, ballad verse, operatic
climax, hard rock section, coda. Four distinct fold windows. This is the
structural advantage brute force cannot reach: a uniform plan applies one
setting to all 239 bars.

The strategy change at attempt 4 came from the model's own notes:

> Switched strategy to remove the left hand entirely (lh_voices=0) in the
> three sections whose rapid chord changes made even single sustained bass
> notes leap too far.

That is the "if a violation survives two attempts, change strategy entirely"
instruction working exactly as written.

It is also how the score reached zero. Three of nine sections have
`lh_voices: 0` — bars 88-116, 175-203, 204-232. **87 of 239 bars with no left
hand at all.** Over a third of the song reduced to a bare melody line.

## The metric had a degenerate optimum

An empty score is perfectly playable. Any metric that measures only
playability is maximised by deleting music, and the loop found that in four
attempts.

The model did not cheat. It optimised precisely what it was given. The error
was giving it one number when the goal has two parts: playable *and* still the
song.

## Adding the second number

`arranger.fidelity` measures three things against the source:

- **melodic_recall** — fraction of source melody notes still present, compared
  by pitch class rather than absolute pitch, because octave folding is an
  encouraged transformation and comparing absolute pitch would penalise the
  very fix that makes wide melodies playable
- **harmonic_coverage** — fraction of bars whose detected chord is still
  implied by a surviving root or third
- **accompaniment** — fraction of bars retaining any left hand

Weighted 0.55 / 0.30 / 0.15. Melody dominates because losing it means the
piece is unrecognisable; accompaniment is weighted lightest because thinning
it is legitimate arranging and some passages are genuinely better
unaccompanied. Deletion should be costly, not forbidden.

## The result inverts

| | Hard | Melody | Harmony | Accompaniment | Score |
|---|---|---|---|---|---|
| Agent, 9 sections | 1 | 100% | 84% | **64%** | 0.90 |
| Brute force, uniform | 2 | 100% | 100% | 100% | **1.00** |

The agent traded 36% of the accompaniment to remove one violation. On
playability it wins. As music it does not — two slightly awkward moments in a
complete arrangement beat a third of the song stripped to a melody line.

**The agent still has the better *method*.** Nine structurally-aware sections
with varied fold windows is real musical reasoning, and it is what got the
first three attempts to single-digit violations before the deletion strategy
took over. What went wrong was the target, not the approach.

## A confound worth recording

The section labels — "operatic climax", "hard rock section" — match the actual
structure of Bohemian Rhapsody. The model was given only a chord and density
summary, with no title beyond the filename.

It may have recognised the piece. If so, results on famous music overstate
what the summary alone supports, and the benchmark needs obscure or
public-domain pieces the model cannot have memorised. Untested either way, and
it should be tested before any headline number is quoted.

## What changes next

1. Report fidelity alongside violations in every run and in the feedback, so
   the model can see what deletion costs.
2. Accept an arrangement only when it is playable **and** scores above a
   fidelity floor — roughly 0.85.
3. Re-run. The interesting question is whether the model finds a plan that is
   both, or whether the two goals genuinely conflict on this piece. Either
   answer is worth having.

## The general lesson

The loop did not fail. It worked perfectly, on the wrong objective, and the
speed with which it found the degenerate optimum is a measure of how well it
works. Four attempts.

This is the argument for keeping a non-model baseline in the harness
permanently. Brute force did not know to delete anything, so its score was
honest by accident — and it was the comparison, not the absolute number, that
made the problem visible.
