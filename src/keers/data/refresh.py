"""Refresh the store from live sources, one company at a time, recording every failure.

A failed company is reported and left out; it never becomes an empty or zero record.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import fx, yahoo
from .beta import BENCHMARK
from .filers import Filers
from .fundamentals import ALL_CONCEPTS, IFRS_CONCEPTS, Fundamentals
from .http import Fetcher, FetchError
from .market import Market, price_sources
from .sec import SEC, ParseError
from .snapshot import price_currency
from .store import Store, is_sec_cik

DEFAULT_DB = "data/keers.sqlite"
PRICE_HISTORY_DAYS = 800


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise SystemExit(
            "SEC_USER_AGENT is not set. SEC requires a User-Agent naming you and a contact, "
            'e.g. "Keers & Co research contact@example.com".'
        )
    return ua


def read_universe(path: str | Path) -> list[str]:
    out = []
    for line in Path(path).read_text().splitlines():
        t = line.split("#", 1)[0].strip()
        if t:
            out.append(t.upper())
    return out


def refresh(
    store: Store,
    tickers: Sequence[str],
    fetcher: Fetcher,
    tiingo_token: str | None = None,
    today: date | None = None,
    rates: bool = True,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """``sources`` is the price-source order; by default ``KEERS_PRICE_SOURCES`` or yahoo then tiingo."""
    today = today or date.today()
    if sources is None:
        sources = price_sources(os.environ.get("KEERS_PRICE_SOURCES"))
    run = store.start_run("refresh")
    sec, market = SEC(fetcher, store), Market(fetcher, store, tiingo_token, sources)
    done: dict[str, Any] = {}
    failed: dict[str, str] = {}
    if rates:
        try:
            done["_rates"] = market.refresh_rates()
        except (FetchError, ParseError) as e:
            failed["_rates"] = str(e)
    # The market benchmark, for betas. Not a company, so no filings.
    try:
        done["_benchmark"] = market.refresh_prices(BENCHMARK, today - timedelta(days=PRICE_HISTORY_DAYS))
    except (FetchError, ParseError) as e:
        failed["_benchmark"] = str(e)
    filers = Filers(fetcher, store, sec)
    for t in tickers:
        try:
            info = refresh_accounts(store, sec, filers, t)
            info["prices"] = market.refresh_prices(t, today - timedelta(days=PRICE_HISTORY_DAYS))
            fxr = refresh_fx(fetcher, store, t, today)
            if fxr:
                info["fx"] = fxr
            done[t] = info
        except (FetchError, ParseError, KeyError, ValueError) as e:
            failed[t] = str(e)
    status = "ok" if not failed else ("partial" if done else "failed")
    # Which source priced each ticker, and any fallback: reported on every run, never passed silently.
    priced: dict[str, str] = {}
    fallbacks = {}
    for k, v in done.items():
        px = v if k == "_benchmark" else v.get("prices", {}) if isinstance(v, dict) else {}
        if not isinstance(px, dict) or "source" not in px:
            continue
        name = BENCHMARK if k == "_benchmark" else k
        priced[name] = px["source"]
        if px.get("fallbackFrom"):
            fallbacks[name] = {"used": px["source"], "failed": px["fallbackFrom"]}
    detail = {"ok": sorted(k for k in done if not k.startswith("_")), "failed": failed, "count": len(tickers),
              "priceSources": priced}  # fmt: skip
    if fallbacks:
        detail["priceFallbacks"] = fallbacks
    store.finish_run(run, status, detail)
    return {
        "status": status,
        "done": done,
        "failed": failed,
        "priceSources": priced,
        "priceFallbacks": fallbacks,
    }


def refresh_accounts(
    store: Store, sec: SEC, filers: Filers, ticker: str, name: str | None = None
) -> dict[str, Any]:
    """Filings for one listing: by its SEC ticker, else through its link to a filer (linking by ``name`` if given).

    Raises KeyError when the listing is neither an SEC ticker nor linked; the caller reports that, never a zero.
    """
    if store.link(ticker) is None and not yahoo.is_foreign(ticker):
        return sec.refresh_company(ticker, ALL_CONCEPTS)
    name = name or store.listing_name(ticker)
    if store.link(ticker) is None and name is None:
        raise KeyError(
            f"{ticker}: no filer linked; run: python -m keers.data link {ticker} --lei <LEI> (or --cik)"
        )
    return filers.refresh(ticker, name, ALL_CONCEPTS if _linked_to_sec(store, ticker) else IFRS_CONCEPTS)


def _linked_to_sec(store: Store, ticker: str) -> bool:
    link = store.link(ticker)
    return link is not None and is_sec_cik(int(link["cik"]))


def refresh_fx(fetcher: Fetcher, store: Store, ticker: str, today: date) -> dict[str, Any] | None:
    """Exchange rates a listing needs: its price currency and its accounts' currency, to and from US dollars."""
    co = store.company(ticker)
    px = store.last_price(ticker)
    need = set()
    if co is not None:
        need.add(Fundamentals(store, co["cik"], today).currency)
    if px is not None:
        need.add(price_currency(store, ticker, px.source) or "")
    need = {c for c in need if c and fx.major(c)[0] != "USD"}
    if not need:
        return None
    unsupported = sorted(c for c in need if not fx.supported(c))
    out = fx.refresh(fetcher, store, need, today - timedelta(days=PRICE_HISTORY_DAYS))
    if unsupported:
        out["unsupported"] = unsupported
    return out


def price_report(res: dict[str, Any]) -> dict[str, Any]:
    """The price-source part of a refresh result, for logs: a count per source, each ticker's source, fallbacks."""
    counts: dict[str, int] = {}
    for src in res["priceSources"].values():
        counts[src] = counts.get(src, 0) + 1
    return {
        "bySource": dict(sorted(counts.items())),
        "tickers": res["priceSources"],
        "fallbacks": res["priceFallbacks"],
    }
