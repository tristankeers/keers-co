"""Command line: ``python -m keers.universe build|search|show|stats|check|export``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any

from ..data.http import Fetcher
from ..data.refresh import DEFAULT_DB, user_agent
from ..data.store import Store
from .build import Builder, check
from .directory import COLUMNS, Universe
from .exchanges import BY_CODE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m keers.universe")
    ap.add_argument("--db", default=os.environ.get("KEERS_DB", DEFAULT_DB))
    ap.add_argument(
        "--keep-raw", action="store_true", help="store full response bodies (hashes are always kept)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="fetch listings from SEC, Nasdaq Trader and OpenFIGI")
    b.add_argument("--exchanges", help="comma-separated Bloomberg codes, e.g. LN,JT (default: all non-US)")
    b.add_argument("--no-us", action="store_true", help="skip the US segment")
    b.add_argument(
        "--max-pages", type=int, help="cap OpenFIGI pages per exchange (a test build; rows are partial)"
    )
    s = sub.add_parser("search", help="search by name, ticker or Yahoo symbol")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--country")
    s.add_argument("--all", action="store_true", help="include secondary listings")
    sh = sub.add_parser("show", help="one listing by key (XLON:VOD) or Yahoo symbol (VOD.L)")
    sh.add_argument("key")
    sub.add_parser("stats", help="counts by exchange: where coverage is thin")
    c = sub.add_parser("check", help="check listings against Yahoo (symbol, currency, name)")
    c.add_argument("keys", nargs="*")
    c.add_argument("--sample", type=int, help="check this many primary listings per exchange")
    c.add_argument("--exchanges", help="limit --sample to these Bloomberg codes")
    e = sub.add_parser("export", help="write the universe to CSV")
    e.add_argument("path")
    e.add_argument("--primary-only", action="store_true")
    a = ap.parse_args(argv)

    store = Store(a.db, keep_bodies=a.keep_raw)
    uni = Universe(store)
    out: Any
    if a.cmd == "build":
        figi_key = (os.environ.get("OPENFIGI_API_KEY") or "").strip() or None
        codes = [x.strip() for x in a.exchanges.split(",")] if a.exchanges else None
        bad = [x for x in codes or [] if x.upper() not in BY_CODE]
        if bad:
            ap.error(f"unknown exchange code(s) {bad}; known: {', '.join(sorted(BY_CODE))}")
        builder = Builder(Fetcher(user_agent()), store, figi_key)
        out = builder.build(codes, a.max_pages, us=not a.no_us, progress=lambda m: print(m, file=sys.stderr))
        print(json.dumps(out, indent=2))
        return 0 if out["status"] in ("ok", "partial") or (a.max_pages and out["status"] != "failed") else 1
    if a.cmd == "search":
        out = [x.as_dict() for x in uni.search(a.query, a.limit, not a.all, a.country)]
    elif a.cmd == "show":
        x = uni.get(a.key)
        if x is None:
            print(json.dumps({"error": f"{a.key}: not in the universe"}))
            return 1
        out = x.as_dict()
    elif a.cmd == "stats":
        out = uni.stats()
    elif a.cmd == "check":
        listings = [x for k in a.keys if (x := uni.get(k)) is not None]
        if a.sample:
            want = {f"openfigi:{c.strip().upper()}" for c in a.exchanges.split(",")} if a.exchanges else None
            per: dict[str, int] = {}
            for x in uni.all(primary_only=True):
                if (want is None or x.segment in want) and per.get(x.segment, 0) < a.sample:
                    per[x.segment] = per.get(x.segment, 0) + 1
                    listings.append(x)
        ua = os.environ.get("SEC_USER_AGENT", "").strip() or "keers-research"
        out = check(Fetcher(ua), store, listings)
    else:
        rows = uni.all(primary_only=a.primary_only)
        with open(a.path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for x in rows:
                w.writerow(x.row())
        out = {"wrote": len(rows), "path": a.path}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
