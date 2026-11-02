"""The Mutopia catalogue adapter, against recorded pages. Nothing here goes online."""

import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters import mutopia  # noqa: E402
from arranger.adapters.mutopia import (  # noqa: E402
    BASE_URL,
    MAX_FILE_BYTES,
    MAX_PAGE_BYTES,
    PAGE_SIZE,
    CataloguePiece,
    CatalogueError,
    LicenseNotPermitted,
    MutopiaCatalogue,
    parse_feed,
    parse_piece_page,
    parse_search_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mutopia"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def result_table(piece_id: int, title: str, anchor: str, licence: str, midi: str = "x.mid") -> str:
    """One search-result block in Mutopia's layout."""
    return f"""
<table class="table-bordered result-table">
<tr><td>{title}</td><td>by A. Composer (1800–1870)</td><td>&nbsp;</td><td>&nbsp;</td></tr>
<tr><td>for Piano</td><td>c. 1846</td><td>Romantic</td><td></td></tr>
<tr><td>Source</td><td><a href="../legal.html#{anchor}">{licence}</a></td>
<td><a href="piece-info.cgi?id={piece_id}">More Information</a></td><td>2019/07/24</td></tr>
<tr><td>Download: <a href="{BASE_URL}/ftp/A/{midi.replace('.mid', '.ly')}">.ly file</a></td>
<td><a href="{BASE_URL}/ftp/A/{midi}">.mid file</a></td><td></td><td></td></tr>
</table>"""


# --- parsing --------------------------------------------------------------------


def test_search_page_lists_every_piece_with_its_facts():
    pieces = parse_search_page(fixture("search-piano-page1.html"))
    assert len(pieces) == PAGE_SIZE
    first = pieces[0]
    assert first.piece_id == 2247 and first.title == "Vocalise № 1" and first.composer == "F. Abt"
    assert first.instruments == "Voice, Piano" and first.style == "Romantic" and first.date == "2019/07/24"
    assert first.license == "Public Domain" and first.license_anchor == "publicdomain" and first.permitted
    assert first.midi_url == f"{BASE_URL}/ftp/AbtF/Vocalise1/Vocalise1.mid"
    assert first.ly_url.endswith("Vocalise1.ly") and first.source_page.endswith("piece-info.cgi?id=2247")
    assert first.slug == "mutopia-2247-f-abt-vocalise-1"
    # The date of composition sits between the instruments and the style and
    # is often blank; a piece with one must not have it read as its style.
    swallows = next(p for p in pieces if p.piece_id == 439)
    assert swallows.style == "Song" and swallows.instruments == "Voice and Piano"
    sonatine = next(p for p in pieces if p.piece_id == 207)
    assert sonatine.style == "Classical" and sonatine.composer == "J. André"
    assert {p.license_anchor for p in pieces} <= {"publicdomain", "cca", "ccasa"}


def test_an_empty_search_page_is_an_empty_list():
    assert parse_search_page(fixture("search-empty.html")) == []
    assert parse_search_page("<html><body>nothing</body></html>") == []


def test_piece_page_names_itself_and_its_licence():
    piece = parse_piece_page(fixture("piece-207.html"))
    assert piece.piece_id == 207 and piece.title == "Sonatine" and piece.composer == "J. André"
    assert piece.instruments == "Piano" and piece.style == "Classical"
    assert piece.license == "Public Domain" and piece.permitted
    assert piece.midi_url.endswith("andre-sonatine.mid") and piece.date == "2013/Jan/06"
    assert piece.opus.startswith("Opus 34")
    shared = parse_piece_page(fixture("piece-1313.html"))
    assert shared.piece_id == 1313 and shared.license_anchor == "ccasa" and shared.permitted
    assert shared.license.startswith("Creative Commons Attribution-ShareAlike")


def test_a_page_that_is_not_a_piece_is_refused():
    with pytest.raises(CatalogueError):
        parse_piece_page("<html><h2>Not found</h2></html>")


def test_feed_carries_ids_and_titles_newest_first():
    items = parse_feed((FIXTURES / "latestadditions.rss").read_bytes())
    assert len(items) == 25
    assert items[0].piece_id == 2247 and items[0].title == "AbtF: Vocalise № 1"
    assert "Voice, Piano" in items[0].description and items[0].published.startswith("Wed, 24 Jul 2019")
    with pytest.raises(CatalogueError):
        parse_feed("<rss><channel><item><title>x</title></item></channel>")


def test_only_public_domain_and_permissive_creative_commons_are_permitted():
    page = (result_table(1, "Free", "publicdomain", "Public Domain")
            + result_table(2, "Shared", "ccasa", "Creative Commons Attribution-ShareAlike 4.0")
            + result_table(3, "Credited", "cca", "Creative Commons Attribution 4.0")
            + result_table(4, "Restricted", "ccancsa", "Creative Commons Attribution-NonCommercial-ShareAlike"))
    pieces = parse_search_page(page)
    assert [p.permitted for p in pieces] == [True, True, True, False]
    assert pieces[3].license_anchor == "ccancsa"


# --- the client ---------------------------------------------------------------------


class FakeSite:
    """Serves the recorded pages and a tiny MIDI file; records every request."""

    def __init__(self, *, fail_first: int = 0, status: int = 503):
        self.urls: list[str] = []
        self.limits: list[int] = []
        self.fail_first = fail_first
        self.status = status
        self.sleeps: list[float] = []
        self.now = 1000.0

    def fetch(self, url: str, *, max_bytes: int, timeout: float) -> bytes:
        self.urls.append(url)
        self.limits.append(max_bytes)
        if self.fail_first:
            self.fail_first -= 1
            raise urllib.error.HTTPError(url, self.status, "down", {}, None)
        if url.endswith(".mid"):
            return b"MThd" + bytes(10)
        if url.endswith(".rss"):
            return (FIXTURES / "latestadditions.rss").read_bytes()
        if "piece-info.cgi?id=207" in url:
            return fixture("piece-207.html").encode("utf-8")
        if "make-table.cgi" in url:
            if "startat=0" in url:
                return fixture("search-piano-page1.html").encode("utf-8")
            return fixture("search-empty.html").encode("utf-8")
        raise urllib.error.HTTPError(url, 404, "no", {}, None)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def client(self, **kwargs) -> MutopiaCatalogue:
        return MutopiaCatalogue(self.fetch, sleep=self.sleep, clock=self.clock, **kwargs)


def test_search_builds_the_query_and_walks_pages_until_one_is_empty():
    site = FakeSite()
    client = site.client()
    found = list(client.iter_search(instrument="Piano"))
    assert len(found) == PAGE_SIZE and found[0].piece_id == 2247
    assert site.urls[0].startswith(f"{BASE_URL}/cgibin/make-table.cgi?")
    assert "Instrument=Piano" in site.urls[0] and "startat=0" in site.urls[0]
    assert "startat=10" in site.urls[1] and len(site.urls) == 2
    assert site.limits == [MAX_PAGE_BYTES, MAX_PAGE_BYTES]


def test_requests_are_spaced_out():
    site = FakeSite()
    client = site.client(min_interval=1.5)
    client.search()
    client.search(start_at=10)
    client.latest()
    assert site.sleeps == [1.5, 1.5], "every request after the first waits the full interval"
    assert len(client.requests) == 3


def test_server_errors_are_retried_and_client_errors_are_not():
    site = FakeSite(fail_first=1, status=503)
    piece = site.client().piece(207)
    assert piece.piece_id == 207 and len(site.urls) == 2 and site.sleeps[0] == 2.0
    site = FakeSite(fail_first=3, status=503)
    with pytest.raises(CatalogueError):
        site.client(retries=2).piece(207)
    assert len(site.urls) == 3
    site = FakeSite(fail_first=1, status=404)
    with pytest.raises(CatalogueError):
        site.client().piece(207)
    assert len(site.urls) == 1, "a 404 is an answer, not a transient failure"


def test_download_refuses_before_requesting_when_the_licence_is_not_permitted():
    site = FakeSite()
    restricted = CataloguePiece(4, "Restricted", "A", "Piano", "", "CC NC", "ccancsa",
                                f"{BASE_URL}/ftp/A/x.mid", f"{BASE_URL}/cgibin/piece-info.cgi?id=4")
    with pytest.raises(LicenseNotPermitted):
        site.client().download(restricted)
    assert site.urls == []
    free = CataloguePiece(1, "Free", "A", "Piano", "", "Public Domain", "publicdomain",
                          f"{BASE_URL}/ftp/A/x.mid", f"{BASE_URL}/cgibin/piece-info.cgi?id=1")
    data = site.client().download(free)
    assert data.startswith(b"MThd") and site.limits == [MAX_FILE_BYTES]
    silent = CataloguePiece(1, "Free", "A", "Piano", "", "Public Domain", "publicdomain", None, "")
    with pytest.raises(CatalogueError):
        site.client().download(silent)


def test_the_client_never_leaves_the_site():
    site = FakeSite()
    elsewhere = CataloguePiece(1, "x", "A", "Piano", "", "Public Domain", "publicdomain",
                               "https://example.com/x.mid", "")
    with pytest.raises(CatalogueError):
        site.client().download(elsewhere)
    assert site.urls == []


def test_default_fetch_caps_what_it_reads(monkeypatch):
    class Response:
        def __init__(self, size):
            self.size = size

        def read(self, n):
            return b"x" * min(n, self.size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = {}

    def fake_urlopen(request, timeout):
        seen["agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Response(seen["size"])

    monkeypatch.setattr(mutopia.urllib.request, "urlopen", fake_urlopen)
    seen["size"] = 10
    assert mutopia._default_fetch(f"{BASE_URL}/x", max_bytes=100, timeout=7) == b"x" * 10
    assert seen["agent"].startswith("arranger-autotest") and seen["timeout"] == 7
    seen["size"] = 101
    with pytest.raises(CatalogueError):
        mutopia._default_fetch(f"{BASE_URL}/x", max_bytes=100, timeout=7)
