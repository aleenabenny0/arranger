# M2: the first run against real music

Three MIDI files, one profile (max_span 12, comfortable 9, leap_rate 50,
skill 2). This is the first time the verifier saw music it was not designed
against. Numbers below are hard violations only.

| File | Notes | Hard | Verdict |
|---|---|---|---|
| Tokyo Ghoul – Unravel | 279 | 0 | Correct |
| Für Elise | 1575 | 69 | Mostly false positives |
| Bohemian Rhapsody | 4089 | 2489 | Correct, and the point of the project |

## Unravel: 0 violations

A single-track melody line, no chords, range B4–G6. Nothing to flag, and
nothing was flagged. The useful information here is negative: the verifier
does not fire on music that is genuinely easy, so it is not simply
pessimistic about everything.

## Bohemian Rhapsody: 2489 violations, all correct

Fourteen tracks — full band, guitars, bass, drums, layered vocals. Two hands
cannot play a band, so "unplayable" is the right answer.

This file is not a failure case. It is the *use case*. Turning something like
this into two playable staves is the entire purpose of the project, and the
2489 violations are a measurement of how much reduction the arranger will
have to do. It doubles as a baseline: whatever the agent eventually produces
has to get this to zero.

## Für Elise: the actual bug

Für Elise is playable by an intermediate pianist, so 69 hard violations means
the verifier is wrong. Two distinct causes, found by tracing hand assignment
through the opening bars.

### Cause 1 — duplicate notes (fixed)

25% of the notes in the file were exact duplicates: the same pitch at the same
tick on two different tracks. Downloaded MIDI routinely stacks layers this way
for a fuller synth sound.

A piano has one key per pitch. A doubled note is not two notes. Left in, the
duplicates inflated every polyphony count — six "fingers" needed for a
three-note chord.

Fixed in `io.py` by deduplicating on `(pitch, tick)` at load time. Effect:

- Für Elise 129 → 69 hard violations
- hand_polyphony 63 → 10

### Cause 2 — the melody flips between hands (open)

The remaining 59 leap violations are one bug wearing many hats. Tracing the
opening:

```
t=0.033  E5:R      L@--  R@76
t=0.238  D#5:L     L@75  R@76
t=0.433  E5:R      L@75  R@76
t=0.633  D#5:L     L@75  R@76
```

That is the famous E–D#–E–D# opening, which is one hand playing one line. The
solver is alternating it between hands.

Why: cost is computed per instant, and an *empty* hand contributes zero
continuity cost. For a lone note at pitch 75, keeping it in the right hand
costs `|75 − 76| = 1`; moving it to the idle left hand costs `|75 − 75| = 0`.
The left hand wins by one point, every other note, forever.

The damage shows up later. At t=1.633 the real left hand enters on A2. Because
the solver had just labelled the melody note C5 as "left hand", it reports the
left hand travelling 27 semitones in 196ms — a leap that never happened. The
hand did not move; the label did.

**This is not a tuning problem.** No value of `max_leap_rate` fixes a
mislabelled hand, and loosening it would only hide real violations. The
profile numbers are not implicated.

### Why this was predicted

`limitations.md`, written before any real music was loaded, lists "greedy
assignment is per-instant, not global" and notes that the fix is a CP-SAT
solver deciding the whole piece at once. This run is the empirical evidence
for that entry — the failure mode is exactly the one anticipated, and it is
now measured rather than theorised.

### What was *not* the cause

The pedal gap was expected to be the main false-positive source. It was not
implicated in any violation examined here. Still a real gap, but demoted
below hand assignment in priority.

## Next

1. **Hand-assignment hysteresis** (cheap): penalise moving a line to the
   other hand when the current hand can reach it. Should clear most of the 59.
2. **CP-SAT solver** (M3): solve assignment globally instead of per instant.
   The proper fix, and it produces infeasibility certificates.
3. **Pedal spans**: still open, still unmeasured.

## Method note

The profile numbers were never adjusted during this investigation. When real
music gets flagged, the instinct is to loosen constraints until the complaints
stop — which would have "fixed" Für Elise while silently disabling the leap
rule everywhere. Tracing to the cause first meant finding a data bug and a
solver bug, neither of which had anything to do with the constraints.
