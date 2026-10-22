"""Command line: ``python -m keers.data refresh|snapshot|status``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from . import snapshot
from .http import Fetcher
from .refresh import DEFAULT_DB, read_universe, refresh, user_agent
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m keers.data")
    ap.add_argument("--db", default=os.environ.get("KEERS_DB", DEFAULT_DB))
    ap.add_argument(
        "--keep-raw", action="store_true", help="store full response bodies (hashes and URLs are always kept)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="fetch filings, prices and rates")
    r.add_argument("tickers", nargs="*")
    r.add_argument("--universe", help="file with one ticker per line")
    r.add_argument("--no-rates", action="store_true")
    s = sub.add_parser("snapshot", help="print a company snapshot as JSON")
    s.add_argument("ticker")
    s.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    s.add_argument("--analyse", action="store_true", help="also run KCI v1 if the snapshot is not blocked")
    sub.add_parser("status", help="show recent refresh runs")
    a = ap.parse_args(argv)

    store = Store(a.db, keep_bodies=a.keep_raw)
    if a.cmd == "refresh":
        tickers = [t.upper() for t in a.tickers] + (read_universe(a.universe) if a.universe else [])
        if not tickers:
            ap.error("give tickers or --universe")
        res = refresh(store, tickers, Fetcher(user_agent()), os.environ.get("TIINGO_TOKEN") or None,
                      rates=not a.no_rates)  # fmt: skip
        print(
            json.dumps({"status": res["status"], "ok": len(res["done"]), "failed": res["failed"]}, indent=2)
        )
        return 0 if res["status"] == "ok" else 1
    if a.cmd == "snapshot":
        snap = snapshot.build(store, a.ticker, a.as_of)
        out = snapshot.analyse(snap) if a.analyse else snap.as_dict()
        if a.analyse and out.get("ran"):
            out["snapshot"] = snap.as_dict()
        print(json.dumps(out, indent=2, default=str))
        return 0
    for run in store.runs():
        print(json.dumps(run.__dict__, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
