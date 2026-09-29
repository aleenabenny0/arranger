# Arranger frontend

A static site: HTML, CSS and ES modules, no build step and no framework. FastAPI
serves it at `/`. It can also be hosted anywhere else; set the
`<meta name="arranger-api">` content in each HTML file to the API origin, and
list the site's origin in the API's `FRONTEND_ORIGINS`.

## Files

| File | What it is |
|---|---|
| `index.html`, `styles.css` | The app shell and the design system (tokens, light and dark, print). |
| `js/app.js` | Session, hash routing, navigation, sign-in, registration, reset and verification views. |
| `js/view-library.js` | Your pieces: upload with progress, search, paging, rename, delete. |
| `js/view-project.js` | One piece: source and corrections, hand profile and calibration, arranging with a cancellable job, result, revisions and comparison, advanced plan editor. |
| `js/view-account.js` | Usage against quotas, email verification, password, sessions, data export, account deletion. |
| `js/api.js` | `fetch` wrapper: cookies, CSRF header, typed errors, idempotency keys, job polling, aborts. |
| `js/dom.js` | Element builder, dialogs, tabs, toasts, live regions. Nothing assigns `innerHTML`. |
| `js/notation.js` | Notation preview with Verovio: paging, zoom, findings marked on their notes. |
| `js/player.js` | WebAudio playback of the source and the arrangement, with seeking, speed and bar tracking. |
| `js/legal.js` | Fills operator details into the legal pages from `/legal/config`. |
| `privacy.html`, `terms.html`, `cookies.html`, `copyright.html`, `support.html` | Draft legal and help pages. See `docs/legal-review.md`. |
| `vendor/` | Third-party code fetched by `python fetch_vendor.py` and verified by SHA-256. Not committed. |

## Rules this code keeps

- **No `innerHTML`, no inline script, no `eval`.** The content security policy
  forbids them and CI greps for them. Text from the server or the user is always
  set as text. Notation SVG is parsed and adopted node by node after scripts,
  `foreignObject` and event-handler attributes are removed.
- **Nothing is faked.** Every number on screen comes from the API. When the
  server has no LilyPond or no audio model, the interface says so instead of
  showing a button that cannot work.
- **Results are tied to what they were made from.** An arrangement made from an
  older source revision or a different hand profile is marked out of date.
- **Accessible by construction.** Headings in order, labelled controls, errors
  in words next to the field and announced, focus moved to the new heading on
  navigation, a skip link, WAI-ARIA tabs, native `<dialog>`, findings listed as
  a table as well as marked in the score, state never shown by colour alone,
  `prefers-reduced-motion` and `prefers-color-scheme` respected, usable at 320
  pixels wide.

## Testing

```bash
node --check js/*.js                       # syntax
pip install -e ".[api,dev,e2e]"
python fetch_vendor.py --dev               # Verovio, and axe-core for the tests
python -m pytest tests/test_browser_e2e.py
```

The browser tests start the real server on a free port and drive it with
Chromium or Edge: register, verify email, upload, correct, arrange, play,
download and read the files, revise, compare, reopen, export the account. They
run axe-core on every view, repeat the journey with the keyboard alone at phone
width in the dark theme, and fail on any console error, failed request or
content-security-policy refusal. Set `E2E_SCREENSHOT_DIR` to keep screenshots.

Not tested: Firefox, Safari, real phones, and real assistive technology.
