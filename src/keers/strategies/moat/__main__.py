"""Command line: ``python -m keers.strategies.moat screen|refetch-accounts|backtest``.

``screen`` grades companies already in the store (``scripts/moat_screen.py`` refreshes them live first);
``refetch-accounts`` downloads SEC company facts again for stored companies whose facts predate the lines the moat
screen reads (cash from operations, income tax, gross profit); ``backtest`` runs the quarterly point-in-time
backtest, on the S&P 500 as it stood each quarter when given the membership file from
``python -m keers.strategies.quant fetch-sp500``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from keers.data import fundamentals as F
from keers.data.http import Fetcher, FetchError
from keers.data.refresh import DEFAULT_DB, read_universe, user_agent
from keers.data.sec import SEC, ParseError
from keers.data.store import Store, is_sec_cik
from keers.strategies.quant import history

from . import backtest, screener

# A company stored before these lines were collected has none of them; its facts are fetched again.
REQUIRED = F.OPERATING_CASH_FLOW


def needs_refetch(store: Store, cik: int) -> bool:
    for c in REQUIRED:
        tax, name = c.split(":", 1)
        if store.db.execute("SELECT 1 FROM facts WHERE cik=? AND taxonomy=? AND concept=? LIMIT 1",
                            (cik, tax, name)).fetchone():  # fmt: skip
            return False
    return True


def refetch_accounts(store: Store, max_minutes: float) -> dict[str, object]:
    sec = SEC(Fetcher(user_agent()), store)
    rows = store.db.execute("SELECT cik, ticker FROM companies ORDER BY ticker").fetchall()
    t0, done, failed, skipped = time.monotonic(), [], {}, 0
    for cik, ticker in rows:
        if not is_sec_cik(cik) or not needs_refetch(store, cik):
            skipped += 1
            continue
        if (time.monotonic() - t0) / 60 > max_minutes:
            failed[ticker] = "not reached (time budget)"
            continue
        try:
            sec.refresh_cik(cik, F.ALL_CONCEPTS, ticker)
            done.append(ticker)
        except (FetchError, ParseError, KeyError) as e:
            failed[ticker] = str(e)[:200]
    return {"refetched": len(done), "upToDate": skipped, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m keers.strategies.moat")
    ap.add_argument("--db", default=os.environ.get("KEERS_DB", DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("screen", help="grade companies in the store")
    s.add_argument("tickers", nargs="*")
    s.add_argument("--universe")
    s.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    s.add_argument("--membership", help="membership file from quant fetch-sp500: screen the index on --as-of")
    s.add_argument("--out")
    r = sub.add_parser(
        "refetch-accounts", help="fetch SEC facts again where they predate the moat screen's lines"
    )
    r.add_argument("--max-minutes", type=float, default=60.0)
    b = sub.add_parser("backtest")
    b.add_argument("tickers", nargs="*")
    b.add_argument("--universe")
    b.add_argument("--start", type=date.fromisoformat, required=True)
    b.add_argument("--end", type=date.fromisoformat, default=date.today())
    b.add_argument("--membership", help="membership file from quant fetch-sp500: screen index members only")
    b.add_argument("--benchmarks", nargs="*", default=[], help="extra benchmark tickers, e.g. RSP")
    b.add_argument("--out")
    a = ap.parse_args(argv)
    store = Store(a.db, keep_bodies=False)

    if a.cmd == "refetch-accounts":
        print(json.dumps(refetch_accounts(store, a.max_minutes), indent=2))
        return 0

    membership = None
    if a.membership:
        membership = history.Membership.from_dict(json.loads(Path(a.membership).read_text()))
    tickers = [t.upper() for t in a.tickers] + (read_universe(a.universe) if a.universe else [])
    if membership is not None and not tickers:
        if a.cmd == "screen":
            tickers = sorted(membership.members(a.as_of) - membership.reused())
        else:
            tickers = sorted(membership.ever(a.start, a.end) - membership.reused())
    if not tickers:
        ap.error("give tickers, --universe or --membership")

    if a.cmd == "screen":
        res = screener.run(store, tickers, a.as_of)
        long_keys: tuple[str, ...] = ("years",)
    else:
        res = backtest.run(store, tickers, a.start, a.end, members=membership.members if membership else None,
                           extra_benchmarks=[x.upper() for x in a.benchmarks],
                           progress=lambda m: print(m, file=sys.stderr, flush=True))  # fmt: skip
        if membership is not None:
            res["warnings"] += membership.warnings
        long_keys = ("curve", "equalWeightCurve", "quarters")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=2, default=str))
    if a.cmd == "screen":
        for c in res["companies"]:
            print(
                f"{c['ticker']:8} {c['status']:22} "
                + " ".join(f"{k['name']}={k['level']}" for k in c["checks"])
            )
    else:
        print(json.dumps({k: v for k, v in res.items() if k not in long_keys}, indent=2, default=str))
    return 0 if a.cmd == "screen" or res.get("ran") else 1


if __name__ == "__main__":
    sys.exit(main())
