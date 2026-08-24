# Eval protocol: does the agent beat brute force?

Written before running it, against pieces whose brute-force baseline hasn't
finished computing yet. That ordering is the whole point: every threshold
below was picked without knowing which candidates pass or fail it, which is
the only way "beats brute force" gets to mean something instead of being
fitted after the fact. `arranger.evaluate`'s own module docstring exists
because single-run conclusions are anecdote (three identical runs on one
piece produced 8, 8, and 16 violations before any feedback existed); this
document is what stops multi-run conclusions from becoming anecdote with
extra steps.

## Why this is needed

The 20-piece corpus in `evals/corpus/` turned out to have brute-force
baseline cost 0.00 on all 20 pieces (see `evals/corpus/README.md`) — nothing
in it can demonstrate the claim at all. The candidates being sourced now
(Bach four-part chorales, Beethoven concerto movements — genuinely
multi-instrument, not solo-piano-idiomatic) are chosen to fix that. But
"chosen because they're hypothesized to be hard" is piece curation, not
evidence. Whether they actually qualify, and what counts as beating brute
force once they do, has to be fixed now.

## Piece pool and eligibility

**Verdict pool:** the 5 candidates currently being fetched —
`bach-o-haupt`, `bach-nun-komm`, `bach-bwv259`,
`beethoven-piano-concerto3-1`, `beethoven-violin-concerto-1`. These, and
only these, can contribute to the corpus-level pass/fail verdict.

**Eligibility rule, fixed now:** a piece qualifies for the verdict iff
`brute_force_baseline()` — the existing, unmodified 54-plan exhaustive
search in `arranger.agent` — returns `cost > 0` on it, measured against
`profiles/me.json`. Nothing else changes this: not the piece's raw
`baseline_hard`, not how interesting the piece looks, not how the eval
turns out. A piece with baseline cost 0 gets run (regression value, same
as the existing corpus) but is excluded from the verdict, exactly like
`evaluate.py`'s scorecard already documents.

**Minimum pool size:** if fewer than 3 of the 5 candidates qualify, the
eval does not produce a pass/fail verdict at all — it produces
"insufficient data," and the next step is sourcing more hard
multi-instrument material (unlocking zip-archive handling to reach Eroica
/ the Bach double violin concerto / the Haydn quartets is the obvious next
lever), not lowering the bar to make 3 out of fewer pieces, and not
reaching into Bohemian Rhapsody to make up the count (see below).

**Bohemian Rhapsody: run, but never counted in the verdict — decided now.**
`samples/Queen - Bohemian Rhapsody.mid` gets run under this protocol's
exact fixed parameters (same model, profile, attempts, repeats) so its
numbers are directly comparable in kind to the candidate pool, and it is
reported as its own row, separately labelled. But it does not count toward
eligibility-pool size, the per-piece majority tally, or the corpus-level
majority — for three reasons, all of which hold regardless of what its
own baseline turns out to be under this run:

1. **Not reproducible.** `samples/` is gitignored and the file has no
   recorded source, license, or checksum (see `evals/corpus/README.md`,
   "`beethoven-fur-elise-woo59` deliberately duplicates..."). Nobody else
   cloning this repo can obtain this exact file, so a verdict that depends
   on it isn't reproducible by anyone but the person who already has it.
   A "corpus-level" claim should be about the corpus — the reproducible,
   licensed thing `fetch_corpus.py` can actually hand to a stranger — not
   about corpus-plus-one-private-file.
2. **Not licensed.** It's a commercial recording's transcription. Per
   CLAUDE.md's corpus licensing rule it can never be part of
   `evals/corpus/`'s manifest. Letting it vote in the same tally as
   properly-licensed pieces would mean the published protocol's headline
   verdict is partly load-bearing on a file the project's own rules forbid
   distributing.
