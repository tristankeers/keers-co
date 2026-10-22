"""Yahoo Finance: daily prices, intraday bars, latest quotes and headlines.

UNOFFICIAL SOURCE, PERSONAL USE ONLY. Yahoo has no public API. These are the same web endpoints the
``yfinance`` library calls; they can change or start refusing requests without notice, and Yahoo's terms do not
cover automated or commercial use. Keers & Co uses them for personal research and paper trading only. They must
be replaced by a licensed feed before any commercial or regulated use (an FCA application, outside money).
See docs/data-sources.md.

The calls go through the shared ``Fetcher`` rather than the ``yfinance`` package, so every response is stored
by SHA-256 like the other sources and the data layer keeps no third-party runtime dependencies.

Rules kept here:
- Every price carries ``source="yahoo"`` and the hash of the raw response it came from.
- A bar that is still forming (today's daily bar during trading, the current minute bar) is never stored as if
  it were final. It is dropped and the result says so.
- A quote comes with warnings when it is old while the market is open, or when Yahoo marks it as delayed.
- Headlines are untrusted third-party text: they are stored and shown, never followed as instructions.
- SEC EDGAR stays the source of truth for financial statements. Nothing here touches fundamentals.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from .models import IntradayBar, NewsItem, PricePoint, Quote
from .sec import ParseError

HOST = "query1.finance.yahoo.com"
CHART_URL = f"https://{HOST}/v8/finance/chart/{{symbol}}?{{query}}"
SEARCH_URL = f"https://{HOST}/v1/finance/search?{{query}}"
# Yahoo refuses requests without a browser-style User-Agent. This one is honest about who is calling.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; keers-research/0.1)", "Accept": "application/json"}

# Intraday intervals Yahoo serves, and the longest range it gives for each.
INTERVALS = {"1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d", "30m": "60d", "60m": "730d", "90m": "60d"}
QUOTE_MAX_AGE = timedelta(minutes=20)
TITLE_MAX = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# Yahoo's exchange suffixes for the non-US markets in the global universe (keers.universe.exchanges).
EXCHANGE_SUFFIXES = {".L", ".PA", ".AS", ".BR", ".LS", ".IR", ".MI", ".MC", ".SW", ".VI", ".ST", ".OL", ".CO",
                     ".HE", ".WA", ".AT", ".IS", ".DE", ".TA", ".SR", ".JO", ".T", ".HK", ".SS", ".SZ", ".TW",
                     ".KS", ".KQ", ".NS", ".SI", ".KL", ".BK", ".JK", ".PS", ".AX", ".NZ", ".TO", ".V", ".SA",
                     ".MX", ".SN"}  # fmt: skip


def symbol(ticker: str) -> str:
    """Yahoo writes US share classes with a dash (BRK.B -> BRK-B). A symbol that already ends in a non-US
    exchange suffix (VOD.L, 7203.T, BT-A.L) is a Yahoo symbol and is left as it is."""
    t = ticker.upper()
    dot = t.rfind(".")
    if dot > 0 and t[dot:] in EXCHANGE_SUFFIXES:
        return t
    return t.replace(".", "-")


def daily_url(ticker: str, start: date, end: date) -> str:
    t0 = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
    t1 = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()) + 86400
    q = urlencode({"period1": t0, "period2": t1, "interval": "1d", "events": "div,split",
                   "includeAdjustedClose": "true"})  # fmt: skip
    return CHART_URL.format(symbol=quote(symbol(ticker)), query=q)


def intraday_url(ticker: str, interval: str, range_: str | None = None) -> str:
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(INTERVALS)}")
    q = urlencode({"interval": interval, "range": range_ or INTERVALS[interval], "includePrePost": "false"})
    return CHART_URL.format(symbol=quote(symbol(ticker)), query=q)


def news_url(ticker: str, count: int = 10) -> str:
    q = urlencode({"q": symbol(ticker), "quotesCount": 0, "newsCount": count, "enableFuzzyQuery": "false"})
    return SEARCH_URL.format(query=q)


# parsing ------------------------------------------------------------------------------------------------------
def _result(doc: Any, ticker: str) -> dict[str, Any]:
    chart = doc.get("chart") if isinstance(doc, dict) else None
    if not isinstance(chart, dict):
        raise ParseError(f"yahoo {ticker}: not a chart response")
    if chart.get("error"):
        err = chart["error"]
        raise ParseError(f"yahoo {ticker}: {err.get('code')}: {err.get('description')}")
    res = chart.get("result")
    if not isinstance(res, list) or not res or not isinstance(res[0], dict):
        raise ParseError(f"yahoo {ticker}: empty chart result")
    out: dict[str, Any] = res[0]
    return out


def _meta(r: dict[str, Any], ticker: str) -> dict[str, Any]:
    meta = r.get("meta")
    if not isinstance(meta, dict):
        raise ParseError(f"yahoo {ticker}: chart has no meta block")
    got = str(meta.get("symbol", "")).upper()
    if got and got != symbol(ticker):
        raise ParseError(f"yahoo {ticker}: response is for {got}")
    return meta


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _series(r: dict[str, Any], ticker: str) -> tuple[list[int], dict[str, list[Any]]]:
    ts = r.get("timestamp") or []
    try:
        q = r["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise ParseError(f"yahoo {ticker}: chart has no price arrays") from e
    cols = {k: list(q.get(k) or []) for k in ("open", "high", "low", "close", "volume")}
    if any(len(v) != len(ts) for v in cols.values() if v):
        raise ParseError(f"yahoo {ticker}: price arrays and timestamps differ in length")
    return list(ts), cols


def _regular_session(meta: dict[str, Any]) -> tuple[int, int] | None:
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    if "start" in reg and "end" in reg:
        return int(reg["start"]), int(reg["end"])
    return None


def parse_daily(doc: Any, ticker: str, raw_sha256: str) -> tuple[list[PricePoint], list[str]]:
    """Daily bars, oldest first, plus notes on anything left out. Days with no close (halts, holidays) are skipped.

    The day is taken in the exchange's own time zone, so a US close is filed under its US trading date.
    """
    r = _result(doc, ticker)
    meta = _meta(r, ticker)
    ts, cols = _series(r, ticker)
    adj: list[Any] = []
    try:
        adj = list(r["indicators"]["adjclose"][0]["adjclose"] or [])
    except (KeyError, IndexError, TypeError):
        adj = []
    offset = timedelta(seconds=int(meta.get("gmtoffset") or 0))
    session = _regular_session(meta)
    market_time = meta.get("regularMarketTime")
    notes: list[str] = []
    out: list[PricePoint] = []
    for i, t in enumerate(ts):
        close = _f(cols["close"][i] if cols["close"] else None)
        if close is None:
            continue
        day = (datetime.fromtimestamp(int(t), UTC) + offset).date()

        def col(name: str, i: int = i) -> float | None:
            return _f(cols[name][i]) if cols[name] else None

        out.append(PricePoint(ticker.upper(), day, close, "yahoo", col("open"), col("high"), col("low"),
                              col("volume"), raw_sha256, _f(adj[i]) if i < len(adj) else None))  # fmt: skip
    # Today's bar while the market is still open is a running price, not a close.
    if out and session and market_time is not None:
        start, end = session
        session_day = (datetime.fromtimestamp(start, UTC) + offset).date()
        if out[-1].day == session_day and int(market_time) < end:
            notes.append(f"dropped {session_day}: the session was still open, so that bar is not a close")
            out.pop()
    elif out and session is None:
        notes.append(
            "Yahoo gave no trading-session times, so the last bar could not be checked for completeness"
        )
    return out, notes


def parse_intraday(
    doc: Any, ticker: str, interval: str, raw_sha256: str
) -> tuple[list[IntradayBar], list[str]]:
    """Intraday bars, oldest first. The bar still forming at fetch time is dropped."""
    r = _result(doc, ticker)
    meta = _meta(r, ticker)
    ts, cols = _series(r, ticker)
    step = _interval_seconds(interval)
    market_time = meta.get("regularMarketTime")
    out: list[IntradayBar] = []
    notes: list[str] = []
    for i, t in enumerate(ts):
        close = _f(cols["close"][i] if cols["close"] else None)
        if close is None:
            continue

        def col(name: str, i: int = i) -> float | None:
            return _f(cols[name][i]) if cols[name] else None

        out.append(IntradayBar(ticker.upper(), datetime.fromtimestamp(int(t), UTC), interval, close, "yahoo",
                               col("open"), col("high"), col("low"), col("volume"), raw_sha256))  # fmt: skip
    if out and market_time is not None and out[-1].start.timestamp() + step > int(market_time):
        notes.append(f"dropped the {interval} bar starting {out[-1].start.isoformat()}: still forming")
        out.pop()
    return out, notes


def _interval_seconds(interval: str) -> int:
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(INTERVALS)}")
    return int(interval[:-1]) * 60


def parse_quote(doc: Any, ticker: str, raw_sha256: str, now: datetime) -> Quote:
    """The latest regular-session price from a chart response, with warnings when it may be stale or delayed."""
    r = _result(doc, ticker)
    meta = _meta(r, ticker)
    price, at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is None or at is None:
        raise ParseError(f"yahoo {ticker}: no regular-market price in the response")
    when = datetime.fromtimestamp(int(at), UTC)
    warnings: list[str] = []
    delay = meta.get("exchangeDataDelayedBy")
    if delay:
        warnings.append(f"Yahoo marks this exchange's data as delayed by {int(delay)} minutes")
    session = _regular_session(meta)
    now_ts = now.timestamp()
    if session is None:
        warnings.append("no trading-session times from Yahoo; freshness unchecked")
    elif session[0] <= now_ts < session[1]:
        if now - when > QUOTE_MAX_AGE:
            mins = int((now - when).total_seconds() // 60)
            warnings.append(f"stale: market is open but the last trade is {mins} minutes old")
    else:
        warnings.append(f"market closed: last regular-session price at {when.isoformat()}")
    return Quote(ticker.upper(), float(price), when, "yahoo", str(meta.get("currency") or ""),
                 str(meta.get("exchangeName") or ""), raw_sha256, tuple(warnings))  # fmt: skip


def clean_text(s: Any, limit: int = TITLE_MAX) -> str:
    """Collapse whitespace, drop control characters and cap the length of third-party text."""
    return " ".join(_CONTROL.sub(" ", str(s or "")).split())[:limit]


def parse_news(doc: Any, ticker: str, raw_sha256: str) -> list[NewsItem]:
    """Headlines from Yahoo's search endpoint. Untrusted text: only the headline, publisher and link are kept."""
    if not isinstance(doc, dict) or not isinstance(doc.get("news", []), list):
        raise ParseError(f"yahoo news {ticker}: not a search response")
    out: list[NewsItem] = []
    for n in doc.get("news", []):
        if not isinstance(n, dict) or not n.get("uuid") or not n.get("title"):
            continue
        link = str(n.get("link") or "")
        if not link.startswith(("https://", "http://")):
            link = ""
        published = n.get("providerPublishTime")
        out.append(
            NewsItem(
                uuid=clean_text(n["uuid"], 100),
                ticker=ticker.upper(),
                title=clean_text(n["title"]),
                publisher=clean_text(n.get("publisher"), 100),
                link=link[:500],
                published=datetime.fromtimestamp(int(published), UTC) if published else None,
                related=tuple(clean_text(t, 12).upper() for t in (n.get("relatedTickers") or [])[:20]),
                source="yahoo",
                raw_sha256=raw_sha256,
            )
        )
    return out
