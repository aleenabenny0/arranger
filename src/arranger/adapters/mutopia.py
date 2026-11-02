"""The Mutopia Project catalogue: licensed music to test the arranger against.

Mutopia (mutopiaproject.org) publishes typeset public-domain and Creative
Commons music with a MIDI file and a machine-readable licence for every
piece. It is the one source the evaluation corpus already draws on
(`evals/corpus/manifest.json`), and the licensing rule in CLAUDE.md is why:
every file this module hands back names its licence, and a licence that is
not public domain or a permissive Creative Commons one is refused before a
single byte of music is downloaded.

Three pages are read, all with the standard library:

- the search table (`cgibin/make-table.cgi`), ten pieces a page, each with
  title, composer, instruments, style, licence, piece id and download links;
- a piece page (`cgibin/piece-info.cgi?id=N`), for the same facts about one
  piece;
- the RSS feed of latest additions, which carries titles and piece ids only.

Every request goes through one client that identifies itself, waits between
requests, caps what it will read, and retries only on the server's failures.
Its `fetch` is injectable, so the parsers and the client's manners are tested
against recorded pages and never against the live site.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Callable
from xml.etree import ElementTree

from ..limits import DEFAULT_LIMITS

BASE_URL = "https://www.mutopiaproject.org"
SEARCH_URL = f"{BASE_URL}/cgibin/make-table.cgi"
PIECE_URL = f"{BASE_URL}/cgibin/piece-info.cgi"
RSS_URL = f"{BASE_URL}/latestadditions.rss"
PAGE_SIZE = 10

USER_AGENT = "arranger-autotest/1.0 (+https://github.com/aleenabenny0/arranger)"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = DEFAULT_LIMITS.max_bytes
MIN_INTERVAL_SECONDS = 1.0
TIMEOUT_SECONDS = 30.0

# Mutopia's legal.html anchors. These two Creative Commons families and the
# public domain are what CLAUDE.md's corpus rule allows; NonCommercial and
# NoDerivatives variants have their own anchors and are not in this set.
PERMITTED_LICENSE_ANCHORS = frozenset({"publicdomain", "cca", "ccasa"})

_PIECE_ID = re.compile(r"piece-info\.cgi\?id=(\d+)")
_MUSIC_ID = re.compile(r"Mutopia-\d{4}/\d{2}/\d{2}-(\d+)")
_LEGAL_ANCHOR = re.compile(r"legal\.html#([a-z0-9]+)")
_DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
_UPDATED = re.compile(r"\d{4}/[A-Za-z]{3}/\d{2}")
_WHITESPACE = re.compile(r"\s+")
_LABELS = (
    "instrument(s)", "style", "opus", "copyright", "source", "download",
    "date of composition", "last updated", "music id number", "typeset using", "maintainer",
)


class CatalogueError(RuntimeError):
    """The site answered in a way this module will not act on."""


class LicenseNotPermitted(CatalogueError):
    """The piece's licence is outside the corpus rule. Nothing was downloaded."""


@dataclass(frozen=True)
class CataloguePiece:
    piece_id: int
    title: str
    composer: str
    instruments: str
    style: str
    license: str            # as the site prints it: "Public Domain", "Creative Commons Attribution 3.0"
    license_anchor: str     # "publicdomain" | "cca" | "ccasa" | anything else the site uses
    midi_url: str | None
    source_page: str
    ly_url: str | None = None
    date: str = ""          # when the typesetting was published, "2019/07/24"
    opus: str = ""

    @property
    def permitted(self) -> bool:
        return self.license_anchor in PERMITTED_LICENSE_ANCHORS

    @property
    def slug(self) -> str:
        """A file-name-safe id: composer surname, title words, piece id."""
        words = re.sub(r"[^a-z0-9]+", "-", f"{self.composer} {self.title}".lower()).strip("-")
        return f"mutopia-{self.piece_id}-{words[:60].strip('-')}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["permitted"] = self.permitted
        data["slug"] = self.slug
        return data


@dataclass(frozen=True)
class FeedItem:
    piece_id: int
    title: str
    description: str
    published: str


# --- parsing --------------------------------------------------------------


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", unescape(text)).strip()


