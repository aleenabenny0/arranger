# Scorecard

**Status as of 2026-08-24: no verdict yet.** This file is normally
generated wholesale by `arranger.evaluate` (`args.out.write_text(...)`) —
what's below is a hand-written status snapshot of the "does the agent beat
brute force" effort, not scorecard output. Running
`python -m arranger.evaluate` against the pool named below, per
`docs/build-log/eval-protocol.md`, will overwrite this file with the real
thing. Until that happens, this is what's established and what's still
open.

## Established

**1. The texture/boundary finding (19 candidates measured, real
`brute_force_baseline` runs, no API calls needed).** Neither
"multi-instrument," "orchestral," nor "many independent voices" predicts
whether a piece resists brute force's 54-plan exhaustive search — all
three were tried and falsified. SATB chorales, a Baroque concerto grosso
(BWV1043), a full Romantic-scale orchestral symphony with no soloist at
all (*Eroica*, all four movements), and a string quartet (Haydn Op.76
No.1) all reduce to cost 0.00 via the same escape hatch: `block,
voices=1` — one bass note per chord, melody folded by octaves, nothing
else. What resists is narrower and more specific: **a genuinely
virtuosic, wide-ranging concerto solo line pitted against orchestral
accompaniment**, where the remaining violations are in the melody itself,
not the accompaniment, and folding by whole octaves can't fix a leap-rate
problem. Full writeup, including the two file-identity gaps that are
flagged rather than resolved: `evals/corpus/README.md`, "Sourcing
criteria" section.

**2. A verdict-eligible pool now exists and meets the pre-registered
minimum.** `docs/build-log/eval-protocol.md`'s original 5-candidate pool
fell short (2/5 qualified); its own pre-registered fallback — source more
hard material, don't lower the bar — was executed. Result, per the
2026-08-24 amendment to that document:

| piece | composer | baseline cost |
|---|---|---|
| `beethoven-piano-concerto3-1` | Beethoven | 3.00 |
| `beethoven-violin-concerto-1` | Beethoven | 2.00 |
| `tchaikovsky-op35-2` | Tchaikovsky | 4.00 |

3 pieces, 2 composers — exactly the minimum pool size the protocol
requires before it will produce a PASS/FAIL verdict instead of
"insufficient data." A fourth candidate (`tchai_op35.mid`, unlabelled,
cost 5.00) was deliberately excluded — its relationship to
`tchaikovsky-op35-2` was never established, and counting both risked one
underlying work casting two votes in a 3-piece pool. Reasoning in the
amendment.

**3. The 20-piece corpus (`evals/corpus/`) cannot test this claim at
all** — brute-force cost is 0.00 on all 20 of its pieces. It remains
useful for regression checks, difficulty calibration, and exercising the
reduction heuristics on real, licensed material; it is not evidence for
or against "beats brute force," and was never going to be once every
piece in it turned out to have that property.

**4. `fetch_corpus.py` supports zip-sourced pieces now** (`zip_url` +
`zip_member`, stdlib `zipfile`, no new dependency) — this is what made
sourcing the *Eroica*/BWV1043/Haydn/Tchaikovsky material possible in the
first place; most of it only exists on Mutopia as zip members.

## Not yet done

- **No real agent run has happened against the 3-piece pool.** Every
  number above comes from `arranger.agent.brute_force_baseline` — a
  deterministic search, no model involved, no API key needed. The actual
  question this protocol exists to answer (does `arranger.agent.arrange`,
  running Claude, beat that baseline) has not been tested. No
  `ANTHROPIC_API_KEY` was available in the environment this was written
  in.
- ~~The 3 pool pieces aren't `manifest.json` entries yet.~~ **Done
  (2026-08-24):** all 3 are real `manifest.json` entries now — checksummed,
  licensed, fetched and verified through the actual `fetch_corpus.py` path
  (`python fetch_corpus.py beethoven-piano-concerto3-1
  beethoven-violin-concerto-1 tchaikovsky-op35-2`), same discipline as the
  20-piece corpus. This was the last blocker to running the eval
  reproducibly; the only thing left is the actual API-key-requiring run.
- **Two file-identity gaps, both named in `evals/corpus/README.md` rather
  than resolved:** Haydn Op.76 No.1's 4th movement (Finale) was never
  retrieved — no `score-4.mid` exists under this zip's naming. And
  `tchai_op35-1.mid` (labelled "movement 1") is too sparse — 1,584 notes
  over 119 bars — to plausibly be a real ~20-minute concerto first
  movement; its 0.00 baseline shouldn't be read as "the first movement
  doesn't resist" without more confidence in what the file contains.
- **The texture/boundary finding is N=4-positive / N=15-negative across
  2 composers.** Real signal — strong enough to source by, not yet strong
  enough to claim as a general rule about Romantic concertos. A targeted
  search for 5 more named composers (Mendelssohn, Brahms, Liszt,
  Rachmaninoff, plus 3 extras) found real concerto MIDI for exactly one
  of them on Mutopia; broadening past that ceiling means either accepting
  fewer composers or widening the source beyond Mutopia, which reopens
  the licensing/reproducibility tradeoffs the corpus was built to avoid.

## Historical numbers (real runs, predate this protocol, not in the current pool)

These came from real Claude API calls, on `samples/Queen - Bohemian
Rhapsody.mid` — not public-domain, not in `evals/corpus/`, no recorded
checksum. Kept for reference; not reproducible by anyone else cloning
this repo, and not part of the verdict pool above for that reason (see
`eval-protocol.md`'s "Bohemian Rhapsody: run, but never counted" section
— the same reasoning that excludes it from the new pool predates the new
pool and was written for exactly this file).

**4 attempts per run, 8 repeats:**

| piece | source | baseline | median | spread | accepted | attempts | beat baseline |
|---|---|---|---|---|---|---|---|
| Queen - Bohemian Rhapsody | 2435 | 1.00 | 5.50 | 0.0-12.0 | 1/8 | 4 | 1/8 |

1/8 runs accepted. 1/8 beat the brute-force baseline. 91,692 output tokens.

**8 attempts per run, 8 repeats** (`SCORECARD_8attempts.md`, unchanged,
kept as its own file):

| piece | source | baseline | median | spread | accepted | attempts | beat baseline |
|---|---|---|---|---|---|---|---|
| Queen - Bohemian Rhapsody | 2435 | 1.00 | 0.00 | 0.0-7.0 | 7/8 | 5 | 7/8 |

7/8 runs accepted. 7/8 beat the brute-force baseline. 114,769 output
tokens.

The gap between these two rows (1/8 vs 7/8, same piece, same repeats,
different attempt budget) is itself a finding from
`docs/build-log/m7-deadline-effect.md` — worth remembering before reading
too much into any single win rate, including the ones this protocol will
eventually produce.
