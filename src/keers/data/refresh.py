"""Refresh the store from live sources, one company at a time, recording every failure.

A failed company is reported and left out; it never becomes an empty or zero record.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .beta import BENCHMARK
from .fundamentals import ALL_CONCEPTS
from .http import Fetcher, FetchError
from .market import Market, price_sources
from .sec import SEC, ParseError
from .store import Store

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
    for t in tickers:
        try:
            info = sec.refresh_company(t, ALL_CONCEPTS)
            info["prices"] = market.refresh_prices(t, today - timedelta(days=PRICE_HISTORY_DAYS))
            done[t] = info
        except (FetchError, ParseError, KeyError) as e:
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
