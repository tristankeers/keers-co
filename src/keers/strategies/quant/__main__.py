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

from . import backtest, history, model, wide
from .pit import PointInTime


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m keers.strategies.quant")
    ap.add_argument("--db", default=os.environ.get("KEERS_DB", DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("fetch-sp500", help="S&P 500 membership history (Wikipedia) plus filings and prices for "
                       "every company in the index during the window, including ones that later left")  # fmt: skip
    w.add_argument("--start", type=date.fromisoformat, required=True)
    w.add_argument("--membership", default="data/sp500-membership.json", help="where to save the membership")
    w.add_argument("--report", default="quant-out/sp500-fetch.json")
    w.add_argument("--max-minutes", type=float, default=300.0)
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
            p.add_argument("--membership", help="membership file from fetch-sp500: rank index members only")
            p.add_argument("--benchmarks", nargs="*", default=[], help="extra benchmark tickers, e.g. RSP")
    a = ap.parse_args(argv)
    store = Store(a.db, keep_bodies=False)  # hashes and URLs are kept; bodies would run to gigabytes
    if a.cmd == "fetch-sp500":
        return fetch_sp500(store, a.start, a.membership, a.report, a.max_minutes)
    membership = None
    if a.cmd == "backtest" and a.membership:
        membership = history.Membership.from_dict(json.loads(Path(a.membership).read_text()))
    tickers = [t.upper() for t in a.tickers] + (read_universe(a.universe) if a.universe else [])
    if membership is not None and not tickers:
        tickers = sorted(membership.ever(a.start, a.end) - membership.reused())
    if not tickers:
        ap.error("give tickers, --universe or --membership")

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
        store, tickers, a.start, a.end, replace(model.Config.preset(a.preset), n_long=a.n_long),
        members=membership.members if membership else None, extra_benchmarks=[b.upper() for b in a.benchmarks],
    )  # fmt: skip
    if membership is not None:
        res["warnings"] += membership.warnings
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=2))
    summary = {k: v for k, v in res.items() if k not in ("curve", "months")}
    print(json.dumps(summary, indent=2))
    return 0 if res.get("ran") else 1


def fetch_sp500(store: Store, start: date, membership_path: str, report_path: str, max_minutes: float) -> int:
    fetcher = Fetcher(user_agent())
    token = (os.environ.get("TIINGO_TOKEN") or "").strip() or None
    market = Market(fetcher, store, token, price_sources(os.environ.get("KEERS_PRICE_SOURCES")))
    resp = fetcher.get(history.WIKI_URL)
    store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
    m = history.parse(resp.body.decode("utf-8"), date.today(), resp.sha256)
    Path(membership_path).parent.mkdir(parents=True, exist_ok=True)
    Path(membership_path).write_text(json.dumps(m.as_dict(), indent=1))
    failed: dict[str, str] = {}
    for b in (backtest.BENCHMARK, "RSP"):
        try:
            market.refresh_prices(b, start - timedelta(days=wide.HISTORY_DAYS))
        except (FetchError, ParseError) as e:
            failed[b] = str(e)
    try:
        market.refresh_rates()
    except (FetchError, ParseError) as e:
        failed["_rates"] = str(e)
    rep = wide.fetch(store, fetcher, market, m, start, date.today(), max_minutes)
    rep["benchmarksFailed"] = failed
    rep["membership"] = {"changes": len(m.changes), "current": len(m.current), "reused": sorted(m.reused()),
                         "warnings": m.warnings}  # fmt: skip
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(rep, indent=1))
    print(
        json.dumps(
            {"counts": rep["counts"], "membership": rep["membership"], "benchmarksFailed": failed}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
