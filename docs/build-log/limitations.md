# Known limitations

Written down so future-you doesn't rediscover them as "bugs". Each entry says
what's wrong, why it was accepted, and what would fix it.

## Hands cannot cross (v1 solver)
`assign_hands` penalises left-centroid > right-centroid heavily. Real pianists
cross constantly. Accepted because crossing is rare in the reduced textures
this project produces, and modelling it in a greedy solver produces worse
results than banning it. **Fix:** CP-SAT solver (milestone 3) handles it
naturally as just another feasible assignment.

## Greedy assignment is per-instant, not global
Hand assignment is decided one onset at a time with a continuity penalty. A
globally better assignment may exist that looks worse at instant 3. **Fix:**
same — CP-SAT over the whole piece.

## No finger assignment
Span and polyphony are checked; which finger plays which note is not. This
misses real infeasibilities like thumb-under passages at speed. **Fix:**
milestone 3. This is also what produces *infeasibility certificates* — proof
that no fingering exists, which is far stronger evidence for the repair agent
than "the greedy solver gave up".

## Pedal is not modelled
`leap_infeasible` does not know that a sustained pedal lets the hand leave a
low note early. Currently this produces false HARD violations on legitimate
romantic textures. **Mitigation:** `PedalSpan` exists in the plan schema; the
verifier must learn to read it before the corpus expands past classical.
**This is the highest-priority known false-positive source.**

## Tempo is assumed constant
Onsets are absolute seconds, so rubato and ritardando are baked in from the
transcription rather than modelled. Fine for verification, wrong for
difficulty estimation, which should reason about notated tempo.

## Rolled chords not yet distinguished
The renderer must stagger onsets for rolled chords so the verifier sees them
as non-simultaneous. Until it does, rolls will be reported as span violations.
