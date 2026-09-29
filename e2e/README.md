# End-to-end tests

Playwright drives the real product in Chromium: the API is started by
`playwright.config.ts` (`webServer`) against a fresh SQLite database under
`e2e/.tmp`, so every run begins with no accounts and no pieces and the tests
register what they need.

```bash
cd e2e
npm ci
npx playwright install chromium     # once
npx playwright test                 # 15 tests, about 40 seconds
npx playwright show-report          # the HTML report of the last run
```

PowerShell: the same commands. The API is started with the repository's
`.venv` Python when it exists, otherwise `python`; `ARRANGER_PYTHON` overrides
that. The notation engine must have been fetched once (`python fetch_vendor.py`
from the repository root).

## What is covered

| File | Tests |
|---|---|
| `tests/auth.spec.ts` | register, sign in, sign out, wrong password, protected routes redirect to sign-in |
| `tests/workflow.spec.ts` | upload a score as JSON, edit the player profile from a preset and by hand, arrange and read the verdict, author and evaluate an edited plan, export MIDI (the file is read back) |
| `tests/views.spec.ts` | the step tabs (WAI-ARIA state), the score (real notation), plan (JSON editor), JSON (verdict and engine report) and revisions views |
| `tests/a11y.spec.ts` | axe-core on home, sign-in, register, library, project, hands and account: no serious or critical WCAG 2.x A/AA violations |
| `tests/api.spec.ts` | `/health`, `/ready`, `/catalog`, and the account round trip through the `request` fixture including the CSRF header on sign-out |

Locators use roles and `data-testid` attributes; the ids are listed in
`frontend-react/README.md`. Traces, screenshots and page snapshots are kept for
failed tests under `test-results/`; CI uploads them and the HTML report as
artifacts.

The Python browser suite in `tests/test_browser_e2e.py` still exists: it is the
deeper journey (email verification, playback, PDF, keyboard-only use at phone
width). This package is the fast, role-based regression suite; the React
rewrite of the web app (item 08) passed it unchanged, which is what it is for.

The API serves `frontend-react/dist`, so build the app first (`npm ci && npm
run build` in `frontend-react`, after `python fetch_vendor.py`).