class _Cells(HTMLParser):
    """Collect every `result-table` on a page as a list of (text, hrefs) cells.

    Headings are kept too: a piece page names the piece in `<h2>` and the
    composer in `<h4>`, outside any table.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[tuple[str, list[str]]]] = []
        self.headings: list[tuple[str, str]] = []
        self._table: list[tuple[str, list[str]]] | None = None
        self._cell_text: list[str] | None = None
        self._cell_hrefs: list[str] = []
        self._heading: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and "result-table" in (attributes.get("class") or ""):
            self._table = []
        elif tag == "td" and self._table is not None:
            self._cell_text, self._cell_hrefs = [], []
        elif tag == "a" and self._cell_text is not None:
            href = attributes.get("href")
            if href:
                self._cell_hrefs.append(href)
        elif tag in ("h2", "h4"):
            self._heading = (tag, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._table is not None and self._cell_text is not None:
            self._table.append((_clean("".join(self._cell_text)), self._cell_hrefs))
            self._cell_text, self._cell_hrefs = None, []
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag in ("h2", "h4") and self._heading is not None:
            self.headings.append((tag, _clean("".join(self._heading[1]))))
            self._heading = None

    def handle_data(self, data: str) -> None:
        if self._cell_text is not None:
            self._cell_text.append(data)
        if self._heading is not None:
            self._heading[1].append(data)


def _absolute(url: str) -> str:
    return urllib.parse.urljoin(f"{BASE_URL}/cgibin/", url)


def _piece_from_cells(cells: list[tuple[str, list[str]]], *, fallback_title: str = "",
                      fallback_composer: str = "") -> CataloguePiece | None:
    piece_id: int | None = None
    midi_url = ly_url = None
    license_text, license_anchor = "", ""
    source_page = ""
    for text, hrefs in cells:
        for href in hrefs:
            if (m := _PIECE_ID.search(href)) and piece_id is None:
                piece_id = int(m.group(1))
                source_page = _absolute(href)
            elif href.lower().endswith(".mid"):
                midi_url = _absolute(href)
            elif href.lower().endswith(".ly"):
                ly_url = _absolute(href)
            elif (m := _LEGAL_ANCHOR.search(href)) and not license_anchor:
                license_anchor = m.group(1)
                license_text = text.split(":", 1)[-1].strip() if text.lower().startswith("copyright") else text
    labelled = {}
    plain: list[str] = []
    for text, _hrefs in cells:
        if ":" in text and text.split(":", 1)[0].strip().lower() in _LABELS:
            # The piece page writes "Instrument(s): Piano" in each cell; the
            # opus cell carries a second label after a comma ("Opus: 34, Meter: ...").
            label, value = text.split(":", 1)
            labelled[label.strip().lower()] = value.strip()
        else:
            plain.append(text)
    if piece_id is None and (m := _MUSIC_ID.search(labelled.get("music id number", ""))):
        piece_id = int(m.group(1))   # a piece page names itself, it does not link to itself
    if piece_id is None and not midi_url:
        return None

    title = fallback_title or (plain[0] if plain else "")
    composer = fallback_composer
    instruments = labelled.get("instrument(s)", "")
    style = labelled.get("style", "")
    opus = labelled.get("opus", "")
    date = ""
    if m := _UPDATED.search(labelled.get("last updated", "")):
        date = m.group(0)
    for position, (text, _hrefs) in enumerate(cells):
        if text.startswith("by ") and not composer:
            composer = text[3:]
        elif text.startswith("for ") and not instruments:
            # The search table's second row is: "for <instruments>", the date
            # of composition (often blank), the style. Positions, not content,
            # tell them apart: a date of composition can read "c. 1846".
            instruments = text[4:]
            if not style and position + 2 < len(cells):
                style = cells[position + 2][0]
        elif _DATE.match(text):
            date = text
    composer = re.sub(r"\s*\([^)]*\)\s*$", "", composer)   # "F. Abt (1819–1885)" -> "F. Abt"
    return CataloguePiece(
        piece_id=piece_id if piece_id is not None else -1,
        title=title, composer=composer, instruments=instruments, style=style,
        license=license_text, license_anchor=license_anchor, midi_url=midi_url,
        source_page=source_page or (f"{PIECE_URL}?id={piece_id}" if piece_id is not None else ""),
        ly_url=ly_url, date=date, opus=opus,
    )


def parse_search_page(html_text: str) -> list[CataloguePiece]:
    """The pieces listed on one search-results page, in page order."""
    parser = _Cells()
    parser.feed(html_text)
    pieces = []
    for table in parser.tables:
        piece = _piece_from_cells(table)
        if piece is not None:
            pieces.append(piece)
    return pieces


def parse_piece_page(html_text: str, *, piece_id: int | None = None) -> CataloguePiece:
    """One piece's facts from its own page."""
    parser = _Cells()
    parser.feed(html_text)
    title = next((text for tag, text in parser.headings if tag == "h2"), "")
    composer = next((text for tag, text in parser.headings if tag == "h4"), "")
    if composer.startswith("by "):
        composer = composer[3:]
    cells = [cell for table in parser.tables for cell in table]
    piece = _piece_from_cells(cells, fallback_title=title, fallback_composer=composer)
    if piece is None or (piece.piece_id < 0 and piece_id is None):
        raise CatalogueError("not a Mutopia piece page")
    if piece.piece_id < 0 and piece_id is not None:
        piece = CataloguePiece(**{**asdict(piece), "piece_id": piece_id,
                                  "source_page": f"{PIECE_URL}?id={piece_id}"})
    return piece


