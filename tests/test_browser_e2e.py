"""The whole product, driven through a real browser against a real server.

Nothing here is mocked except email delivery (a recording fake, so no mail is
sent). The server is the real app on a real socket with a real background
worker; the browser is Chromium or Edge; the files that come back are read.

    python fetch_vendor.py --dev
    pip install playwright && playwright install chromium   # or have Edge/Chrome installed
    pytest tests/test_browser_e2e.py

The tests are skipped, with the reason, when Playwright, a browser, the
notation engine or axe-core is missing. CI installs all four, so they run there.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from conftest import PASSWORD, FakeEmailSender, build_settings

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
uvicorn = pytest.importorskip("uvicorn")

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.ir import Note, Score, TrackInfo  # noqa: E402
from arranger.timeline import Timeline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AXE = ROOT / ".cache" / "tools" / "axe.min.js"
VEROVIO = ROOT / "frontend" / "vendor" / "verovio-toolkit-wasm.js"
SHOTS = Path(os.environ["E2E_SCREENSHOT_DIR"]) if os.environ.get("E2E_SCREENSHOT_DIR") else None

pytestmark = [
    pytest.mark.skipif(not AXE.is_file(), reason="axe-core is missing: run `python fetch_vendor.py --dev`"),
    pytest.mark.skipif(not VEROVIO.is_file(), reason="Verovio is missing: run `python fetch_vendor.py`"),
]


def waltz(bars: int = 8, title: str = "Browser Waltz") -> Score:
    """A tune over a wide left hand: enough for the arranger to have work to do.

    The MIDI writer puts staff 2 on its own track, so the file has two parts.
    """
    beat = 0.5
    notes = []
    tune = [72, 74, 76, 77, 79, 77, 76, 74]
    for bar in range(bars):
        start = bar * 3 * beat
        for k in range(3):
            notes.append(Note(pitch=tune[(bar + k) % len(tune)], onset=start + k * beat, duration=beat * 0.95,
                              bar=bar + 1, track=0))
        root = [48, 43, 45, 41][bar % 4]
        notes.append(Note(pitch=root, onset=start, duration=beat * 0.95, bar=bar + 1, track=1, staff=2))
        for k in (1, 2):
            for pitch in (root + 12, root + 16, root + 19):
                notes.append(Note(pitch=pitch, onset=start + k * beat, duration=beat * 0.9, bar=bar + 1, track=1, staff=2))
    return Score(notes=notes, title=title, tempo_bpm=120, timeline=Timeline.constant(120, 3, 4),
                 tracks=[TrackInfo(0, "Melody"), TrackInfo(1, "Accompaniment")])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from arranger_api.main import create_app, get_email_sender

    tmp = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    settings = build_settings(
        sqlite_path=str(tmp / "app.db"), artifact_dir=str(tmp / "files"), frontend_dir=ROOT / "frontend",
        public_base_url=origin, cors_origins=[origin], cookie_secure=False, csrf_protection=True,
        require_verified_email=True, job_workers=1,
    )
    app = create_app(settings)
    sender = FakeEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not instance.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("the server did not start")
        time.sleep(0.05)
    try:
        yield {"origin": origin, "sender": sender, "tmp": tmp, "app": app}
    finally:
        instance.should_exit = True
        thread.join(timeout=20)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        errors = []
        forced = os.environ.get("E2E_BROWSER_CHANNEL")
        channels = [forced] if forced else [None, "msedge", "chrome"]
        for channel in channels:
            try:
                launched = playwright.chromium.launch(channel=channel) if channel else playwright.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - any launch failure means "try the next browser"
                errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0]}")
                continue
            try:
                yield launched
            finally:
                launched.close()
            return
        pytest.skip("no browser could be started: " + "; ".join(errors))


class Problems:
    """Everything the browser complained about: script errors, CSP refusals, failed requests.

    The one failure a healthy page makes is `GET /auth/me` answering 401 before
    anyone has signed in: that is how the app asks "is there a session?".
    """

    def __init__(self, page):
        self.items: list[str] = []
        page.on("pageerror", lambda exc: self.items.append(f"pageerror: {exc}"))
        page.on("console", self._console)
        page.on("requestfailed", self._failed)
        page.on("response", self._response)

    def _console(self, message):
        # Failed requests are judged by `_response`, which knows the URL; the console line does not.
        if message.type == "error" and "Failed to load resource" not in message.text:
            self.items.append(f"console.error: {message.text}")

    def _response(self, response):
        if response.status < 400:
            return
        if response.status == 401 and response.url.endswith("/auth/me"):
            return
        self.items.append(f"http {response.status}: {response.request.method} {response.url}")

    def _failed(self, request):
        # Navigating away aborts in-flight requests on purpose; downloads show up as aborted navigations.
        if "ERR_ABORTED" not in str(request.failure):
            self.items.append(f"requestfailed: {request.method} {request.url} {request.failure}")


def check_accessibility(page, where: str):
    """axe-core with the WCAG 2.0, 2.1 and 2.2 A/AA rules. Any violation fails the test."""
    page.evaluate(AXE.read_text(encoding="utf-8"))
    result = page.evaluate(
        """async () => await axe.run(document, { runOnly: { type: "tag",
            values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] } })"""
    )
    lines = []
    for violation in result["violations"]:
        targets = "; ".join(" ".join(node["target"]) for node in violation["nodes"][:4])
        lines.append(f"{violation['id']} ({violation['impact']}): {violation['help']} -> {targets}")
    assert not lines, f"accessibility problems on {where}:\n" + "\n".join(lines)


def shot(page, name: str):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def no_sideways_scroll(page, where: str):
    widths = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert widths[0] <= widths[1] + 1, f"{where} scrolls sideways: content {widths[0]}px in a {widths[1]}px window"


def focused_text(page) -> str:
    return page.evaluate("() => (document.activeElement.textContent || '').trim()")


def register_and_verify(page, server, email):
    origin, sender = server["origin"], server["sender"]
    page.goto(origin + "/")
    page.get_by_role("heading", level=1, name="Piano arrangements that fit your hands").wait_for()
    page.get_by_role("link", name="Create a free account").click()
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Create account").click()
    page.get_by_role("heading", level=1, name="Your pieces").wait_for()
    # Unverified: the page says so, and says what to do about it.
    assert page.get_by_text("Verify your email address to upload and arrange.").is_visible()
    link = next(v["verify_link"] for v in reversed(sender.verifications) if v["email"] == email)
    assert link.startswith(origin + "/#/verify-email?token=")
    page.goto(link)
    page.get_by_role("heading", level=1, name="Email verified").wait_for()
    assert "token" not in page.url, "the token must be scrubbed from the address bar"
    page.get_by_role("link", name="Go to your pieces").click()
    page.get_by_role("heading", level=1, name="Your pieces").wait_for()


def test_upload_arrange_preview_download_and_reopen(server, browser, tmp_path):
    origin = server["origin"]
    midi = tmp_path / "browser_waltz.mid"
    midi.write_bytes(write_midi(waltz()))

    context = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    problems = Problems(page)

    # --- signed out -----------------------------------------------------------------
    page.goto(origin + "/")
    page.get_by_role("heading", level=1, name="Piano arrangements that fit your hands").wait_for()
    check_accessibility(page, "home")
    shot(page, "01-home")

    register_and_verify(page, server, "pianist@example.com")
    check_accessibility(page, "empty library")
    assert page.get_by_text("No pieces yet. Upload one to begin.").is_visible()

    # --- upload ---------------------------------------------------------------------
    page.set_input_files("#upload-input", str(midi))
    page.get_by_role("heading", level=1, name="Browser Waltz").wait_for()
    assert page.get_by_role("tab", name="1. Source").get_attribute("aria-selected") == "true"
    facts = page.locator("dl.facts").first.inner_text()
    assert "8 bars" in facts and "3/4" in facts and "120 beats per minute" in facts
    assert page.get_by_text("Likely the tune").count() == 1
    check_accessibility(page, "source step")
    shot(page, "02-source")

    # The source can be looked at as notation before anything is arranged.
    page.get_by_role("button", name="Show the source as notation").click()
    page.locator(".notation svg g.note").first.wait_for(timeout=60_000)

    # Correct the import: say which part is the tune. That makes source revision 2.
    page.get_by_label("Use Right hand as the melody").check()
    page.get_by_role("button", name="Save corrections").click()
    page.get_by_text("source revision 2").wait_for()

    # --- hands ----------------------------------------------------------------------
    page.get_by_role("tab", name="2. Your hands").click()
    page.get_by_label("Start from").select_option("small_hands")
    reach = page.get_by_label("Largest reach (semitones)")
    assert int(reach.input_value()) < 12, "the small-hands preset must narrow the reach"
    check_accessibility(page, "hands step")
    shot(page, "03-hands")

    # --- arrange --------------------------------------------------------------------
    page.get_by_role("tab", name="3. Arrangement").click()
    page.get_by_role("button", name="Arrange for my hands").click()
    page.get_by_role("heading", level=2, name="Arrangement 1").wait_for(timeout=120_000)
    verdict = page.locator(".verdict").inner_text()
    assert re.search(r"Passes modeled constraints|finding|unresolved", verdict, re.I), verdict
    assert page.get_by_text(re.compile(r"Fidelity score \d+%")).is_visible()
    assert page.get_by_role("heading", name="What was changed").is_visible()

    # The score is real notation, drawn from the MusicXML the server made.
    page.locator(".notation svg g.note").first.wait_for(timeout=60_000)
    assert page.locator(".notation svg g.note").count() >= 24
    check_accessibility(page, "arrangement step")
    shot(page, "04-arrangement")

    # --- listen ---------------------------------------------------------------------
    page.get_by_role("button", name="Play", exact=True).click()
    page.get_by_role("button", name="Pause", exact=True).wait_for()
    page.wait_for_function("() => Number(document.querySelector('input[aria-label=Position]').value) > 0.3")
    page.get_by_role("button", name="Pause", exact=True).click()
    page.get_by_role("button", name="Play", exact=True).wait_for()
    page.get_by_label("Original").check()      # compare against the source

    # --- download -------------------------------------------------------------------
    with page.expect_download() as info:
        page.get_by_role("button", name="Download MIDI").click()
    assert info.value.suggested_filename.endswith(".mid")
    assert Path(info.value.path()).read_bytes()[:4] == b"MThd"

    with page.expect_download() as info:
        page.get_by_role("button", name="Download MusicXML").click()
    xml = Path(info.value.path()).read_text(encoding="utf-8")
    assert "<score-partwise" in xml and xml.count("<note") >= 24

    if page.get_by_role("button", name="Download Printable PDF").count():
        with page.expect_download(timeout=180_000) as info:
            page.get_by_role("button", name="Download Printable PDF").click()
        assert Path(info.value.path()).read_bytes()[:5] == b"%PDF-"
    else:
        assert page.get_by_text("PDF engraving is not installed on this server.").is_visible()

    # --- revise: a second arrangement for bigger hands, then compare ------------------
    page.get_by_role("tab", name="2. Your hands").click()
    page.get_by_label("Start from").select_option("advanced")
    page.get_by_role("tab", name="3. Arrangement").click()
    assert page.get_by_text("your hand profile has changed since").is_visible()
    page.get_by_role("button", name="Arrange for my hands").click()
    page.get_by_role("heading", level=2, name="Arrangement 2").wait_for(timeout=120_000)
    page.get_by_role("tab", name="Revisions").click()
    page.get_by_role("button", name="Compare").click()
    page.get_by_role("heading", name="What differs").wait_for()
    assert page.get_by_text(re.compile(r"^Hands: ")).count() >= 1
    check_accessibility(page, "revisions")
    shot(page, "05-revisions")

    # --- reopen: a fresh page load finds everything again -------------------------------
    page.goto(origin + "/#/library")
    page.reload()
    page.get_by_role("link", name="Browser Waltz").wait_for()
    assert "2 arrangements" in page.locator(".project-row").first.inner_text()
    check_accessibility(page, "library")
    shot(page, "06-library")
    page.get_by_role("link", name="Browser Waltz").click()
    page.get_by_role("heading", level=2, name="Arrangement 2").wait_for(timeout=60_000)
    page.locator(".notation svg g.note").first.wait_for(timeout=60_000)

    # --- account and legal pages ----------------------------------------------------------
    page.get_by_role("link", name="Account").click()
    page.get_by_role("heading", level=1, name="Account").wait_for()
    assert page.get_by_text("pianist@example.com · verified").is_visible()
    check_accessibility(page, "account")
    shot(page, "07-account")
    with page.expect_download() as info:
        page.get_by_role("button", name="Download a copy of my data").click()
    assert Path(info.value.path()).read_bytes()[:2] == b"PK"

    for name in ("privacy", "terms", "cookies", "copyright", "support"):
        page.goto(f"{origin}/{name}.html")
        page.locator("[data-config]").first.wait_for()
        page.wait_for_function("() => ![...document.querySelectorAll('[data-config]')].some((el) => el.textContent === '')")
        assert page.locator("#draft-banner").is_visible(), "an unconfigured deployment must say it is a draft"
        check_accessibility(page, f"{name}.html")
    page.goto(f"{origin}/privacy.html")
    page.locator(".missing").first.wait_for()
    assert "not provided" in page.locator("dd[data-config='operator.name']").inner_text()
    shot(page, "08-privacy")

    assert not problems.items, "the browser reported problems:\n" + "\n".join(problems.items)
    context.close()


def test_keyboard_only_on_a_phone_sized_dark_screen(server, browser, tmp_path):
    origin = server["origin"]
    midi = tmp_path / "keys.mid"
    midi.write_bytes(write_midi(waltz(4, title="Keys")))
    context = browser.new_context(viewport={"width": 320, "height": 700}, color_scheme="dark",
                                  reduced_motion="reduce", has_touch=True)
    page = context.new_page()
    problems = Problems(page)
    register_and_verify(page, server, "keyboard@example.com")
    no_sideways_scroll(page, "library on a phone")
    check_accessibility(page, "library, dark, phone")

    # The first Tab stop is the skip link, and it goes to the content.
    page.goto(origin + "/#/library")
    page.reload()
    page.get_by_role("heading", level=1, name="Your pieces").wait_for()
    page.keyboard.press("Tab")
    assert focused_text(page) == "Skip to content"
    page.keyboard.press("Enter")
    assert page.evaluate("() => document.activeElement.id") == "main"

    # The file chooser is reachable by keyboard and its focus is visible on the drop zone.
    page.locator("#upload-input").focus()
    outline = page.evaluate("() => getComputedStyle(document.querySelector('.dropzone')).outlineStyle")
    assert outline != "none", "focusing the hidden file input must draw a focus ring on the drop zone"

    page.set_input_files("#upload-input", str(midi))
    page.get_by_role("heading", level=1, name="Keys").wait_for()
    no_sideways_scroll(page, "source step on a phone")

    # Tabs follow the WAI-ARIA pattern: one Tab stop, arrows move, Home and End jump.
    page.get_by_role("tab", name="1. Source").focus()
    page.keyboard.press("ArrowRight")
    assert focused_text(page) == "2. Your hands"
    assert page.get_by_role("tab", name="2. Your hands").get_attribute("aria-selected") == "true"
    page.keyboard.press("End")
    assert focused_text(page) == "Advanced"
    page.keyboard.press("Home")
    assert focused_text(page) == "1. Source"
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert page.get_by_role("tab", name="3. Arrangement").get_attribute("aria-selected") == "true"

    # Arrange with the keyboard alone.
    page.get_by_role("button", name="Arrange for my hands").focus()
    page.keyboard.press("Enter")
    page.get_by_role("heading", level=2, name="Arrangement 1").wait_for(timeout=120_000)
    page.locator(".notation svg g.note").first.wait_for(timeout=60_000)
    no_sideways_scroll(page, "arrangement on a phone")
    check_accessibility(page, "arrangement, dark, phone")
    shot(page, "09-phone-arrangement")

    # A dialog takes focus, closes on Escape, and gives focus back to what opened it.
    page.goto(origin + "/#/library")
    rename = page.get_by_role("button", name="Rename Keys")
    rename.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Rename piece")
    dialog.wait_for()
    assert page.evaluate("() => document.activeElement.closest('dialog') !== null")
    check_accessibility(page, "rename dialog")
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label')").lower() == "rename keys"

    # Delete with the keyboard; the list says what happened.
    page.get_by_role("button", name="Delete Keys").focus()
    page.keyboard.press("Enter")
    page.get_by_role("dialog", name="Delete this piece?").wait_for()
    page.get_by_role("button", name="Delete", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_text("No pieces yet. Upload one to begin.").wait_for()

    assert not problems.items, "the browser reported problems:\n" + "\n".join(problems.items)
    context.close()


def test_a_bad_upload_and_a_wrong_password_are_explained_in_words(server, browser, tmp_path):
    context = browser.new_context(viewport={"width": 1024, "height": 800})
    page = context.new_page()
    register_and_verify(page, server, "errors@example.com")

    junk = tmp_path / "notes.mid"
    junk.write_bytes(b"this is not a MIDI file at all")
    page.set_input_files("#upload-input", str(junk))
    alert = page.locator(".panel .form-error[role=alert]")
    alert.wait_for()
    assert alert.inner_text().strip() and "Traceback" not in alert.inner_text()
    assert page.get_by_text("No pieces yet. Upload one to begin.").is_visible(), "a failed upload must leave nothing behind"

    page.get_by_role("button", name="Sign out").click()
    page.get_by_role("link", name="Sign in").first.click()
    page.get_by_label("Email address").fill("errors@example.com")
    page.get_by_label("Password").fill("definitely-the-wrong-password")
    page.get_by_role("button", name="Sign in").click()
    error = page.locator("form .form-error[role=alert]")
    error.wait_for()
    assert error.inner_text().strip()
    check_accessibility(page, "sign-in with an error showing")
    assert page.evaluate("() => location.hash") == "#/login"
    context.close()
