# M4b: the first live run

First real API call. The loop succeeded — and spent half its budget on a
single bug wearing two disguises.

```
Tokyo Ghoul - Unravel: 279 notes, 50 bars
brute-force baseline: 0 hard (block, 1 voices)
  attempt 1: rejected - ValueError: no JSON object in model response
  attempt 2: rejected - JSONDecodeError: Expecting ',' delimiter (char 1459)
  attempt 3: 1 hard violations
  attempt 4: 0 hard violations
  PLAYABLE after 4 attempt(s)
tokens: 8325 in, 7237 out
```

## One bug, two error messages

The two failures looked unrelated — one "no JSON at all", one "malformed
JSON". They were the same thing: `max_tokens` was set to 2000, and plans for a
50-bar piece do not fit in 2000 tokens.

Cut off **mid-JSON**, the response fails as a syntax error.
Cut off **before the JSON begins**, while the model is still writing prose,
the response fails as "no JSON object".

The second disguise is the dangerous one. It reads as *the model ignored the
instruction to return JSON only* — a prompt problem — when it was a budget
problem. Prompt fixes for that would have accomplished nothing and looked
like they were being ignored.

**Fixes:**
- `max_tokens` 2000 → 8000.
- `TruncatedResponse`, raised on `stop_reason == "max_tokens"`, telling the
  model it ran out of room and should use fewer sections. Previously the model
  received "Expecting ',' delimiter" and was sent hunting for a comma that was
  never missing.
- System prompt now asks for one-sentence rationales.

## A fix that had to be reverted

Response prefill — seeding the reply with `{` so prose is structurally
impossible — was added at the same time, and the next run failed immediately:

```
400 invalid_request_error: This model does not support assistant message
prefill. The conversation must end with a user message.
```

Removed. Two lessons:

1. **Two fixes at once meant neither could be evaluated.** The prefill broke
   the run before the token fix could be observed, and its value was never
   measured.
2. **It was unnecessary.** Re-reading attempt 1 with the token limit in mind,
   the prose was not the model ignoring instructions — it was the visible part
   of a truncated response. Fixing the ceiling addresses both failures; the
   parser already tolerates prose around a complete JSON object.

## A latent crash

The error handler read `raw` before it was guaranteed to be bound. Any failure
inside `model(messages)` itself — a network error, an API error — would raise
`NameError` from the handler and lose the real exception. Now initialised
before the `try`.

Found by reading the traceback from the prefill failure rather than by any
test. Error paths run rarely, so their bugs surface late and disguised as
something else.

## Cost

7237 output tokens across four attempts, of which two produced nothing usable.
At roughly 1800 tokens per attempt, the 2000-token ceiling was always going to
be hit. Fractions of a cent, but the same arithmetic at scale is the
difference between a viable eval harness and an unaffordable one.

## Still unknown

Bohemian Rhapsody has not completed a live run. Brute-force baseline on this
profile is **36** (walking bass, 1 voice).

The open question is whether the model produces *multiple sections*. One
section cannot beat brute force, which already searched all 18 single-section
combinations exhaustively. Several sections with different treatments is the
only structural advantage available to it.
