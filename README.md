# Arranger

Audio in, playable-for-you sheet music out.

You want to play a song. There's no sheet music, or what exists is written for
a different instrument, or it's three grades above you. Arranger transcribes
the recording, arranges it for *your* hands at *your* level, and verifies the
result is physically playable before you ever see it.

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
- [ ] **M2** MusicXML/MIDI loaders, deterministic renderer
- [ ] **M3** CP-SAT fingering solver with infeasibility certificates
- [ ] **M4** Eval harness + baseline, 20-piece public-domain corpus
- [ ] **M5** LangGraph agent, subagents, bounded repair loop
- [ ] **M6** Audio front end: separation, transcription, harmonic fidelity

## Design notes

- `docs/build-log/why-plans-not-notes.md` — why the model never emits notation
- `docs/build-log/limitations.md` — what's knowingly wrong and what fixes it
- `CLAUDE.md` — working rules for agents in this repo