3. **It isn't a blind data point.** Two prior runs already exist on it
   (`SCORECARD.md`: 1/8 beat baseline; `SCORECARD_8attempts.md`: 7/8) —
   this eval already has priors about how it behaves that it has for none
   of the 5 candidates. With a minimum pool of 3, one piece is a third of
   the vote; letting a piece we already have favorable information about
   into that vote changes how hard the test is in a way that has nothing
   to do with the candidates actually being sourced. Excluding it is what
   keeps this a blind test of the *new* pool, which is the thing being
   decided before the baselines come back.

If Bohemian Rhapsody's numbers under this protocol are interesting (e.g.
they replicate or contradict the historical 1/8 and 7/8 results), that's
worth a line in the report — as context, not as a vote.

## Amendment, 2026-08-24: pool expanded per the pre-registered fallback

The original 5-candidate pool cleared eligibility on only 2 of 5 (both
Beethoven concerto movements; all 3 Bach chorales came back at brute-force
cost 0.00). Per this document's own rule, that's below the minimum of 3 —
the correct response, written above before any of these numbers existed,
was to source more hard multi-instrument material, not lower the bar.

That sourcing happened: Beethoven's *Eroica* Symphony (4 movements),
Bach's Double Violin Concerto BWV1043 (3 files), and Haydn's Op.76 No.1
string quartet (4 files) were added and measured — all 11 came back at
0.00, which is itself the finding written up in `evals/corpus/README.md`
("Sourcing criteria" section): texture and instrument count don't predict
resistance, a featured virtuosic Romantic concerto soloist does. A
follow-up targeted search for more concertos in that specific shape
(Mendelssohn, Brahms, Liszt, Rachmaninoff, plus Bruch/Paganini/Saint-Saëns)
found real, licensed, typeset MIDI for exactly one — Tchaikovsky's Violin
Concerto Op.35 — the other composers' concertos don't exist on Mutopia at
all, checked against their real ftp listings.

Op.35's zip gave 3 files. Two qualify: `tchai_op35-2.mid` (cost 4.00) and
an unlabelled `tchai_op35.mid` (cost 5.00, the densest file in the
archive). A decision, made now, before any agent run against this pool:
**`tchai_op35.mid` does not join the verdict pool.** Its relationship to
`tchai_op35-2.mid` was never established — same zip, overlapping
plausible content, and unlike the numbered file it isn't confirmed to be
an independent movement rather than a fuller rendering of material
`tchai_op35-2.mid` already contains. Counting both as independent votes in
a 3-piece pool risks one underlying work casting two ballots. It's run and
reported for context, the same treatment as Bohemian Rhapsody, for a
related but distinct reason: not "unlicensed," but "not established as a
musically independent data point."

**Amended verdict pool (3 pieces, meets the minimum, 2 composers):**
`beethoven-piano-concerto3-1` (baseline cost 3.00), `beethoven-violin-
concerto-1` (2.00), `tchaikovsky-op35-2` (4.00). The original 5-candidate
pool's 3 non-qualifying chorales stay in the run set for regression value,
per the original eligibility rule; they're just not part of the count that
determines the verdict.

**Done as of 2026-08-24:** all 3 pool pieces are now `evals/corpus/manifest.json`
entries with real `id`/`license`/`sha256`/`midi_url` (or `zip_url` +
`zip_member`) fields, fetched and checksum-verified through the actual
`fetch_corpus.py` path — not just measured in passing. `python
fetch_corpus.py beethoven-piano-concerto3-1 beethoven-violin-concerto-1
tchaikovsky-op35-2` reproduces them from a fresh clone. The remaining
non-qualifying material sourced during the texture investigation (the 3
chorales, *Eroica*'s 4 movements, BWV1043's 3 files, Haydn's 4 files, and
`tchai_op35-1`/`tchai_op35.mid`) is still only written up in prose in
`evals/corpus/README.md`, not added as manifest entries — it doesn't need
to be for this protocol to run, since none of it is in the verdict pool.

## Fixed run parameters

