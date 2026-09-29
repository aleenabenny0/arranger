# Known limitations

Written down so future-you doesn't rediscover them as "bugs". Each entry says
what's wrong, why it was accepted, and what would fix it. Entries that have
been fixed are kept at the bottom, because the reasoning is still useful.

## Playability

### The solver searches, it does not prove
`verify.solver` is a shortest-path search over onset slices, phrase by phrase,
with a beam. When the beam or the time budget cuts the search short, the result
is `UNKNOWN` and every finding carries `certainty: unproven`; the interface says
"not proven: the search was cut short". An `INFEASIBLE` from a pruned search is
only reported when every surviving path dropped a note. **Fix:** a complete
search for short phrases, or an exact solver behind the same interface. It must
stay dependency-free to live in `arranger.verify`.

### No per-note fingering
The solver assigns hands, checks that a chord fits the fingers the profile
allows, and scores stretches between adjacent fingers from a fixed table. It
does not choose a finger for each note, so thumb-under passages, finger
substitution and repeated-note fingering are not modelled, and nothing prints
fingering. **Fix:** extend the state from "hand position" to "finger on each
held key". The state space grows a lot; it needs the phrase decomposition that
already exists.

### Human-written piano music still produces a few HARD findings
On the public-domain corpus, read as written, HARD findings fell from 401
(greedy) to 240 (solver, staves binding) to 152 (staves as a hint) to 21 (pedal
assumed each bar) to 8 (advanced profile). The remaining ones are real
notation the model does not understand: grace notes and arpeggiated chords
written as simultaneous, and redistribution between the hands that the staves
hide. This is why new rules default to STRAIN. **Fix:** read arpeggio and grace
marks from MusicXML into `Note.rolled`; MIDI cannot carry them.

### Half-pedalling and finger legato
Pedal is a span that is either down or up. A held bass under a change of
harmony with half-pedal is read as a release. `legato_break` is STRAIN for this
reason.

### The profile is a simplification of a hand
Reach is one number per hand. Real reach depends on which fingers, on black
versus white keys, and on what the other fingers are holding. The stretch table
is from the literature (Parncutt et al.), not from this player. Guided
calibration measures reach and speed; it does not measure stretch between
individual fingers.

## Analysis and arranging

### Melody extraction follows the most singable high line
It is a cheapest-path search with priors for track name, register and
continuity. It is wrong when the tune is in the tenor or bass, when two parts
swap, and in fugal textures. The source step lets the user name the melody
part, which always wins. It cannot name a melody that moves between parts.

### Chords are triads and sevenths from templates
No sus, add, altered or polychords; slash chords only through the detected
bass. Jazz harmony is reduced to its nearest seventh chord.

### The planner scores by re-rendering the whole piece
Every candidate costs one render and one verify of the full score, with at most
10 regions. A 10,000-note piece takes about 19 seconds. The planner has a stop
signal and gets half of the job's time budget; past that it keeps its safest
figures. **Fix:** score a candidate on its own region plus a bar either side.

### Fidelity saturates
`accompaniment` measures whether the left hand is present and how busy it is,
not how many notes survived. A waltz reduced from 80 notes to 36 can score
100%. The interface says so next to the figure, and the report lists what was
left out. `melodic_recall` is the one number that is a strict count.

### Difficulty is a heuristic
The 1-to-10 figure combines density, reach, leaps and hand independence. It has
not been calibrated against graded repertoire and is labelled as an estimate.

### Stride and broken tenths rarely win
They are offered at skill 7 and above behind tempo gates, and the verifier then
judges them like any other figure. On the corpus they have not yet been chosen:
walking bass and arpeggios score better against the same profile. That may be
right or may be the penalty table; it has not been studied.

## Notation

Output has notes, rests, ties, two voices per staff, triplets, key, meter and
tempo changes, pickup bars, pedal marks and rolled-chord marks. It does not
have grace notes, ornaments, slurs, dynamics, repeats, voltas, lyrics, chord
symbols, cross-staff beaming, or tuplets other than triplets. Rhythms that do
not fit the grid are moved to the nearest position and a warning says how many.
Every lossy step adds a line to the import or export warnings.

## Audio

- One instrument at a time. There is no source separation: a band is
  transcribed as if it were one piano.
- Accuracy has only been measured on synthesised audio (note F1 1.0 on a single
  line, 0.86 on two-hand texture). **No real recording has been measured.**
  Expect worse with pedal, reverb and soft playing. `docs/audio-transcription.md`
  has the method.
- Tempo is estimated and can be refused as untrustworthy; barlines are not
  inferred. The user sets tempo and meter in the source step.
- `basic-pitch` must be installed with `--no-deps`; the two-step install has not
  been tried on a clean machine other than through the Dockerfile in CI.

## Built, not verified against the real thing

| Piece | What has been tested | What has not |
|---|---|---|
| S3 artifact store | Signing against AWS's worked example; behaviour against a fake bucket | Any real bucket |
| Dockerfile | Reviewed; CI builds it and checks capabilities | A local build |
| Project workflow on Postgres | The same journey on SQLite; the older storage flow on Postgres | The journey on Postgres, until CI runs it |
| Model-assisted repair | Every branch with fake models: refusal, truncation, provider errors, cost and time caps | A real API call. None has been made. |
| Browser support | Edge/Chromium, desktop and phone width, light and dark | Firefox, Safari, real phones, real assistive technology |
| Backups, restore, rollback | Written in `docs/runbook.md` | Rehearsed |

## Service

- SQLite mode is for one host. Use Postgres for more than one.
- `ARTIFACT_BACKEND=database` stores files as rows; fine for MIDI, MusicXML and
  PDF, heavy for many long recordings.
- The in-browser player is a simple synthesiser, not a piano sample set, and it
  does not render pedal resonance.
- Note-level editing in the interface is limited to choosing parts,
  transposing, setting tempo and meter, and removing low-confidence notes. The
  API accepts `add`, `update` and `delete` edits; there is no piano-roll to make
  them with.

## Resolved, kept for the reasoning

- **Hands cannot cross / greedy per-instant assignment** (v1). Replaced by the
  phrase-based solver; crossing is allowed and reported as STRAIN
  (`hand_crossing`). `verify/hands.py` keeps the greedy version for comparison.
- **Pedal is not modelled.** Pedal spans are read from MIDI and from the plan's
  `pedal_bars`; held notes leave the hand free. `verify(..., assume_pedal=True)`
  exists for reading human scores that carry no pedal data.
- **Tempo is assumed constant.** `Timeline` carries tempo, meter and key maps;
  notes carry beats as well as seconds.
- **Rolled chords not yet distinguished.** The renderer staggers rolled chords
  and marks them; the verifier sees them as they are played.
