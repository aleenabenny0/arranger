# Architecture

Arranger is organized around one rule: model judgment and mechanical
correctness live in different layers. The model chooses an `ArrangementPlan`;
deterministic code renders that plan into a `Score`; the verifier decides
whether that score is playable for a specific `PlayerProfile`.

## Layers

```text
interfaces
  CLI and FastAPI today; workers later
    |
application
  use cases: verify_score, render_plan, fidelity_for, arrange_score, baseline_for
    |
adapters
  JSON score debug format, MIDI input today; MusicXML/audio/storage later
    |
domain core
  ir, profile, plan, render, fidelity, verify
```

## Domain core

The domain core is the part to trust and protect:

- `arranger.ir`: `Note` and `Score`, the internal representation.
- `arranger.profile`: measurable player limits.
- `arranger.plan`: the only schema a model may emit.
- `arranger.render`: deterministic plan-to-score rendering.
- `arranger.verify`: dependency-free playability oracle.
- `arranger.fidelity`: checks that the arrangement is still recognizably the
  source music.

The verifier must remain dependency-free. It is the oracle every other layer
is judged against, so it should not break because a parser, model SDK, or web
framework changed.

## Application Layer

`arranger.application` is the stable orchestration surface for callers:

- `verify_score(score, profile)`
- `render_plan(plan, source)`
- `fidelity_for(source, arranged)`
- `arrange_score(source, profile, model=...)`
- `baseline_for(source, profile)`

CLIs, APIs, job workers, notebooks, and tests should prefer this layer instead
of manually wiring renderer, verifier, agent, and scoring code together.

## Adapters

Adapters translate external artifacts into domain objects:

- `arranger.adapters.score_json`: stable debug/test format.
- `arranger.adapters.midi`: MIDI input, currently backed by `arranger.io`.

Future adapters should live beside these:

- `musicxml.py`: MusicXML input/output.
- `pdf.py`: engraving through MuseScore or LilyPond.
- `audio.py`: transcription pipeline using source separation and pitch
  detection.

Adapters may depend on third-party libraries. The domain core should not.

## Storage

`arranger_api.storage` is the persistence layer for the HTTP API. It uses
Postgres when `DATABASE_URL` or `ARRANGER_DATABASE_URL` is configured. Local
development falls back to SQLite. The repository stores evolving domain
payloads as JSON plus query-friendly metadata such as title, note count, bar
count, status, and verdict counts.

Local runs use `data/arranger.db`. Tests use in-memory SQLite connections.
Cloud runs should use managed Postgres.

## Auth And Permissions

`arranger_api.main` serves the static frontend from `/` when the `frontend/`
directory is present. `arranger_api.settings` reads cloud configuration from
environment variables such as `PORT`, `FRONTEND_ORIGINS`, `COOKIE_SECURE`, and
`FRONTEND_DIR`.

`arranger_api.auth` owns browser authentication. Registration and login create
HTTP-only cookie sessions. Persistent records carry `user_id`, and repository
methods require that user id for saved-resource reads and writes.

The stateless compute endpoints are public. Persistent endpoints are private:
profiles, scores, plans, arrangements, and runs can only be accessed by their
owner.

Unsafe cookie-authenticated writes require a double-submit CSRF token. Auth and
write paths are rate-limited in process. Password reset tokens are stored only
as hashes and are consumed once, revoking active sessions for that user. Reset
delivery goes through an email provider port so production can use Resend while
local development can log reset links without external services.

## Ports

`arranger.ports` names infrastructure-facing protocols. Today it defines the
model and score-reader boundaries. As the project grows, add ports before
binding the core directly to databases, queues, HTTP frameworks, or model
vendors.

## Dependency Direction

Allowed direction:

```text
interfaces -> application -> adapters/domain -> domain
```

Avoid these:

- verifier importing model clients or web frameworks
- renderer reading files directly
- model clients emitting notes, MusicXML, or MIDI events
- APIs duplicating orchestration that belongs in `arranger.application`

## Next Architectural Milestones

1. Move MusicXML/PDF work into adapters, not the domain core.
2. Replace greedy hand assignment behind the existing `assign_hands` boundary.
3. Move rate limiting to Redis or the hosting edge before multi-replica scale.
4. Add object storage for generated MIDI/MusicXML/PDF artifacts once export exists.
5. Move rate limiting and session invalidation metadata to Redis before
   multi-replica scale.
6. Convert the repair loop to LangGraph only after the use cases and artifacts
   are stable.

See `docs/artifact-storage.md` for the current artifact storage decision.
