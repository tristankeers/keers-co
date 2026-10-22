"""Small read helpers the workbench needs that the data layer does not offer yet.

Kept here, rather than in ``keers.data``, so this package can merge independently of the data-layer work in
flight. Each could move into ``keers.data`` later unchanged.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import urlencode

from keers.data import yahoo
from keers.data.http import Fetcher
from keers.data.models import PricePoint
from keers.data.sec import ParseError
from keers.data.store import SOURCE_RANK, Store

# Yahoo's exchange suffixes. A symbol ending in one is a non-US listing and keeps its dot; any other dot is a US
# share class, which Yahoo writes with a dash. Matches keers.data.yahoo.symbol once the universe work lands.
EXCHANGE_SUFFIXES = frozenset(
    ".L .PA .AS .BR .LS .IR .MI .MC .SW .VI .ST .OL .CO .HE .WA .AT .IS .DE .TA .SR .JO .T .HK .SS .SZ .TW .KS "
    ".KQ .NS .SI .KL .BK .JK .PS .AX .NZ .TO .V .SA .MX .SN .F .BO .IL .TWO .CN .NE".split()
)


def is_foreign(sym: str) -> bool:
    t = sym.upper()
    dot = t.rfind(".")
    return dot > 0 and t[dot:] in EXCHANGE_SUFFIXES


def symbol(ticker: str) -> str:
    """Yahoo's form of a symbol: BRK.B -> BRK-B, while VOD.L and 7203.T stay as they are."""
    t = ticker.strip().upper()
    return t if is_foreign(t) else t.replace(".", "-")


def price_history(store: Store, ticker: str, start: date, end: date) -> list[PricePoint]:
    """Daily bars from ONE source, oldest first: the best-ranked source among those with the latest day.

    Sources are never stitched together, so a chart never mixes two providers' closes.
    """
    args = (ticker.upper(), start.isoformat(), end.isoformat())
    rows = store.db.execute(
        "SELECT source, MAX(day), COUNT(*) FROM prices WHERE ticker=? AND day>=? AND day<=? GROUP BY source",
        args,
    ).fetchall()
    if not rows:
        return []
    latest = max(r[1] for r in rows)
    best = min((r for r in rows if r[1] == latest), key=lambda r: (SOURCE_RANK.get(r[0], 99), -r[2]))[0]
    q = (
        "SELECT ticker,day,source,close,open,high,low,volume,raw_sha256,adj_close FROM prices "
        "WHERE ticker=? AND day>=? AND day<=? AND source=? ORDER BY day"
    )
    return [
        PricePoint(r[0], date.fromisoformat(r[1]), r[3], r[2], r[4], r[5], r[6], r[7], r[8], r[9])
        for r in store.db.execute(q, (*args, best))
    ]


def companies(store: Store) -> list[dict[str, Any]]:
    """Every company with filings in the store, by ticker."""
    q = "SELECT cik,ticker,name,sic,fiscal_year_end,updated_at FROM companies ORDER BY ticker"
    keys = ("cik", "ticker", "name", "sic", "fiscalYearEnd", "updatedAt")
    return [dict(zip(keys, r, strict=True)) for r in store.db.execute(q)]


SEARCH_URL = f"https://{yahoo.HOST}/v1/finance/search?{{query}}"
SEARCH_TYPES = {"EQUITY", "ETF"}


def yahoo_search(fetcher: Fetcher, query: str, count: int = 10) -> list[dict[str, str]]:
    """Listings matching ``query`` from Yahoo's search (world exchanges; unofficial, personal use only)."""
    q = urlencode({"q": query.strip()[:60], "quotesCount": count, "newsCount": 0, "enableFuzzyQuery": "false",
                   "listsCount": 0})  # fmt: skip
    resp = fetcher.get(SEARCH_URL.format(query=q), headers=yahoo.HEADERS)
    try:
        doc = json.loads(resp.body)
    except ValueError as e:
        raise ParseError("yahoo search: not JSON") from e
    return parse_yahoo_search(doc)


def parse_yahoo_search(doc: Any) -> list[dict[str, str]]:
    """Symbol, name, exchange and type of each equity or ETF. Third-party text, cleaned and capped."""
    if not isinstance(doc, dict) or not isinstance(doc.get("quotes", []), list):
        raise ParseError("yahoo search: not a search response")
    out: list[dict[str, str]] = []
    for q in doc.get("quotes", []):
        if (
            not isinstance(q, dict)
            or str(q.get("quoteType", "")).upper() not in SEARCH_TYPES
            or not q.get("symbol")
        ):
            continue
        out.append({
            "symbol": yahoo.clean_text(q["symbol"], 24).upper(),
            "name": yahoo.clean_text(q.get("longname") or q.get("shortname"), 120),
            "exchange": yahoo.clean_text(q.get("exchDisp") or q.get("exchange"), 40),
            "type": yahoo.clean_text(q.get("quoteType"), 12).upper(),
            "sector": yahoo.clean_text(q.get("sectorDisp") or q.get("sector"), 60),
            "industry": yahoo.clean_text(q.get("industryDisp") or q.get("industry"), 80),
        })  # fmt: skip
    return out
