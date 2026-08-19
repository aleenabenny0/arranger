# Arranger

Takes a recording, produces sheet music that **this specific player** can
actually play, at a difficulty they can actually handle.

## The one rule that everything else depends on

**The model emits an `ArrangementPlan`. It never emits notation.**

A deterministic renderer turns plans into MusicXML. If you find yourself
writing code where an LLM produces MusicXML, note names, or MIDI events
directly, stop — that design has been tried and rejected, and the reasons are
in `docs/build-log/why-plans-not-notes.md`.

The split: the model makes *musical judgement* (what to keep, how to voice it,
what the left hand does). Code guarantees *correctness* (well-formed output,
notes on the page, spans within reach). Judgement is what models are good at.
Correctness is what they are unreliable at. Do not mix them.

## Architecture

```
audio → separate → transcribe → clean → analyze
                                          ↓
                                    ArrangementPlan ──→ render ──→ verify
                                          ↑                          │
                                          └──── repair ←─────────────┘
                                              (max 4, then escalate)
```

| Package | Role | May depend on |
|---|---|---|
| `arranger.ir` | Note/Score data model | stdlib only |
| `arranger.profile` | The player's physical limits | stdlib only |
| `arranger.verify` | **The oracle.** Playability constraints | stdlib only |
| `arranger.io` | MusicXML/MIDI loaders | music21, pretty_midi |
| `arranger.plan` | ArrangementPlan schema | pydantic |
| `arranger.render` | Plan → MusicXML → PDF | music21, LilyPond |
| `arranger.audio` | Separation, transcription, fidelity | demucs, basic-pitch, librosa |
| `arranger.agent` | LangGraph orchestration | langgraph |

**`arranger.verify` has zero third-party dependencies and must stay that way.**
It is the component every other component's correctness is measured against.
It cannot be allowed to break because a library changed under it.

## Working rules

- Constraint changes require a test for the violating case **and** a test for
  a near-miss that must stay clean. False positives are the expensive failure
  mode here: they make the agent damage music that was already fine, and you
  will blame the model instead of the rule.
- New rules default to `STRAIN`, not `HARD`. Promote to `HARD` only after
  checking it against real human-made arrangements in `evals/corpus/`.
- Never widen a profile's limits to make a test pass. The profile describes a
  human being. If the arrangement doesn't fit, the arrangement is wrong.
- Every `Violation` must carry `measured`, `limit`, and enough of `pitches`
  /`bar`/`hand` for the repair agent to act without re-deriving anything.
- Run `python tests/test_constraints.py` before proposing any change to
  `verify/`. It runs in under a second; there is no excuse.

## Musical conventions

- Enharmonic spelling follows key context, always. Never emit a note name
  without knowing the key — that is the engraver's job, not the reducer's.
- Voice leading beats voicing prettiness. A smooth inner line is worth more
  than a fuller chord.
- The melody is sacred. `melodic_recall` below 0.9 is a failed arrangement no
  matter how good the harmony score looks.
- When reducing, drop doublings first, then inner voices, then bass movement,
  then harmonic colour. Never drop the melody, and never drop the root of a
  chord that establishes a key change.

## Corpus licensing

The eval corpus is public-domain and Creative Commons audio only (IMSLP,
Musopen, ccMixter). This keeps the benchmark redistributable, which is what
makes it a benchmark rather than a demo. Do not add commercial recordings to
`evals/corpus/`, even locally — it always leaks into a commit eventually.

## Current state

Milestone 1 complete: the verifier works and is tested.
Next: MusicXML loader (`arranger.io`), so real scores can be checked.
Known limitations live in `docs/build-log/limitations.md` — read it before
concluding that a bug is new.
