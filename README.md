# Arranger

MIDI in, playable-for-you sheet music out.

You have a MIDI file of a song — a full-band arrangement, an orchestral
reduction, whatever you could find — and you want to play it on piano. It
isn't written for your instrument, or it's three grades above you, or it just
doesn't fit your hands. Arranger reduces it to two hands, arranges it for
*your* level, and verifies the result is physically playable before you ever
see it.

Turning an audio recording into that starting MIDI isn't built yet — see
Roadmap below.

## The interesting part

Playability is not a matter of opinion. Whether a chord fits under a hand,
whether a leap is possible in the time available, whether a passage needs more
fingers than you have — these are constraints, and constraints can be checked
by a machine.

So the LLM never writes notation. It emits an **arrangement plan** — what to
keep, how to voice it, what the left hand does — and a deterministic renderer
turns that into a score. A dependency-free verifier then checks the result
against a physical model of one specific player and returns a structured
verdict. Failures go back to the agent as numbers it can act on, not prose it
has to interpret.

The model does judgement. Code does correctness.

## Try it

```bash
git clone <your-repo> && cd arranger
PYTHONPATH=src python3 tests/test_constraints.py
PYTHONPATH=src python3 -m arranger.verify.cli \
    tests/fixtures/too_hard.json --profile profiles/me.json
```

```
NOT PLAYABLE  —  4 hard, 0 strain
  [bar 1] LH must span 19 semitones (C2-G3); max is 12. Drop an inner voice
          or move one pitch an octave.
  [bar 3] LH must move 27 semitones in 50ms; feasible budget is 9. Sustain
          the lower note with pedal, or re-voice so the hand stays put.
  [bar 3] D8 is outside the playable range A0-C8
```

No dependencies required for the above. That's deliberate — see `CLAUDE.md`.

## Your hands, in a file

`profiles/me.json` is the physical model. Every number is measurable at a
piano in under a minute:

```json
{ "max_span": 12, "comfortable_span": 9, "max_leap_rate": 70.0, "skill_level": 5 }
```

`max_span` is the widest block chord you can hold. `max_leap_rate` is how fast
your hand relocates, in semitones per second — play a two-octave leap cleanly
and time it. Change these and the same piece becomes playable or doesn't.

## Status

- [x] **M1** Playability verifier + profile model (17 tests, zero deps)
- [x] **M2** MIDI loader, deterministic renderer (MusicXML input/output still open — see Roadmap)
- [ ] **M3** CP-SAT fingering solver with infeasibility certificates
- [x] **M4** Eval harness + baseline, 20-piece public-domain corpus (`evals/corpus/`, `python fetch_corpus.py`)
- [x] **M5** Bounded repair loop, calls the Claude API directly (LangGraph orchestration still open — see Roadmap)

## Open question: does the agent beat brute force?

`arranger.agent.arrange` is supposed to earn its cost against
`brute_force_baseline` — an exhaustive search over 54 fixed plans with no
model involved (see `arranger.agent`'s own docstring). Testing that claim
turned into real work of its own, tracked in `SCORECARD.md` rather than
here. Short version:

- **Established:** the 20-piece public-domain corpus (`evals/corpus/`)
  can't test this at all — brute force already reaches cost 0.00 on every
  piece in it. What actually resists brute force turned out not to be
  "orchestral" or "multi-instrument" (both tested and falsified against
  real orchestral and chamber scores) but specifically *virtuosic Romantic
  concerto solo writing* — confirmed on 2 composers so far. Full writeup:
  `evals/corpus/README.md`, "Sourcing criteria" section.
- **Open:** a 3-piece verdict-eligible pool now exists and meets the
  pre-registered minimum (`docs/build-log/eval-protocol.md`), but no real
  agent run has happened against it yet — every number so far comes from
  the deterministic baseline search, not from Claude. `SCORECARD.md` has
  the current pool, what's missing before the eval can run
  reproducibly, and the historical (pre-protocol, non-reproducible) numbers
  from `samples/Queen - Bohemian Rhapsody.mid` for context.

## Roadmap

Not built yet:

- **Audio front end.** Turning a recording into a starting score — source
  separation, pitch transcription, harmonic-fidelity scoring against the
  original audio. `demucs`, `basic-pitch`, and `librosa` are listed as an
  optional `audio` extra in `pyproject.toml`, but nothing in `src/` imports
  them yet. Every run today starts from a MIDI file.
- **MusicXML input and engraved (PDF) output.** `arranger.io` reads MIDI
  only; `arranger.render` produces an internal score, not printable notation.
- **CP-SAT hand/finger solver.** `arranger.verify.hands` is a fast greedy
  assignment today — right most of the time, but it cannot model hand
  crossing and doesn't produce the infeasibility certificates a CP-SAT
  solver would.
- **LangGraph orchestration.** The repair loop in `arranger.agent` is a
  plain Python loop calling the Claude API directly, not a LangGraph graph.

## Design notes

- `docs/build-log/why-plans-not-notes.md` — why the model never emits notation
- `docs/build-log/limitations.md` — what's knowingly wrong and what fixes it
- `docs/build-log/eval-protocol.md` — pre-registered design for the "does
  the agent beat brute force" eval, decided before results came back
- `evals/corpus/README.md` — corpus licensing, and what actually resists
  brute force (measured, not guessed)
- `CLAUDE.md` — working rules for agents in this repo
