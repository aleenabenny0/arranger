---
name: left-hand-patterns
description: Choosing a left-hand accompaniment figure when arranging for piano — block chords, arpeggios, Alberti bass, stride, pedal tones, and broken tenths. Use this whenever writing or repairing the lh_pattern field of an ArrangementPlan, when a left hand fails hand_span or leap_infeasible verification, or whenever deciding how an accompaniment should be realised for a given genre, tempo, and skill level.
---

# Left-hand patterns

The left hand carries harmony, bass, and pulse simultaneously. Choosing its
pattern is the single highest-leverage decision in a piano arrangement: it
sets difficulty, genre, and whether the piece sounds thin or full, and it
determines whether the hand can physically keep up.

## Selection table

| Pattern | Tempo | Difficulty | Span needed | Sounds like |
|---|---|---|---|---|
| `block` | any | 1-3 | chord width | Hymn, simple pop |
| `pedal_tone` | any | 1-2 | single note | Ambient, film |
| `broken_octave` | slow-mid | 3-4 | 12 | Ballad |
| `arpeggio` | slow-mid | 4-6 | 12-14 | Romantic, ballad |
| `alberti` | mid-fast | 4-5 | 9-12 | Classical era |
| `broken_tenth` | slow | 6-7 | 16 (rolled) | Jazz, gospel |
| `stride` | mid-fast | 7-9 | 12 + fast leaps | Ragtime, jazz |
| `walking` | mid | 5-6 | single note | Jazz, blues |

## Rules that prevent most failures

**Tempo gates the pattern.** Stride requires the hand to cross an octave-plus
every beat. At 160bpm that is a 24-semitone move in under 200ms, which fails
`leap_infeasible` for most profiles. Check `max_leap_rate` against the actual
distance before choosing stride or broken tenths.

**Rolled chords buy span.** A 10th that fails `hand_span` as a block is fine
rolled — the notes are no longer simultaneous, so the constraint does not
apply. This is the cheapest fix for a span violation and it usually sounds
*better* in ballads. Mark it in the plan as `roll: true`; the renderer emits
the arpeggiate mark and staggers the onsets so the verifier sees the truth.

**Pedal tones rescue impossible passages.** If the left hand must be in two
places, put the low note under a pedal span and let the hand leave. The sound
persists; the hand does not need to.

**Match the era, then break it deliberately.** Alberti bass in a modern pop
ballad sounds like a MIDI file. Block chords in a ragtime sounds like a
mistake. When deviating from the table, say why in the plan's rationale.

## Fixing specific violations

- `hand_span` on a block pattern → roll it, or switch to `broken_octave`
- `leap_infeasible` on stride → drop to `broken_octave`, or halve the leap by
  using the 5th instead of the root in the bass
- `hand_polyphony` → shell voicings: root and 7th only, let the right hand
  supply the 3rd

## Skill-level ceilings

Do not exceed the profile's `skill_level` by more than one:

- level 1-3 → `block`, `pedal_tone`
- level 4-6 → add `arpeggio`, `alberti`, `broken_octave`, `walking`
- level 7+ → add `stride`, `broken_tenth`

An arrangement one level above the player is a good practice target. Three
levels above is an arrangement they will abandon.
