"""Market data: share prices (Yahoo, Tiingo or Stooq), intraday bars, quotes, headlines and rates (FRED).

Daily prices come from an ordered list of sources (``DEFAULT_PRICE_SOURCES``, or ``KEERS_PRICE_SOURCES``). The
first source that answers wins. Every price row records its source, and when an earlier source failed the result
says which one and why, so a fallback is never silent.

Licence caveat: Yahoo is an unofficial source for personal use only (see ``yahoo.py`` and docs/data-sources.md).
Stooq and FRED are free for research use. Check each provider's terms before any redistribution.
FRED's Treasury series are published in percent; they are stored as published and converted only where used.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from . import yahoo
from .http import Fetcher, FetchError, Response
from .models import NewsItem, PricePoint, Quote, RatePoint
from .sec import ParseError
from .store import Store

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate={start}"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Rates the valuation needs: the 10-year Treasury (risk-free rate) and Moody's Baa yield (a cost-of-debt check).
FRED_SERIES = ("DGS10", "DGS3MO", "BAA10Y", "DBAA")

# Tried in order. Tiingo is skipped when there is no token. Stooq blocks scripted downloads, so it is opt-in.
PRICE_SOURCES = ("yahoo", "tiingo", "stooq")
DEFAULT_PRICE_SOURCES = ("yahoo", "tiingo")


def price_sources(spec: str | None) -> tuple[str, ...]:
    """Parse a comma-separated list such as "yahoo,tiingo". Empty means the default order."""
    if not spec or not spec.strip():
        return DEFAULT_PRICE_SOURCES
    out = tuple(s.strip().lower() for s in spec.split(",") if s.strip())
    bad = [s for s in out if s not in PRICE_SOURCES]
    if bad:
        raise ValueError(f"unknown price source(s) {bad}; choose from {', '.join(PRICE_SOURCES)}")
    return out


def stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace(".", "-") + ".us"


def _num(s: str) -> float | None:
    s = s.strip()
    if s in ("", ".", "N/D", "null"):
        return None
    return float(s)


def _page_text(html: str, limit: int = 240) -> str:
    """Visible text of an HTML page, so a refusal or captcha page shows up readably in logs."""
    import re

    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())[:limit]


def parse_stooq_csv(text: str, ticker: str, raw_sha256: str) -> list[PricePoint]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip().lower() for c in rows[0][:5]] != ["date", "open", "high", "low", "close"]:
        raise ParseError(f"stooq {ticker}: not a price file. Page says: {_page_text(text)!r}")
    out: list[PricePoint] = []
    for r in rows[1:]:
        if not r:
            continue
        close = _num(r[4])
        if close is None:
            continue
        vol = _num(r[5]) if len(r) > 5 else None
        out.append(
            PricePoint(ticker.upper(), date.fromisoformat(r[0]), close, "stooq",
                       _num(r[1]), _num(r[2]), _num(r[3]), vol, raw_sha256)
        )  # fmt: skip
    return out


def parse_tiingo_json(doc: Any, ticker: str, raw_sha256: str) -> list[PricePoint]:
    if not isinstance(doc, list):
        raise ParseError(f"tiingo {ticker}: expected a list, got {type(doc).__name__}")
    out: list[PricePoint] = []
    for r in doc:
        try:
            out.append(
                PricePoint(ticker.upper(), date.fromisoformat(str(r["date"])[:10]), float(r["close"]), "tiingo",
                           r.get("open"), r.get("high"), r.get("low"), r.get("volume"), raw_sha256,
                           float(r["adjClose"]) if r.get("adjClose") is not None else None)
            )  # fmt: skip
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(f"tiingo {ticker}: bad row {r!r}") from e
    return out


def parse_fred_csv(text: str, series: str, raw_sha256: str) -> list[RatePoint]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) < 2 or rows[0][1].strip() != series:
        raise ParseError(f"fred {series}: unexpected header {rows[0] if rows else None!r}")
    out: list[RatePoint] = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        v = _num(r[1])
        if v is not None:
            out.append(RatePoint(series, date.fromisoformat(r[0]), v, "fred", raw_sha256))
    return out


class Market:
    def __init__(
        self,
        fetcher: Fetcher,
        store: Store,
        tiingo_token: str | None = None,
        sources: Sequence[str] = DEFAULT_PRICE_SOURCES,
    ) -> None:
        self.fetcher = fetcher
        self.store = store
        self.tiingo_token = tiingo_token
        self.sources = tuple(s for s in sources if s != "tiingo" or tiingo_token)
        if not self.sources:
            raise ValueError("no usable price source (Tiingo needs TIINGO_TOKEN)")
        self.refused: dict[str, str] = {}  # sources that throttled or blocked us during this run

    def _daily(self, source: str, ticker: str, start: date) -> tuple[Response, list[PricePoint], list[str]]:
        if source == "yahoo":
            resp = self.fetcher.get(yahoo.daily_url(ticker, start, date.today()), headers=yahoo.HEADERS)
            points, notes = yahoo.parse_daily(json.loads(resp.body), ticker, resp.sha256)
            return resp, [p for p in points if p.day >= start], notes
        if source == "tiingo":
            url = TIINGO_URL.format(ticker=ticker.lower(), start=start.isoformat())
            resp = self.fetcher.get(url, headers={"Authorization": f"Token {self.tiingo_token}"})
            return resp, parse_tiingo_json(json.loads(resp.body), ticker, resp.sha256), []
        resp = self.fetcher.get(STOOQ_URL.format(symbol=stooq_symbol(ticker)))
        points = [p for p in parse_stooq_csv(resp.body.decode(), ticker, resp.sha256) if p.day >= start]
        return resp, points, []

    def refresh_prices(self, ticker: str, start: date) -> dict[str, Any]:
        """Daily prices from the first source that answers. Earlier failures are reported in ``fallbackFrom``."""
        failed: dict[str, str] = {}
        for source in self.sources:
            if source in self.refused:
                failed[source] = f"skipped: refused earlier in this run ({self.refused[source]})"
                continue
            try:
                resp, points, notes = self._daily(source, ticker, start)
            except (FetchError, ParseError, ValueError) as e:  # ValueError: a body that is not JSON
                failed[source] = str(e)
                if isinstance(e, FetchError) and any(f"HTTP {c}" in str(e) for c in (401, 403, 429)):
                    # Throttled or blocked: stop asking for the rest of the run instead of retrying every ticker.
                    self.refused[source] = str(e)[-120:]
                continue
            self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
            if not points:
                failed[source] = f"no prices since {start}"
                continue
            self.store.put_prices(points)
            out: dict[str, Any] = {
                "ticker": ticker.upper(),
                "source": source,
                "rows": len(points),
                "last": str(points[-1].day),
            }
            if notes:
                out["notes"] = notes
            if failed:
                out["fallbackFrom"] = failed
            return out
        raise FetchError(
            f"{ticker}: no price source answered: " + "; ".join(f"{k}: {v}" for k, v in failed.items())
        )

    # Yahoo only: intraday bars, the latest quote and headlines (personal use; see yahoo.py) ---------------------
    def refresh_bars(self, ticker: str, interval: str = "5m", range_: str | None = None) -> dict[str, Any]:
        resp = self.fetcher.get(yahoo.intraday_url(ticker, interval, range_), headers=yahoo.HEADERS)
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        bars, notes = yahoo.parse_intraday(_json(resp, ticker), ticker, interval, resp.sha256)
        if not bars:
            raise ParseError(f"yahoo {ticker}: no completed {interval} bars")
        self.store.put_bars(bars)
        return {"ticker": ticker.upper(), "source": "yahoo", "interval": interval, "rows": len(bars),
                "last": bars[-1].start.isoformat(), "notes": notes}  # fmt: skip

    def quote(self, ticker: str, now: datetime | None = None) -> Quote:
        resp = self.fetcher.get(yahoo.intraday_url(ticker, "1m", "1d"), headers=yahoo.HEADERS)
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        q = yahoo.parse_quote(_json(resp, ticker), ticker, resp.sha256, now or datetime.now(UTC))
        self.store.put_quote(q)
        return q

    def refresh_news(self, ticker: str, count: int = 10) -> list[NewsItem]:
        """Headlines for ``ticker``. UNTRUSTED third-party text: store and show it, never act on what it says."""
        resp = self.fetcher.get(yahoo.news_url(ticker, count), headers=yahoo.HEADERS)
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        items = yahoo.parse_news(_json(resp, ticker), ticker, resp.sha256)
        self.store.put_news(items)
        return items

    def refresh_rates(self, series: tuple[str, ...] = FRED_SERIES) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in series:
            resp = self.fetcher.get(FRED_URL.format(series=s))
            self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
            points = parse_fred_csv(resp.body.decode(), s, resp.sha256)
            if not points:
                raise ParseError(f"fred {s}: no observations")
            self.store.put_rates(points)
            out[s] = {"rows": len(points), "last": str(points[-1].day), "value": points[-1].value}
        return out


def _json(resp: Response, ticker: str) -> Any:
    try:
        return json.loads(resp.body)
    except ValueError as e:
        raise ParseError(f"{ticker}: {resp.url} did not return JSON") from e
