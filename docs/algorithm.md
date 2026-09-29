# Algorithm

Arranger now uses a two-stage planning process:

```text
score + player profile
  -> musician-style analysis
  -> deterministic planner
  -> draft ArrangementPlan
  -> render
  -> verify + fidelity
  -> targeted repair + model repair loop when needed
```

The deterministic planner gives the system a stable first draft before any
model call. The model can still improve musical judgment, but the first plan is
grounded in measurable score features and the same verifier used everywhere
else.

## Deterministic Planner

`arranger.planner` does four things:

1. Extracts bar-level features with `analyze_score()`.
2. Scores likely section boundaries with `detect_regions()`.
3. Rates each region with `score_region_difficulty()`.
4. Generates multiple left-hand candidates for each region.
5. Chooses the best per-section candidate by rendering, verifying, and
   measuring fidelity in the context of the whole plan.

The current feature set is intentionally small:

- notes per bar
- distinct onsets per bar
- rough density
- detected chord root and quality
- maximum simultaneous notes
- shortest rhythmic gap
- melody low/high pitch
- melody span
- largest melody leap
- tempo
- player skill, span, hand speed, and range

## Musician-Style Analysis

`arranger.musicianship` adds a reduction layer based on how pianists normally
turn dense music into a playable score:

1. Protect the melody.
2. Protect the bass.
3. Keep chord-defining notes such as thirds and sevenths.
4. Thin doubled notes, fifths, crowded inner voices, and low-register clutter.
5. Add texture only after the musical skeleton is clear.

`analyze_note_importance()` assigns every source note a layer and importance
score. `reduction_priority()` returns the least-essential notes first, which
gives future renderers and repair tools a musician-like thinning order.

Difficulty scores are 0-10 and ranked as `easy`, `moderate`, `challenging`,
`demanding`, or `extreme`. They combine texture density, chord thickness,
rhythmic activity, melody span, melody leaps, tempo, and harmonic movement.
Regions can also be sorted with `rank_regions_by_difficulty()` so the hardest
section is visible before any model repair step.

Section labels now include role, energy, difficulty, and treatment. Example:

```text
bars 3-4 high extreme 8.4: climax reduction, single-voice harmony
```

The current decision rules are still safety-first, but no longer only
whole-piece fallbacks:

- wide melodies get octave folding
- beginner, fast, wide, dense, or high-difficulty regions use safer left-hand patterns
- `pedal_tone` and low voice counts are preferred when playability risk is high
- advanced sparse regions can receive more active patterns such as `walking`,
  `arpeggio`, `alberti`, or `broken_octave`
- dense or strained regions are compared against compact block and pedal
  treatments before the whole plan is accepted
- conservative whole-piece candidates remain available as emergency baselines

## Agent Integration

The agent loop still exists, but it no longer starts from nothing. The first
model prompt now includes the deterministic draft plan and asks the model to
revise only where musical judgment or playability requires it.

The prompt also receives:

- lowest-priority notes to thin first
- retrieved arranging guidance from a local guidance library
- targeted repair suggestions after verifier failures

This keeps responsibilities clear:

- deterministic planner: safe first draft
- renderer: turns plans into notes
- verifier: checks physical playability
- fidelity scorer: catches arrangements that delete too much music
- guidance retrieval: explains the arranging rule that applies
- repair suggester: proposes concrete section-field edits
- model: improves judgment and repairs failures

## API Surface

`POST /plan/deterministic` returns:

- the deterministic plan
- verifier verdict
- fidelity metrics

`POST /plan/analysis` returns:

- detected regions and difficulty
- hardest-region ranking
- note importance
- reduction priority
- retrieved arranging guidance
- candidate-ranking rows suitable for later ML training

This makes the algorithm visible to the frontend and gives tests a direct
surface for checking planner quality.

## AI and ML Roadmap

The project now has the architecture for AI and ML without making the core
arranger random:

- The LLM receives a deterministic plan, retrieved guidance, and exact repair
  suggestions.
- Candidate-ranking rows export region features, profile features, pattern
  choices, verifier cost, planner penalty, and the chosen label.
- Persistent dry runs save those rows to `candidate_rankings`, which turns real
  usage into a training set instead of a one-off debug artifact.
- Once enough user-approved arrangements exist, those rows can train a ranking
  model that replaces or augments the current heuristic penalty.

## Next Algorithm Work

The next useful improvements are:

1. Save explicit user accept/reject edits for each candidate row.
2. Add a real embedding/vector store once a licensed arranging corpus exists.
3. Train a lightweight ranking model against accepted user edits.
4. Corpus-level evaluation that compares deterministic planner, brute force,
   and model repair on the same pieces.
