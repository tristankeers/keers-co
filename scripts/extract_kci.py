"""Extract the KCI engine from KCI's published index.html, or check the committed copy.

KCI's original source code was not recoverable, so the published page is the
source of record. This script cuts fixed line ranges out of that page. It does
not change a single byte of what it cuts.

    python scripts/extract_kci.py --check              # verify committed files (downloads the pinned page)
    python scripts/extract_kci.py --check --html PATH  # verify against a local copy
    python scripts/extract_kci.py --write --html PATH  # (re)write the files
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy" / "kci"
MANIFEST = LEGACY / "SOURCE.json"

SEED_PREFIX = "<script>window.KCI_PUBLIC = true; window.KCI_SEED = "
SEED_SUFFIX = ";</script>"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_html(manifest: dict, html_path: str | None) -> bytes:
    if html_path:
        data = Path(html_path).read_bytes()
    else:
        with urllib.request.urlopen(manifest["source"]["raw_url"], timeout=60) as resp:  # noqa: S310 (pinned https URL)
            data = resp.read()
    expected = manifest["source"]["sha256"]
    if sha256(data) != expected:
        sys.exit(f"index.html hash {sha256(data)} does not match pinned {expected}")
    return data


def cut(html: bytes, manifest: dict) -> dict[str, bytes]:
    lines = html.decode("utf-8").split("\n")
    out: dict[str, bytes] = {}
    for name, spec in manifest["slices"].items():
        start, end = spec["lines"]
        text = "\n".join(lines[start - 1 : end]) + "\n"
        if name == "seed.json":
            assert text.startswith(SEED_PREFIX) and text.rstrip("\n").endswith(SEED_SUFFIX), "seed line moved"
            text = text[len(SEED_PREFIX) :].rstrip("\n")[: -len(SEED_SUFFIX)] + "\n"
        out[name] = text.encode("utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    ap.add_argument("--html", help="local copy of KCI index.html at the pinned commit")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    files = cut(load_html(manifest, args.html), manifest)

    failed = False
    for name, data in files.items():
        target = LEGACY / name
        if args.write:
            target.write_bytes(data)
            manifest["slices"][name]["sha256"] = sha256(data)
            print(f"wrote {name} ({len(data)} bytes)")
        else:
            ok = target.read_bytes() == data and sha256(data) == manifest["slices"][name]["sha256"]
            print(f"{'ok  ' if ok else 'FAIL'} {name}")
            failed |= not ok
    if args.write:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
