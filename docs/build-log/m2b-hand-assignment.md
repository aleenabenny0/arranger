# M2b: fixing the false leap violations

Starting point: Für Elise reported 69 hard violations, 58 of them leaps, for a
piece an intermediate pianist can play. Three separate causes were found. Only
one of them was the cause originally suspected.

| Stage | Für Elise | Unravel | Bohemian Rhapsody |
|---|---|---|---|
| After M2 dedupe | 69 | 0 | 2489 |
| + idle-hand cost | 68 | 3 | 2489 |
| + per-hand leap timing | 17 | 3 | 2468 |
| + leap rate 50 → 90 | **11** | **0** | 2435 |

Für Elise: 69 → 11, an 84% reduction. Bohemian Rhapsody barely moved, which is
correct — it is a fourteen-track band arrangement and genuinely unplayable by
two hands.

## Cause 1 — a melodic line alternating between hands

Traced by printing hand assignment across the opening bars:

```
t=0.033  E5:R      L@--  R@76
t=0.238  D#5:L     L@75  R@76
t=0.433  E5:R      L@75  R@76
t=0.633  D#5:L     L@75  R@76
```

That is one hand playing one line, split across two.

The cost function charged `|new_position − old_position|` per hand, but an
*idle* hand contributed nothing. For a lone note at pitch 75, keeping it in
the right hand cost `|75 − 76| = 1`; giving it to the idle left hand cost 0.
The left hand won by one point, every other note.

Fix: `IDLE_HAND_COST`, a price for waking a resting hand. The melody now stays
in one hand for the entire piece.

**Effect on the violation count: 69 → 68.** Almost nothing. The fix was
correct and necessary, and it was not what was driving the number.

This is worth recording precisely because it is unsatisfying. A correct fix
that does not move the metric is easy to mistake for a wrong fix, and easy to
revert.

## Cause 2 — the leap budget used the wrong clock

The real problem, found by printing what surrounded each violation:

```
bar 4  LH moves 11 st, budget 5.2
  from t=4.033: C5
  to   t=4.038: A2 C5
```

Five milliseconds to move eleven semitones — impossible, and reported as such.
But the note at 4.033 was a *right hand* note. The left hand had been resting
for far longer and had plenty of time to arrive.

The budget was computed as `time since the previous onset by anybody`. It
should have been `time since this hand last played`. A hand that rests while
the other hand plays four fast notes gets all four notes' worth of time to
move.

Fix: track `last_played` per hand and measure each hand's budget from its own
last note.

**Effect: 68 → 17.** Leap violations 58 → 7.

## Cause 3 — the guessed parameter really was too low

The seven survivors were all marginal: 12 semitones against a budget of 10.2,
17 against 14.8. Octave leaps at ordinary speed.

`max_leap_rate` was set to 50 semitones/second by guessing, because there was
no piano available to measure it — flagged in `calibration.md` as the weakest
number in the profile and the first thing to suspect.

Sensitivity sweep (total hard violations, leaps in brackets):

| rate | Für Elise | Bohemian Rhapsody | Unravel |
|---|---|---|---|
| 50 | 17 (7) | 2468 (235) | 3 (3) |
| 70 | 13 (3) | 2445 (212) | 0 (0) |
| 90 | 11 (1) | 2435 (202) | 0 (0) |
| 110 | 11 (1) | 2425 (192) | 0 (0) |
| 140 | 11 (1) | 2390 (157) | 0 (0) |

The curve flattens at 90 and stays flat to 140. That flat region is the useful
signal: within it, the answer stops depending on the exact value, so the
parameter is no longer doing the work. Below 90 the results are sensitive to a
number that was never measured.

**90 is the recommended value**, as the low end of the stable region — high
enough to stop generating false positives, low enough to still catch real
ones. It remains an estimate until timed at an actual keyboard.

## Was this just tuning until the complaints stopped?

Nearly, and the order matters. Had the rate been raised first, Für Elise would
have gone quiet and both structural bugs would still be there — silently
producing wrong hand assignments, waiting to appear as a mystery later.

Raising it was only legitimate after the two real bugs were fixed and every
remaining violation was marginal rather than structural. A 24-semitone
violation against a 10-semitone budget is a bug. A 12 against 10.2 is a
parameter question.

The distinction that made it safe: **the sweep flattens.** If violations had
kept falling as the rate rose, that would mean the rule was simply being
switched off. A flat region means the remaining violations are robust to the
parameter, so the parameter can be set anywhere inside it.

## A test was reversed

`test_wide_leap_is_allowed_when_the_other_hand_is_free` asserted that a fast
wide leap should be excused when the other hand is idle. `IDLE_HAND_COST`
makes that false.

The test was rewritten rather than the code, and the reasoning recorded in the
test body. A per-instant solver cannot know whether the other hand is free
across a whole phrase — only at this instant. Für Elise demonstrated that
guessing "free" by default costs far more than it saves.

This is a real capability loss: genuine two-hand rescues in sparse passages
are now flagged. It is the correct trade at this stage and it is the strongest
argument for the M3 global solver, which decides assignment across the whole
piece and does not have to guess.

## Remaining in Für Elise: 11

Ten `hand_polyphony`, one leap. Not yet investigated. The likely cause is
sustain: MIDI note durations reflect the pedal, so an arpeggio held under
pedal looks like six fingers pressing at once when it is one hand rolling
through six notes.

That is the pedal gap from `limitations.md` — still unfixed, but now the
largest remaining source rather than a speculative one.