- **Model:** `claude-sonnet-5` (`arranger.agent.DEFAULT_MODEL`), unchanged.
- **Profile:** `profiles/me.json`, unchanged — the same profile every
  number in this project has been measured against.
- **Attempts per run:** 4 (`MAX_ATTEMPTS`, unchanged).
- **Repeats per piece:** 5 — `arranger.evaluate`'s own CLI default, not a
  number picked for this eval. The historical 8-repeat runs
  (`SCORECARD_8attempts.md`) were a deep dive into one piece with spare
  budget; running 8 repeats across up to 6 pieces would be 6x that
  project's largest prior spend, so this drops to the tool's own default
  instead of re-justifying a bigger number. That's a real tradeoff — 5
  repeats resolves win-rate in 20% steps instead of 8's 12.5%, a coarser
  instrument — and it's being named here, not discovered after the numbers
  look inconvenient.
- **Countdown:** on (default) — the countdown-off ablation
  (`m7-deadline-effect.md`) is a separate question and does not get mixed
  into this run.
- **Total call budget:** repeats × attempts × N_pieces, upper bound
  5 × 4 × 6 = 120 API calls (5 candidates + Bohemian Rhapsody run
  alongside for context; fewer in practice — most runs accept before the
  4th attempt). Fixed before running. No re-running a piece that
  "looked bad," no extending repeats on pieces close to the threshold, no
  swapping model or attempts if the first pass is unconvincing. A second
  eval with different parameters is a new eval, logged as one, not a quiet
  replacement of this one.

## Verdict criteria

**Per run:** a win iff `run.best_cost < baseline_cost`, strictly. Ties do
not count — this is `PieceResult.beat_baseline()` exactly as it already
exists in `arranger.evaluate`, unmodified.

**Per piece:** "the agent beats brute force on this piece" iff it wins a
strict majority of repeats — with repeats=5, that's ≥3/5. 2/5 or fewer does
not count, including exactly a coin-flip-adjacent 2/5.

**Corpus-level claim** ("the agent beats brute-force baseline"): supported
iff the per-piece win holds on a strict majority of *qualifying* pieces.
With 3 qualifying pieces that's 2/3; with 4, it's 3/4 (a 2/4 tie does not
support the claim — ties default to not-supported, not to "inconclusive
counts as a pass").

## What falsifies the claim

Any of the following is a negative result, to be reported as exactly that
— not reframed, not excluded as an outlier after the fact:

1. **Fewer than 3 pieces qualify.** (Reported as insufficient data, which
   is also a kind of negative result: the corpus still can't test the
   claim.)
2. **The agent fails the per-piece majority (≥3/5) on more than half of
   qualifying pieces.**
3. **The corpus-level majority fails** (per "Verdict criteria," above),
   even if 2 does not hold — i.e. even if it wins outright on individual
   pieces, if that's not a majority of the qualifying pool the corpus-level
   claim is not supported.
4. **Median-cost regression:** on any qualifying piece, if the agent's
   *median* run cost is ≥ the brute-force baseline cost (not just an
   occasional bad run, but a typical one), that piece is flagged as "loses
   to brute force" and reported as such regardless of what the aggregate
   verdict says. This is a stronger and more specific claim than #2 and
   gets its own line in the report — "doesn't clearly win" and "typically
   loses" are different findings and should not collapse into one bullet.

## Reporting commitment

Whatever happens gets written to `SCORECARD.md` with an explicit verdict
line — `PASS`, `FAIL`, or `INSUFFICIENT DATA` — computed only from the
verdict pool (3 pieces as of the 2026-08-24 amendment, above), stated in
those terms, plus every raw run log
(`--raw`, same JSON format as `runs/raw*.json`) kept regardless of outcome.
Bohemian Rhapsody's row appears in the same table, clearly marked
"context, not counted," never folded into the verdict computation or its
surrounding prose. A negative result is not a reason to hold the report,
rerun with different settings, or quietly narrow which pieces get cited in
the summary.
