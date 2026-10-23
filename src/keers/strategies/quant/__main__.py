"""Command line: ``python -m keers.strategies.quant fetch|signals|backtest``.

``fetch`` downloads filings and long daily price histories (Yahoo first, SEC for the company record) into the
store; ``signals`` prints today's ranking and target book; ``backtest`` runs the point-in-time backtest. Placing
paper trades is done in the workbench (Portfolio tab or POST /api/portfolios/{id}/strategies/c/rebalance), so
every ticket passes the paper desk and the risk limits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from keers.data.fundamentals import ALL_CONCEPTS
from keers.data.http import Fetcher, FetchError
from keers.data.market import Market, price_sources
from keers.data.refresh import DEFAULT_DB, read_universe, user_agent
from keers.data.sec import SEC, ParseError
from keers.data.store import Store

from . import backtest, model
from .pit import PointInTime


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m keers.strategies.quant")
    ap.add_argument("--db", default=os.environ.get("KEERS_DB", DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "signals", "backtest"):
        p = sub.add_parser(name)
        p.add_argument("tickers", nargs="*")
        p.add_argument("--universe", help="file with one ticker per line")
        if name == "fetch":
            p.add_argument("--years", type=int, default=10)
        if name == "signals":
            p.add_argument("--as-of", type=date.fromisoformat, default=date.today())
        if name == "backtest":
            p.add_argument("--start", type=date.fromisoformat, required=True)
            p.add_argument("--end", type=date.fromisoformat, default=date.today())
            p.add_argument("--n-long", type=int, default=model.Config.n_long)
            p.add_argument(
                "--preset", choices=["v1", "v2"], default="v2", help="model version (v1 kept to compare)"
            )
            p.add_argument("--out", help="write the full result as JSON here")
    a = ap.parse_args(argv)
    tickers = [t.upper() for t in a.tickers] + (read_universe(a.universe) if a.universe else [])
    if not tickers:
        ap.error("give tickers or --universe")
    store = Store(a.db)

    if a.cmd == "fetch":
        fetcher = Fetcher(user_agent())
        token = (os.environ.get("TIINGO_TOKEN") or "").strip() or None
        market = Market(fetcher, store, token, price_sources(os.environ.get("KEERS_PRICE_SOURCES")))
        sec = SEC(fetcher, store)
        start = date.today() - timedelta(days=round(365.25 * a.years))
        failed: dict[str, str] = {}
        for t in [backtest.BENCHMARK, *tickers]:
            try:
                if t != backtest.BENCHMARK:
                    sec.refresh_company(t, ALL_CONCEPTS)
                market.refresh_prices(t, start)
            except (FetchError, ParseError, KeyError) as e:
                failed[t] = str(e)
        try:
            market.refresh_rates()
        except (FetchError, ParseError) as e:
            failed["_rates"] = str(e)
        print(json.dumps({"fetched": len(tickers) + 1 - len(failed), "failed": failed}, indent=2))
        return 0 if not failed else 1

    if a.cmd == "signals":
        rows = model.rank(PointInTime(store, a.as_of), tickers, model.Config())
        print(json.dumps({"asOf": a.as_of.isoformat(), "rows": [r.as_dict() for r in rows],
                          "targets": model.targets(rows)}, indent=2))  # fmt: skip
        return 0

    res = backtest.run(
        store, tickers, a.start, a.end, replace(model.Config.preset(a.preset), n_long=a.n_long)
    )
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=2))
    summary = {k: v for k, v in res.items() if k not in ("curve", "months")}
    print(json.dumps(summary, indent=2))
    return 0 if res.get("ran") else 1


if __name__ == "__main__":
    sys.exit(main())
