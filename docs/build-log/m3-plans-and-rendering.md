# M3: plans and the renderer

The layer that makes an agent possible. The model will emit an
`ArrangementPlan`; a deterministic renderer turns that into notes; the
verifier checks them. This entry covers building the middle step.

## Result

Hard violations, source vs. best hand-searched plan:

| File | Source | Arranged | Best plan found |
|---|---|---|---|
| Bohemian Rhapsody | 2468 | **51** | walking bass, 1 voice |
| Für Elise | 17 | **12** | block, 1 voice |
| Unravel | 3 | **0** | block, 1 voice |

Bohemian Rhapsody is the headline: a fourteen-track band arrangement reduced
to two playable hands, a 98% reduction. That number is the baseline the agent
must beat — and beating it is not guaranteed, because the "best plan" above
was found by brute force over every pattern and voicing. The agent's job is
to reach comparable results *with musical reasoning* on pieces where brute
force is too coarse, and to produce plans a human would want to play.

## The first render made everything worse

Für Elise 17 → 227. Bohemian Rhapsody 2468 → 4071. Three bugs, found by
printing the notes sounding at the first violation rather than guessing.

### Bug 1 — the melody dived into the accompaniment

`extract_melody` took the highest sounding note at every onset. That reads
correctly until the melody rests: then the highest sounding note is an
accompaniment note two octaves down, so the "melody" plunges into the bass and
back. The result was a right hand spanning fifteen semitones and leaping
constantly.

Fix: a floor. Notes more than nine semitones below the median top note are
accompaniment, not melody. A rest in the melody is a rest — not an excuse to
grab whatever is lowest.

### Bug 2 — duplicate melody notes

The same pitch appeared at 3.233 and 3.238. The merge check compared the new
onset against the previous note's *end*, so a 400ms note re-triggering 5ms
later looked like a separate note rather than an overlap.

Fix: compare against the previous note's extent, not its end. Overlapping
means duplicate.

### Bug 3 — left-hand chords bled across barlines

Bar spans were derived from note durations, which under sustain run well past
the barline. Every left-hand chord therefore overlapped the next one, and the
accompaniment stacked on itself. Fix: clip each bar where the next one starts.

After all three: Bohemian Rhapsody 4071 → 61.

## The chord detector had a musical bug

`test_chords_are_detected_per_bar` failed: G-B-D under an E melody was
detected as E minor 7 rather than G major.

Not unreasonable — the two chords share pitches, and Em7 explains the melody E
as well. But the bass is G, and the bass is what decides a root. A chord
symbol exists to tell the left hand where to sit, so a wrong root is the one
error in this module that actually matters.

Fix: a bonus when the candidate root matches the bar's lowest pitch class.
The chord detector now weights bass over pitch-class coverage.

## Plan choices measurably matter

Sweeping every left-hand pattern, violations range from 0 to 206:

| Pattern | Für Elise | Bohemian Rhapsody | Unravel |
|---|---|---|---|
| block | 12 | 61 | 0 |
| pedal_tone | 12 | 59 | 0 |
| walking | 17 | 59 | 2 |
| arpeggio | 28 | 91 | 13 |
| alberti | 31 | 91 | 7 |
| broken_octave | 206 | 200 | 87 |

This is the check that the plan layer is not decorative. If every pattern
produced similar counts, the model's choices would be theatre and the whole
design would be pointless. A 200-violation spread means the decisions are
real.

`broken_octave` being worst is a good sanity signal: broken octaves force the
hand to cross an octave repeatedly at tempo, and they *are* the hardest of
these patterns to play. The verifier independently rediscovered something
every pianist knows, which is weak evidence that its physical model is
tracking reality.

## A feature that does not work yet

`roll_wide_chords` was meant to be the cheap fix for hand-span violations:
rolled notes are not simultaneous, so the span rule should stop applying.
Measured, it makes things *worse* (Bohemian Rhapsody 61 → 70).

The reason: the implementation staggers onsets by 30ms but leaves the notes
sustaining, so they still overlap and the span rule still fires — while the
extra onsets create extra leap checks. Modelling a roll properly requires the
verifier to understand that a rolled chord is held by the pedal rather than
the fingers, which is the same gap as `limitations.md`'s pedal entry.

Left in the schema, documented as ineffective. A plan may set it; it will not
help yet.

## What survives

Für Elise's remaining 12 and Bohemian Rhapsody's 51 are almost entirely
**right hand**: melody spans of 14-19 semitones and fast melodic leaps. The
left hand is essentially solved; the melody is not.

That is the correct next problem, and it is a *plan-level* one — melodies
exceeding a hand need octave displacement at phrase boundaries, which is
exactly the kind of judgement call the model should be making and the brute
force search cannot express.

## Test count

19 render tests, 18 verifier tests. The two that matter most:

- `test_patterns_produce_different_output` — guards against the plan layer
  quietly becoming a no-op
- `test_melody_is_never_dropped` — enforces the rule in CLAUDE.md across every
  pattern, so a future left-hand change cannot silently eat the tune