def parse_feed(xml_text: str | bytes) -> list[FeedItem]:
    """Latest additions, newest first. Titles and ids only; see `piece()` for the rest."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise CatalogueError(f"feed is not XML: {exc}") from exc
    items = []
    for item in root.iter("item"):
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        m = _PIECE_ID.search(link)
        if not m:
            continue
        items.append(FeedItem(
            piece_id=int(m.group(1)),
            title=_clean(item.findtext("title") or ""),
            description=_clean(item.findtext("description") or ""),
            published=_clean(item.findtext("pubDate") or ""),
        ))
    return items


# --- the client -------------------------------------------------------------


def _default_fetch(url: str, *, max_bytes: int, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only, fixed host
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise CatalogueError(f"{url} is larger than {max_bytes} bytes")
    return data


class MutopiaCatalogue:
    """A polite client for the catalogue. One instance per run.

    `fetch(url, max_bytes=..., timeout=...)` does the network; tests pass a
    function that reads recorded pages. `sleep` and `clock` exist for the
    same reason: the pause between requests is asserted, not waited for.
    """

    def __init__(
        self,
        fetch: Callable[..., bytes] | None = None,
        *,
        min_interval: float = MIN_INTERVAL_SECONDS,
        max_page_bytes: int = MAX_PAGE_BYTES,
        max_file_bytes: int = MAX_FILE_BYTES,
        timeout: float = TIMEOUT_SECONDS,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self.min_interval = min_interval
        self.max_page_bytes = max_page_bytes
        self.max_file_bytes = max_file_bytes
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.requests: list[str] = []

    def _get(self, url: str, *, max_bytes: int) -> bytes:
        if not url.startswith(BASE_URL + "/"):
            raise CatalogueError(f"refusing to fetch {url}: not on {BASE_URL}")
        attempt = 0
        while True:
            now = self._clock()
            if self._last_request is not None:
                wait = self.min_interval - (now - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._clock()
            self.requests.append(url)
            try:
                return self._fetch(url, max_bytes=max_bytes, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    attempt += 1
                    self._sleep(2.0 * attempt)
                    continue
                raise CatalogueError(f"{url}: HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                if attempt < self.retries:
                    attempt += 1
                    self._sleep(2.0 * attempt)
                    continue
                raise CatalogueError(f"{url}: {exc.reason}") from exc

    def _text(self, url: str) -> str:
        return self._get(url, max_bytes=self.max_page_bytes).decode("utf-8", errors="replace")

    def search(self, *, instrument: str = "Piano", start_at: int = 0, query: str = "") -> list[CataloguePiece]:
        """One page of results. Empty when `start_at` is past the end."""
        params = urllib.parse.urlencode({
            "Instrument": instrument, "startat": max(0, int(start_at)), "searchingfor": query,
        })
        return parse_search_page(self._text(f"{SEARCH_URL}?{params}"))

    def iter_search(
        self, *, instrument: str = "Piano", start_at: int = 0, max_pages: int | None = None,
        query: str = "",
    ) -> Iterator[CataloguePiece]:
        """Walk the results page by page until a page comes back empty."""
        pages = 0
        offset = start_at
        while max_pages is None or pages < max_pages:
            found = self.search(instrument=instrument, start_at=offset, query=query)
            if not found:
                return
            yield from found
            pages += 1
            offset += PAGE_SIZE

    def latest(self) -> list[FeedItem]:
        return parse_feed(self._get(RSS_URL, max_bytes=self.max_page_bytes))

    def piece(self, piece_id: int) -> CataloguePiece:
        return parse_piece_page(self._text(f"{PIECE_URL}?id={int(piece_id)}"), piece_id=int(piece_id))

    def download(self, piece: CataloguePiece) -> bytes:
        """The piece's MIDI bytes. Refuses before the request if the licence is not permitted."""
        if not piece.permitted:
            raise LicenseNotPermitted(
                f"{piece.title} ({piece.source_page}) is licensed '{piece.license or piece.license_anchor}', "
                "which the corpus rule does not allow"
            )
        if not piece.midi_url:
            raise CatalogueError(f"{piece.title} has no MIDI file on Mutopia")
        return self._get(piece.midi_url, max_bytes=self.max_file_bytes)
