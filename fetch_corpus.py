"""Download the eval corpus from evals/corpus/manifest.json.

    python fetch_corpus.py            # fetch everything not already verified
    python fetch_corpus.py joplin-solace bach-invention-04-bwv775

Why a manifest plus a fetch script, instead of committing the MIDI files:
the corpus licensing rule (CLAUDE.md) says public-domain/CC audio material
must stay out of the repo even when it's properly licensed -- it is still
binary content that doesn't belong in git history, and it leaks into a
commit eventually if it's sitting in the tree. The manifest is the
redistributable part: composer, license, source URL, and a sha256 so a
fresh clone gets exactly the same bytes every contributor already checked,
without ever downloading anything Claude (or anyone) hasn't verified.

Every entry in the manifest was fetched and hashed once, by hand, before
being recorded there. This script's job on every run after that is just to
reproduce the same bytes -- if a download doesn't match its recorded
sha256, that is treated as a hard failure, not a warning, because a
manifest that silently accepts different bytes than it claims to isn't a
manifest.

Most Mutopia pieces publish a bare .mid. Multi-movement works (concertos,
symphonies, quartets) instead publish a *-mids.zip with one member per
movement plus per-instrument part files. A manifest entry for one of those
gives `zip_url` + `zip_member` instead of `midi_url`; the sha256 recorded
against it is of the *extracted member's bytes*, not the zip -- from the
corpus's point of view a zip is just where this piece's one .mid happens
to live, same as any other file being at a URL.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "evals" / "corpus"
MANIFEST = CORPUS_DIR / "manifest.json"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _acquire(piece: dict) -> bytes:
    """Get this piece's raw MIDI bytes, from a direct URL or a zip member."""
    if "zip_url" in piece:
        with urllib.request.urlopen(piece["zip_url"], timeout=30) as resp:
            zip_bytes = resp.read()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            return zf.read(piece["zip_member"])
    with urllib.request.urlopen(piece["midi_url"], timeout=30) as resp:
        return resp.read()


def fetch_one(piece: dict) -> tuple[str, str]:
    """Returns (status, detail).

    status is one of: ok, cached, unrecorded, mismatch, error. `unrecorded`
    is the path for a manifest entry mid-way through being added -- see
    "Extending the corpus" in evals/corpus/README.md: it downloads the file
    and hands back the hash to paste into the manifest, rather than failing
    with a KeyError on a field that isn't there yet.
    """
    dest = CORPUS_DIR / f"{piece['id']}.mid"
    expected = piece.get("sha256")

    if dest.exists() and expected is not None:
        existing = sha256_of(dest.read_bytes())
        if existing == expected:
            return "cached", f"{dest.stat().st_size} bytes, verified"
        # Stale or corrupt local copy. Fall through and re-fetch rather than
        # trusting a file that doesn't match what the manifest recorded.

    try:
        data = _acquire(piece)
    except urllib.error.URLError as exc:
        return "error", f"download failed: {exc}"
    except zipfile.BadZipFile as exc:
        return "error", f"not a valid zip: {exc}"
    except KeyError as exc:
        return "error", f"zip_member {exc} not found in archive"

    got = sha256_of(data)

    if expected is None:
        dest.write_bytes(data)
        return (
            "unrecorded",
            f'{len(data)} bytes -- add "bytes": {len(data)}, '
            f'"sha256": "{got}" to manifest.json',
        )

    if got != expected:
        return (
            "mismatch",
            f"expected sha256 {expected}, got {got} "
            f"({len(data)} bytes) -- upstream file may have changed; "
            "do not use this copy without checking",
        )

    dest.write_bytes(data)
    return "ok", f"{len(data)} bytes, verified"


def main(argv: list[str]) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    pieces = manifest["pieces"]

    if argv:
        wanted = set(argv)
        pieces = [p for p in pieces if p["id"] in wanted]
        missing = wanted - {p["id"] for p in pieces}
        if missing:
            print(f"unknown id(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    markers = {
        "ok": "fetched",
        "cached": "cached ",
        "unrecorded": "NEW    ",
        "mismatch": "FAILED ",
        "error": "FAILED ",
    }
    failures, pending = [], []
    for piece in pieces:
        status, detail = fetch_one(piece)
        print(f"  {markers[status]}  {piece['id']:<40} {detail}")
        if status in ("mismatch", "error"):
            failures.append(piece["id"])
        elif status == "unrecorded":
            pending.append(piece["id"])

    print()
    if failures:
        print(f"{len(failures)}/{len(pieces)} piece(s) failed: {', '.join(failures)}")
        return 1
    if pending:
        print(f"{len(pending)} piece(s) downloaded but not yet recorded in manifest.json: {', '.join(pending)}")
        return 1

    print(f"{len(pieces)} piece(s) verified in {CORPUS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
