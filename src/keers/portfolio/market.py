"""Market data for paper trading, read from the market-data store: the price to fill and mark at, the average
daily volume, and a suggested sector. Read only; fetching is the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from keers.data.store import SOURCE_RANK, Store
from keers.risk.checks import Mark


@dataclass(frozen=True)
class MarketMark:
    """The latest price for a symbol and where it came from."""

    symbol: str
    price: float | None
    day: date | None  # trading date of the price
    at: str | None  # trade time (quote) or date (close)
    source: str  # e.g. "yahoo quote", "yahoo close", "none"
    raw_sha256: str
    currency: str | None
    adv_shares: float | None
    adv_sessions: int
    warnings: tuple[str, ...] = ()
    sector: str | None = None  # suggestion only
    sector_basis: str = ""

    def risk_mark(self) -> Mark:
        return Mark(self.price, self.day, self.currency, self.adv_shares, self.warnings)

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "price": self.price, "day": self.day.isoformat() if self.day else None,
                "at": self.at, "source": self.source, "rawSha256": self.raw_sha256, "currency": self.currency,
                "advShares": self.adv_shares, "advSessions": self.adv_sessions, "warnings": list(self.warnings),
                "sector": self.sector, "sectorBasis": self.sector_basis}  # fmt: skip


def daily_rows(
    store: Store, symbol: str, start: date, end: date
) -> list[tuple[date, float, float | None, str, str]]:
    """(day, close, volume, source, raw hash) from the one best-ranked source with the latest day, oldest first."""
    args = (symbol.upper(), start.isoformat(), end.isoformat())
    rows = store.db.execute(
        "SELECT source, MAX(day), COUNT(*) FROM prices WHERE ticker=? AND day>=? AND day<=? GROUP BY source",
        args,
    ).fetchall()
    if not rows:
        return []
    latest = max(r[1] for r in rows)
    best = min((r for r in rows if r[1] == latest), key=lambda r: (SOURCE_RANK.get(r[0], 99), -r[2]))[0]
    q = "SELECT day, close, volume, source, raw_sha256 FROM prices WHERE ticker=? AND day>=? AND day<=? AND source=? ORDER BY day"
    return [(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in store.db.execute(q, (*args, best))]


def average_volume(
    rows: list[tuple[date, float, float | None, str, str]], sessions: int, minimum: int
) -> tuple[float | None, int]:
    """Mean volume of the last ``sessions`` days that have one; None when fewer than ``minimum`` do."""
    vols = [r[2] for r in rows if r[2] is not None and r[2] > 0][-sessions:]
    if len(vols) < minimum:
        return None, len(vols)
    return sum(vols) / len(vols), len(vols)


def mark(store: Store, symbol: str, today: date, currency_hint: str | None = None, adv_sessions: int = 20,
         adv_minimum: int = 10) -> MarketMark:  # fmt: skip
    """The newer of the latest stored Yahoo quote and the latest stored daily close."""
    sym = symbol.upper()
    rows = daily_rows(store, sym, today - timedelta(days=60), today)
    adv, n = average_volume(rows, adv_sessions, adv_minimum)
    q = store.last_quote(sym)
    last = rows[-1] if rows else None
    if q is not None and (last is None or q.at.date() >= last[0]):
        return MarketMark(sym, q.price, q.at.date(), q.at.isoformat(), f"{q.source} quote", q.raw_sha256,
                          q.currency or currency_hint, adv, n, tuple(q.warnings))  # fmt: skip
    if last is not None:
        return MarketMark(sym, last[1], last[0], last[0].isoformat(), f"{last[3]} close", last[4], currency_hint,
                          adv, n)  # fmt: skip
    return MarketMark(sym, None, None, None, "none", "", currency_hint, adv, n, ("no price in the store",))


def closes(store: Store, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    return [(r[0], r[1]) for r in daily_rows(store, symbol, start, end)]


# Broad sectors from the SEC's Standard Industrial Classification code. Coarse, and labelled as derived; the
# person can always enter a sector instead.
_SIC2 = {
    13: "Energy", 29: "Energy", 28: "Healthcare and chemicals", 35: "Technology", 36: "Technology",
    38: "Technology", 37: "Industrials", 48: "Communications", 49: "Utilities", 73: "Technology",
    80: "Healthcare and chemicals", 60: "Financials", 61: "Financials", 62: "Financials", 63: "Financials",
    64: "Financials", 65: "Real estate", 67: "Financials", 78: "Communications",
}  # fmt: skip
_SIC_RANGES = (
    (1, 9, "Materials"), (10, 14, "Materials"), (15, 17, "Industrials"), (20, 27, "Consumer staples"),
    (30, 34, "Materials"), (39, 39, "Consumer discretionary"), (40, 47, "Industrials"), (50, 51, "Industrials"),
    (52, 59, "Consumer discretionary"), (70, 79, "Consumer discretionary"), (81, 89, "Industrials"),
)  # fmt: skip


def sector_from_sic(sic: str | None) -> str | None:
    if not sic or not str(sic).strip().isdigit():
        return None
    two = int(str(sic).strip().zfill(4)[:2])
    if two in _SIC2:
        return _SIC2[two]
    return next((name for lo, hi, name in _SIC_RANGES if lo <= two <= hi), None)
