"""Fetch filings and prices for every company that was in the S&P 500 at any time in a backtest window.

* Today's members: CIK from the Wikipedia constituents table or the SEC ticker map.
* Companies that left: the SEC ticker map only lists today's tickers. If the old ticker is still in the map under
  the same company name, that CIK is used. If it is in the map under a different name, the ticker now belongs to
  another company: its prices would be someone else's, so the company is left unpriced and reported. Otherwise
  the CIK is looked up by company name in SEC's free CIK list (exact match after removing punctuation and legal
  suffixes); an ambiguous or missing match means no accounts, and that is reported too.
* Prices come from the usual chain (Yahoo, then Tiingo). Yahoo has no history for most delisted tickers; Tiingo's
  free plan allows about 50 requests an hour, so its requests are paced and the run stops at a time budget. The
  store is kept between runs (GitHub Actions cache), so a later run carries on where the last stopped.

Every company's outcome is in the report: priced or not, accounts or not, and why.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from keers.data.fundamentals import ALL_CONCEPTS
from keers.data.http import Fetcher, FetchError
from keers.data.market import Market
from keers.data.sec import SEC, ParseError
from keers.data.store import Store

from .history import Membership, _norm

CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
TIINGO_HOST = "api.tiingo.com"
TIINGO_INTERVAL = 75.0  # seconds; about 48 requests an hour, under the free plan's 50
HISTORY_DAYS = 420  # prices needed before the first signal date


def parse_cik_lookup(text: str) -> dict[str, set[int]]:
    """SEC's ``cik-lookup-data.txt`` (``NAME:CIK:`` per line) -> {normalised name: {CIKs}}."""
    out: dict[str, set[int]] = {}
    for line in text.splitlines():
        parts = line.rstrip().rstrip(":").rsplit(":", 1)
        if len(parts) != 2 or not parts[1].strip().isdigit():
            continue
        out.setdefault(_norm(parts[0]), set()).add(int(parts[1]))
    return out


def resolve(m: Membership, sec_map: dict[str, tuple[int, str]], lookup: dict[str, set[int]],
            tickers: set[str]) -> dict[str, dict[str, Any]]:  # fmt: skip
    """For each ticker: {"cik": int | None, "basis": ..., "prices": bool, "note": ...}."""
    names = m.names()
    reused = m.reused()
    out: dict[str, dict[str, Any]] = {}
    for t in sorted(tickers):
        name = names.get(t, "")
        if t in reused:
            out[t] = {"cik": None, "basis": "none", "prices": False,
                      "note": "ticker used by more than one company in the index history; left out"}  # fmt: skip
            continue
        if t in m.current:
            cik = m.ciks.get(t) or (sec_map[t][0] if t in sec_map else None)
            out[t] = {
                "cik": cik,
                "basis": "constituents" if t in m.ciks else "sec map",
                "prices": True,
                "note": "",
            }
            continue
        hit = sec_map.get(t)
        if hit is not None:
            if _norm(hit[1]) == _norm(name):
                out[t] = {
                    "cik": hit[0],
                    "basis": "sec map (same name)",
                    "prices": True,
                    "note": "still listed",
                }
            else:
                out[t] = {"cik": None, "basis": "none", "prices": False,
                          "note": f"ticker now belongs to {hit[1]!r}, not {name!r}; its prices would be wrong"}  # fmt: skip
            continue
        ciks = lookup.get(_norm(name), set()) if name else set()
        if len(ciks) == 1:
            out[t] = {
                "cik": next(iter(ciks)),
                "basis": "name match",
                "prices": True,
                "note": "no longer listed",
            }
        else:
            why = "no name match" if not ciks else f"{len(ciks)} CIKs share the name"
            out[t] = {"cik": None, "basis": "none", "prices": True, "note": f"no accounts: {why}"}
    return out


def _has_prices(store: Store, ticker: str, start: date) -> bool:
    r = store.db.execute(
        "SELECT MIN(day), COUNT(*) FROM prices WHERE ticker=? AND adj_close IS NOT NULL", (ticker.upper(),)
    ).fetchone()
    return bool(r and r[1] and r[0] <= (start + timedelta(days=30)).isoformat())


def fetch(store: Store, fetcher: Fetcher, market: Market, m: Membership, start: date, end: date,
          max_minutes: float = 300.0, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:  # fmt: skip
    fetcher.min_interval.setdefault(TIINGO_HOST, TIINGO_INTERVAL)
    sec = SEC(fetcher, store)
    tickers = m.ever(start, end)
    lookup = parse_cik_lookup(fetcher.get(CIK_LOOKUP_URL).body.decode("latin-1"))
    plan = resolve(m, sec.tickers(), lookup, tickers)
    stored = {r[0]: r[1] for r in store.db.execute("SELECT cik, ticker FROM companies")}
    t0 = clock()
    report: dict[str, Any] = {}
    stopped = False
    for t, p in plan.items():
        row = dict(p)
        if (clock() - t0) / 60 > max_minutes:
            stopped = True
            row["fetched"] = "not reached (time budget)"
            report[t] = row
            continue
        if p["cik"] is not None:
            owner = stored.get(p["cik"])
            if owner is not None and owner != t:
                row["accounts"] = f"CIK already stored under {owner}; not overwritten"
            elif owner == t:
                row["accounts"] = "already stored"
            else:
                try:
                    sec.refresh_cik(p["cik"], ALL_CONCEPTS, t)
                    stored[p["cik"]] = t
                    row["accounts"] = "fetched"
                except (FetchError, ParseError, KeyError) as e:
                    row["accounts"] = f"failed: {e}"
        if p["prices"]:
            if _has_prices(store, t, start - timedelta(days=HISTORY_DAYS)):
                row["priced"] = "already stored"
            else:
                try:
                    r = market.refresh_prices(t, start - timedelta(days=HISTORY_DAYS))
                    row["priced"] = f"{r['source']} {r['rows']} rows to {r['last']}"
                except (FetchError, ParseError) as e:
                    row["priced"] = f"failed: {str(e)[:200]}"
        report[t] = row
    counts = {
        "tickers": len(plan),
        "current": sum(1 for t in plan if t in m.current),
        "withAccounts": sum(1 for r in report.values() if str(r.get("accounts", "")).startswith(("fetched", "already"))),
        "priced": sum(1 for r in report.values() if str(r.get("priced", "")).split(" ")[0] in ("yahoo", "tiingo", "already")),
        "leftOut": sum(1 for r in report.values() if not r["prices"]),
        "stoppedAtBudget": stopped,
    }  # fmt: skip
    return {"counts": counts, "companies": report}
