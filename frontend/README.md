# Arranger Frontend

This is a dependency-free static workspace for the current JSON contracts.
It can be opened directly in a browser, and it is also served by FastAPI at
`/` when the backend is running.

## What it does now

- Loads a built-in sample score.
- Registers, logs in, logs out, and keeps session cookies through the backend.
- Uploads score JSON shaped like `tests/fixtures/too_hard.json`.
- Uploads profile JSON shaped like `profiles/me.json`.
- Edits high-level arrangement-plan sections.
- Calls the Python backend's `/render-and-verify` endpoint when it is running.
- Saves the current score, profile, and plan through the backend.
- Falls back to a browser-side source-score verifier when the backend is not
  running.
- Shows verdict counts, violations, plan health, a bar timeline, and JSON
  views.
- Exports the drafted plan and current verdict payload as JSON.

## What it does not do yet

- It does not read MIDI in the browser.
- It does not start the Python backend by itself.
- It does not generate MusicXML, PDF, or MIDI output.

## Backend

Install the optional API dependencies, then start the server before clicking
`Save Work` or `Verify Plan` in the frontend:

```bash
pip install -e .[api]
arranger-api
```

Or:

```bash
python -m uvicorn arranger_api.main:app --reload
```

When served by FastAPI, the frontend uses the same origin automatically. When
opened as a local file, it defaults to `http://127.0.0.1:8000`.

Saved records are stored in `data/arranger.db` and belong to the signed-in
user.
