# Eval corpus

20 pieces, Public Domain or Creative Commons, all from the [Mutopia
Project](https://www.mutopiaproject.org/). See `manifest.json` for the full
list: composer, catalog number, license, source URL, sha256, and a measured
baseline (hard/strain violations of the *unarranged* MIDI against
`profiles/me.json`) that works as a rough difficulty proxy for the source
material — see below for why that's a different question from whether an
arrangement agent has anything to prove against this corpus.

## Get the files

```bash
python fetch_corpus.py            # everything
python fetch_corpus.py joplin-solace bach-invention-04-bwv775   # a subset
```

This downloads each piece's MIDI, checks its sha256 against `manifest.json`,
and writes it to `evals/corpus/<id>.mid`. Those `.mid` files are gitignored
on purpose (see Why not commit the audio, below) — running the fetch script
is how you get them, on this machine or a fresh clone, and it's what CI
should run before any eval that reads this corpus.

If a downloaded file doesn't match its recorded checksum, the script fails
loudly instead of silently using it. That means either the upstream file
changed or the manifest entry is stale — both need a human to look, not a
script deciding on its own which bytes are "close enough."

## Why Mutopia, and only Mutopia, for now

CLAUDE.md's corpus licensing rule names three approved sources: IMSLP,
Musopen, ccMixter. All three host audio; none of them are what `arranger`
consumes today. The pipeline is MIDI-in (see README, Roadmap — the audio
front end doesn't exist yet), and of the three, none reliably publish MIDI
with an explicit, per-piece, machine-readable license field the way Mutopia
does. Mutopia isn't in the original three-source list, but it satisfies the
same rule the list exists to enforce: public-domain or CC-licensed,
verifiably so, per piece. When the audio front end lands, IMSLP/Musopen/
ccMixter become relevant again and this corpus should grow an audio
counterpart — same manifest shape, different `source`/`*_url` fields.

## What's in it, and why

Spans early-classical teaching pieces (André, Burgmüller, Clementi-tier
difficulty) through pieces that generate real hard violations unarranged —
Chopin's Op.28 No.20 (wide sustained chords), Schumann's *Träumerei* (dense
inner voicing), Satie's *Gymnopédie No. 2* (a live example of the
pedal-modelling gap in `docs/build-log/limitations.md` — slow tempo, but the
verifier doesn't know about the pedal, so a lot of `leap_infeasible` shows up
that a pianist wouldn't actually hit), and Joplin's *Solace* (198 hard
violations unarranged — the stress-test role `samples/Queen - Bohemian
Rhapsody.mid` plays today, but licensed and checksummed).

One piece, `schubert-impromptu-d899-3`, ships as 6 MIDI tracks instead of the
usual 3 — `arranger.io._assign_staves` only infers hands from exactly 2
note-carrying tracks, so this is a real trigger for that gap, not a synthetic
one.

Two entries are CC-BY-SA rather than Public Domain (`mozart-k545-1-allegro`,
`field-nocturne-5-h37`) — the `attribution` field in the manifest names who
to credit if an arrangement built from them is ever redistributed.

`beethoven-fur-elise-woo59` deliberately duplicates a piece already in
`samples/Fur Elise.mid`. That old copy has no recorded source, license, or
checksum — it's exactly the kind of file the corpus licensing rule exists to
replace. Prefer the corpus entry going forward.

## Brute-force baseline is 0 for every piece here

`arranger.agent.brute_force_baseline` searches 54 fixed single-section plans
(7 left-hand patterns × 3 voice counts × 3 fold windows applied uniformly
across the whole piece) and reports the best one's cost. Run against this
corpus with `evals/corpus/SMOKETEST.md`'s harness (scripted model, no API
calls — see that file), **every one of the 20 pieces hits baseline cost
0.00** — a fully playable, fidelity-clean arrangement is reachable with one
fixed strategy for the whole piece, no per-section judgement required.

That's a real, load-bearing property of this corpus, not a measurement
artifact: it's consistent whether you look at the raw `baseline_hard` column
in `manifest.json` (which does vary, 0-198 — that's violations in the
*unarranged* source) or the brute-force *arranged* cost (which doesn't vary
at all — it's 0 everywhere). The two numbers are measuring different things.
`baseline_hard` says how far a piece is from playable before arranging.
Brute-force cost says how hard it is to *reach* playable, and for every
piece here, one fixed left-hand pattern reaches it.

The likely reason: everything in this corpus is already idiomatic solo piano
writing (Burgmüller, Bach, Mozart, Chopin, Schumann, Brahms, Schubert, Field
are literally piano repertoire; Joplin's rag reduces just as cleanly).
`docs/build-log/m3-plans-and-rendering.md` shows the one case brute force
struggles on — Bohemian Rhapsody, a fourteen-track band arrangement, whose
best single-section plan still leaves cost 1.00, not 0 — is a fundamentally
different kind of source material: something that was never one instrument
to begin with, so no single fixed reduction strategy fits the whole piece.

**Practical consequence:** this corpus cannot demonstrate "the agent beats
brute force," because brute force already wins outright on every piece in
it. `evaluate.py`'s own scorecard footer already says as much ("pieces whose
baseline cost is already 0 cannot demonstrate anything about the agent");
it just turns out that's *all 20 pieces*, not a few of them. What this
corpus *is* still good for: regression checks, difficulty calibration via
`baseline_hard`, and exercising the reduction heuristics (melody/chord
extraction, hand assignment) on real, varied, properly-licensed material.
For the "beats brute force" question specifically, see the next section —
it turned out "multi-instrument" alone isn't the fix either.

### Sourcing criteria: what actually resists brute force (measured, not guessed)

The first instinct — "solo piano is too idiomatic, source something with
independent voices instead" — was tested directly against `profiles/me.json`,
piece by piece, before any of it went into `manifest.json`. That instinct
turned out to be wrong twice before it was right. All 19 candidates
measured so far:

| piece | texture | tracks | brute-force cost |
|---|---|---|---|
| Bach, *O Haupt voll Blut und Wunden* | SATB chorale + piano | 5 | 0.00 |
| Bach, *Nun komm, der Heiden Heiland* | SATB chorale | 3 | 0.00 |
| Bach, BWV259 chorale | 4-part harmonisation | 3 | 0.00 |
| Bach, BWV1043, `score.mid` (unlabelled — relationship to the two numbered movements below not confirmed) | concerto grosso, 2 soloists | 7 | 0.00 |
| Bach, BWV1043, movement 1 (`score-1.mid`) | concerto grosso, 2 soloists | 7 | 0.00 |
| Bach, BWV1043, movement 2 (`score-2.mid`) | concerto grosso, 2 soloists | 7 | 0.00 |
| Beethoven, *Eroica* Symphony, I. Allegro con brio | full orchestra, no soloist | 9 | 0.00 |
| Beethoven, *Eroica*, II. Marcia funebre | full orchestra, no soloist | 10 | 0.00 |
| Beethoven, *Eroica*, III. Scherzo | full orchestra, no soloist | 9 | 0.00 |
| Beethoven, *Eroica*, IV. Finale | full orchestra, no soloist | 10 | 0.00 |
| Haydn Op.76 No.1, `score.mid` (unlabelled — relationship to the three numbered movements below not confirmed) | string quartet | 5 | 0.00 |
| Haydn Op.76 No.1, movement 1, presumed I. Allegro con spirito (`score-1.mid`, by file order) | string quartet | 5 | 0.00 |
| Haydn Op.76 No.1, movement 2, presumed II. Adagio sostenuto (`score-2.mid`, by file order) | string quartet | 5 | 0.00 |
| Haydn Op.76 No.1, movement 3, presumed III. Menuet (`score-3.mid`, by file order) | string quartet | 5 | 0.00 |
| Tchaikovsky, Violin Concerto Op.35, `tchai_op35-1.mid` (labelled "movement 1" — but see caveat below) | orchestra + solo violin | 12 | 0.00 |
| **Beethoven, Piano Concerto No.3, I. Allegro con brio** | **orchestra + solo piano** | 14 | **3.00** |
| **Beethoven, Violin Concerto, I. (mvt)** | **orchestra + solo violin** | 14 | **2.00** |
| **Tchaikovsky, Violin Concerto Op.35, `tchai_op35-2.mid` (labelled "movement 2")** | **orchestra + solo violin** | 16 | **4.00** |
| **Tchaikovsky, Violin Concerto Op.35, `tchai_op35.mid` (unlabelled, densest file — plausibly the full 3-movement work)** | **orchestra + solo violin** | 16 | **5.00** |

15 of 19 hit 0.00, cleared by the same escape hatch every time —
`block, voices=1` (occasionally with `fold=12`/`fold=16` for a wide melodic
range) — one bass note per chord, no upper left-hand voices, and it's
enough. That includes a *full Romantic-scale symphony with no soloist at
all* (Eroica, all four movements) and a *string quartet* (nothing but four
genuinely independent solo lines, no accompaniment to flatten in the first
place). The original hypothesis in this section — "orchestral reduction
texture resists, vocal/chamber texture doesn't" — is falsified by Eroica:
it's exactly the orchestral texture that instinct predicted would resist,
and it didn't.

(Two honest gaps, named rather than smoothed over: Haydn Op.76 No.1 is a
four-movement quartet and only 3 numbered movements plus the unlabelled
`score.mid` came back from its zip — no `score-4.mid` exists under this
archive's naming, so the Finale wasn't tested. And `tchai_op35-1.mid` is
labelled "movement 1" but is short and sparse — 1,584 notes over 119 bars —
for what should be a roughly 20-minute Allegro moderato; it's more likely
an excerpt, a cadenza, or a mislabelled fragment than the real first
movement, and its 0.00 cost shouldn't be read as "Tchaikovsky's first
movement doesn't resist" without more confidence in what the file actually
contains. Both gaps are "unlikely to break the pattern, but unmeasured,"
not "resolved.")

**What's left standing, and what's different about it:** the two Beethoven
concerto movements plus the two larger, denser Tchaikovsky files — four
pieces, two composers, and every one of them a Romantic concerto with a
single featured soloist against orchestral accompaniment. Bach's double
violin concerto has soloists too and still reduces to 0.00. The
mechanistic difference, visible in the winning plan itself: every 0.00
piece's left hand voices=1 the *accompaniment* down to nothing and that's
sufficient — the melody line, once folded, still fits. The four pieces
that resist do so even with voices=1 on the left hand: the remaining
violations are in the *melody* (the soloist's own line), which folding by
whole octaves can't resolve because the problem isn't register, it's the
density and leap rate of genuinely virtuosic Romantic solo writing.
Baroque solo violin writing (BWV1043) and ensemble melodic material
(Eroica, Haydn) both stay inside a more conservative practical range even
at their most active; a 19th-century concerto soloist doesn't.

**Sourcing rule going forward, held to N=4-positive / N=15-negative across
two composers and named as such:** neither "multi-instrument,"
"orchestral," nor "many independent voices" predicts resistance to brute
force — all three were tried and falsified in this table. The working
signal, now supported by a second composer independently, is a *genuinely
virtuosic continuous solo line written against orchestral accompaniment* —
i.e., specifically a concerto, and specifically one with solo writing
dense/wide-ranging enough that folding can't rescue it, which BWV1043's
Baroque solo violin writing wasn't. Two composers is still a small sample
to source blind by era or genre label — full orchestral concerto scores
are also just rare on Mutopia: a targeted search for five more named
Romantic-concerto composers (Mendelssohn, Brahms, Liszt, Rachmaninoff,
plus Bruch/Paganini/Saint-Saëns as extras) found real, typeset, licensed
MIDI for exactly one of them (this Tchaikovsky concerto) — the other four
composers have no concerto on Mutopia at all, checked against their actual
ftp directory listings, not just search coverage. **Before adding any
"hard" candidate to this corpus, measure its `brute_force_baseline` cost
first** — the same discipline this table followed three rounds running,
each round overturning or narrowing the sourcing theory that came before
it.

*("N=4" here counts every measured piece with nonzero cost, including
`tchai_op35.mid`. That's a different, narrower question from "how many of
these can vote in the beats-brute-force verdict" — `tchai_op35.mid` was
excluded from that specific 3-piece pool because its relationship to
`tchaikovsky-op35-2` was never established (same zip, plausibly
overlapping content), not because it stopped resisting. See
`docs/build-log/eval-protocol.md`'s 2026-08-24 amendment and
`SCORECARD.md` for the pool itself — "4 resist" and "pool of 3" are both
correct, and they're answering different questions.)*

## Zip-only pieces are no longer excluded

The 20 pieces above all ship as a bare `.mid`. Earlier drafts of this corpus
skipped anything published only inside a multi-movement `*-mids.zip`
(Clementi's Op.36 No.1 sonatina, Grieg's *Anitra's Dance*), to keep
`fetch_corpus.py` a single-file-per-entry download. That constraint is gone:
`fetch_corpus.py` now accepts a `zip_url` + `zip_member` pair instead of
`midi_url` (stdlib `zipfile`, no new dependency) — a manifest entry can
point at one member inside an archive and everything else (caching,
checksum verification, `python fetch_corpus.py <id>` for a single-piece
re-fetch) works identically.
This is what made it possible to source and baseline the orchestral
candidates above, several of which (Beethoven's piano and violin concerto
movements) only exist as zip members on Mutopia.

## Extending the corpus

Add an entry to `manifest.json` with `id`, `title`, `composer`, `catalog`,
`style`, `license`, `attribution` (or `null`), `source`, `source_page`, and
either `midi_url` (a direct `.mid`) or `zip_url` + `zip_member` (a member
path inside a `*-mids.zip`, for multi-movement works). Then run:

```bash
python fetch_corpus.py <new-id>
```

It will download the file, print the sha256 it got, and fail because the
manifest doesn't have that hash yet — that failure message *is* the value to
paste into `bytes`/`sha256`. Re-run once the manifest has it; a clean
"fetched ... verified" means the entry is done. Fill in `baseline_hard` /
`baseline_strain` by running the piece through `arranger.verify` against
`profiles/me.json`, the same way every other entry here was measured.
