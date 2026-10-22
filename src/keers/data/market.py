"""Market data: daily share prices (Stooq or Tiingo) and interest rates (FRED).

Licence caveat: Stooq and FRED are free for research use. Check each provider's terms before any redistribution.
FRED's Treasury series are published in percent; they are stored as published and converted only where used.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Any

from .http import Fetcher
from .models import PricePoint, RatePoint
from .sec import ParseError
from .store import Store

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate={start}"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Rates the valuation needs: the 10-year Treasury (risk-free rate) and Moody's Baa yield (a cost-of-debt check).
FRED_SERIES = ("DGS10", "DGS3MO", "BAA10Y", "DBAA")


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
                           r.get("open"), r.get("high"), r.get("low"), r.get("volume"), raw_sha256)
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
    def __init__(self, fetcher: Fetcher, store: Store, tiingo_token: str | None = None) -> None:
        self.fetcher = fetcher
        self.store = store
        self.tiingo_token = tiingo_token

    def refresh_prices(self, ticker: str, start: date) -> dict[str, Any]:
        if self.tiingo_token:
            url = TIINGO_URL.format(ticker=ticker.lower(), start=start.isoformat())
            resp = self.fetcher.get(url, headers={"Authorization": f"Token {self.tiingo_token}"})
            points = parse_tiingo_json(json.loads(resp.body), ticker, resp.sha256)
        else:
            url = STOOQ_URL.format(symbol=stooq_symbol(ticker))
            resp = self.fetcher.get(url)
            points = [p for p in parse_stooq_csv(resp.body.decode(), ticker, resp.sha256) if p.day >= start]
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        if not points:
            raise ParseError(f"{ticker}: no prices since {start}")
        self.store.put_prices(points)
        return {
            "ticker": ticker.upper(),
            "source": points[0].source,
            "rows": len(points),
            "last": str(points[-1].day),
        }

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
