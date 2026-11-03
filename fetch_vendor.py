"""Download the third-party browser code the frontend needs, and verify it.

    python fetch_vendor.py          # what the running app needs
    python fetch_vendor.py --dev    # also the accessibility checker the browser tests use

The notation preview is rendered in the browser by Verovio (LGPL-3.0-or-later,
https://www.verovio.org), used unmodified. It is a 7 MB WebAssembly build, so
it is fetched at install or image-build time rather than committed, the same
way `fetch_corpus.py` handles the evaluation corpus. The version and checksums
are pinned: if the bytes ever differ, this fails loudly instead of serving
whatever came back.

`--dev` also fetches axe-core (MPL-2.0, https://github.com/dequelabs/axe-core),
which the browser tests inject into pages to check them. It goes to
`.cache/tools/`, never into `frontend/`, so it is never served to users.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "frontend" / "vendor"
DEV_TOOLS = ROOT / ".cache" / "tools"

PACKAGES = [
    {
        "name": "verovio",
        "version": "6.3.0",
        "tarball": "https://registry.npmjs.org/verovio/-/verovio-6.3.0.tgz",
        "tarball_sha256": "503fa08873eea0b49208bed2450835603c49835abf0faacca5357bb9a9ca0a41",
        "files": {
            "package/dist/verovio-toolkit-wasm.js": (
                "verovio-toolkit-wasm.js",
                "d794119cd5ea83a3e835849945f0289c91ac2bac427f4aa312c7010a800b46bd",
            ),
        },
    },
]
DEV_PACKAGES = [
    {
        "name": "axe-core",
        "version": "4.10.3",
        "tarball": "https://registry.npmjs.org/axe-core/-/axe-core-4.10.3.tgz",
        "tarball_sha256": "0f2b4d7dcdf7d1219df8d1959ad68e565f51d14c3f0d88bb71cd59abeb956292",
        "files": {
            "package/axe.min.js": (
                "axe.min.js",
                "880970c081707360e64f34cea25ff91892f5bc95675b0776925b9709dd8a68bb",
            ),
        },
    },
]
MAX_TARBALL_BYTES = 64 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(package: dict, target: Path = VENDOR) -> None:
    wanted = package["files"]
    if all((target / out).is_file() and sha256((target / out).read_bytes()) == digest
           for out, digest in wanted.values()):
        print(f"  ok       {package['name']} {package['version']} (already present, verified)")
        return
    print(f"  fetching {package['name']} {package['version']} ...")
    with urllib.request.urlopen(package["tarball"], timeout=120) as response:  # noqa: S310 - pinned https URL
        data = response.read(MAX_TARBALL_BYTES + 1)
    if len(data) > MAX_TARBALL_BYTES:
        raise SystemExit(f"{package['name']}: download is larger than expected; refusing it")
    if sha256(data) != package["tarball_sha256"]:
        raise SystemExit(
            f"{package['name']}: checksum mismatch.\n  expected {package['tarball_sha256']}\n"
            f"  got      {sha256(data)}\nThe upstream file changed or the download was tampered with."
        )
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member_name, (out_name, digest) in wanted.items():
            member = archive.extractfile(member_name)   # read by exact name; nothing is extracted to disk
            if member is None:
                raise SystemExit(f"{package['name']}: {member_name} is missing from the package")
            blob = member.read()
            if sha256(blob) != digest:
                raise SystemExit(f"{package['name']}: {member_name} does not match its pinned checksum")
            (target / out_name).write_bytes(blob)
            print(f"  wrote    {(target / out_name).relative_to(ROOT).as_posix()} ({len(blob):,} bytes, verified)")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    unknown = [a for a in args if a != "--dev"]
    if unknown:
        raise SystemExit(f"unknown argument: {unknown[0]} (the only option is --dev)")
    for package in PACKAGES:
        fetch(package)
    if "--dev" in args:
        for package in DEV_PACKAGES:
            fetch(package, DEV_TOOLS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
