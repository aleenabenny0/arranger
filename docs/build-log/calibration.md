# Calibration log

The profile in `profiles/me.json` is the physical model the entire verifier
trusts. This file records where each number came from, so that when the
checker misbehaves, there is a documented list of which numbers to suspect
first instead of a guess.

## Round 1 — ruler, no instrument

No piano available. Measured hand span against a ruler and converted using
standard key geometry (white keys 23.5mm; C-to-C octave ≈ 16.4cm centre to
centre).

| Setting | Value | How it was derived | Confidence |
|---|---|---|---|
| `max_span` | 12 | 16cm measured ≈ one octave | Medium |
| `comfortable_span` | 9 | 14cm measured ≈ a 7th (11), rounded **down** to a 6th | Low |
| `max_notes_per_hand` | 5 | No injury or limitation | High |
| `max_leap_rate` | 50.0 | **Pure guess.** Cannot be measured without keys | Very low |
| `skill_level` | 2 | Self-assessed | Medium |

### Why comfortable_span was rounded down

14cm measures as a 7th, but the question asks what can be *held through a
whole phrase without aching* — not what can be reached once on a flat surface.
Curved fingers over keys reach less than a flat hand on a ruler, and passages
using black keys are harder again at the same interval.

The asymmetry matters: too tight produces arrangements slightly easier than
necessary, which is a mild annoyance. Too loose produces arrangements that
look fine on screen and hurt to play, which is how a tool stops getting used.
Erring tight is the cheap mistake.

### Why max_leap_rate is the weakest number

It describes how fast the hand relocates across the keyboard, which has no
ruler equivalent. 50 semitones/second is the middle option, chosen because
this rule is already the most likely source of false positives (the verifier
does not model the sustain pedal yet — see `limitations.md`).

**If the checker starts flagging music that is known to be playable, suspect
this number first.**

## Round 2 — at a real piano (TODO)

Outstanding checks, in priority order:

1. Time a clean two-octave leap. Replace the guessed `max_leap_rate`.
2. Hold an octave through a full phrase. If it strains, drop `max_span` to 11.
3. Hold a 6th, then a 7th, through a phrase. Set `comfortable_span` to
   whichever is genuinely sustainable.
4. Repeat 2 and 3 on black keys (F# to F#). Same intervals are harder there;
   if the difference is large, the profile may need a black-key penalty, which
   the model currently does not have.

## Validation status

**Not yet validated against real music.** The required test: take a piece
that can already be played, encode a few bars, and confirm the verifier
reports PLAYABLE. Until that passes, every number above is an estimate and
any verdict from the checker should be treated as provisional.

## Note on skill_level vs. span

These do different jobs and are expected to disagree. Span settings describe
what the hand *can* do; `skill_level` describes what the arrangement should
*aim* for. A capable hand attached to a beginning reader is a normal
combination, and produces sparse arrangements — block chords, simple left
hand — which is correct. Raise `skill_level` as reading improves without
touching the span numbers.
