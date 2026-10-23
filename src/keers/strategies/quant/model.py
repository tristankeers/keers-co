"""Rank companies on the signals and pick the long book (``quant-c-v1``).

1. Each signal is turned into a cross-sectional z-score among the companies where it is known, after clipping
   the raw scores at the 5th and 95th percentiles so one outlier cannot dominate.
2. The composite is the mean of the z-scores. A company with any unknown signal is not ranked.
3. Eligible companies must pass a liquidity screen (price and median traded value over 20 sessions).
4. The book is the top ``n_long`` by composite, equal weight, ``sleeve`` of NAV in total but at most
   ``max_weight`` a name, long only.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .pit import PointInTime
from .signals import SIGNALS, Signal


@dataclass(frozen=True)
class Config:
    n_long: int = 15
    sleeve: float = 0.30  # share of NAV for the whole book (strategy C's risk cap is 40%)
    max_weight: float = 0.05  # one name, share of NAV (the risk cap is 10%)
    min_price: float = 5.0
    min_traded_value: float = 5e6  # median close × volume over 20 sessions, USD
    signals: tuple[str, ...] = tuple(SIGNALS)
    clip: float = 0.05

    def __post_init__(self) -> None:
        if self.n_long < 1 or not 0 < self.sleeve <= 0.40:
            raise ValueError("n_long must be at least 1 and sleeve between 0 and 0.40 of NAV")
        unknown = set(self.signals) - set(SIGNALS)
        if unknown or not self.signals:
            raise ValueError(f"unknown signals: {sorted(unknown)}")

    def as_dict(self) -> dict[str, Any]:
        return {"nLong": self.n_long, "sleeve": self.sleeve, "maxWeight": self.max_weight, "minPrice": self.min_price,
                "minTradedValue": self.min_traded_value, "signals": list(self.signals), "clip": self.clip}  # fmt: skip


@dataclass
class Row:
    ticker: str
    signals: dict[str, Signal]
    liquidity: str = ""  # empty when eligible, else why not
    z: dict[str, float] = field(default_factory=dict)
    composite: float | None = None
    rank: int | None = None
    weight: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "signals": {k: s.as_dict() for k, s in self.signals.items()},
                "liquidity": self.liquidity or "ok", "z": self.z, "composite": self.composite, "rank": self.rank,
                "weight": self.weight}  # fmt: skip


def liquidity(pit: PointInTime, ticker: str, cfg: Config) -> str:
    rows = pit.volumes(ticker, 45)[-20:]
    if len(rows) < 10:
        return "fewer than 10 sessions with volume"
    if rows[-1][1] < cfg.min_price:
        return f"price {rows[-1][1]:.2f} below {cfg.min_price:.2f}"
    tv = statistics.median(c * v for _, c, v in rows)
    if tv < cfg.min_traded_value:
        return f"median traded value {tv:,.0f} below {cfg.min_traded_value:,.0f}"
    return ""


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def zscores(values: dict[str, float], clip: float) -> dict[str, float]:
    if len(values) < 2:
        return {k: 0.0 for k in values}
    xs = list(values.values())
    lo, hi = _quantile(xs, clip), _quantile(xs, 1 - clip)
    c = {k: min(max(v, lo), hi) for k, v in values.items()}
    mu, sd = statistics.fmean(c.values()), statistics.pstdev(c.values())
    return {k: (v - mu) / sd if sd > 0 else 0.0 for k, v in c.items()}


def rank(pit: PointInTime, tickers: Sequence[str], cfg: Config) -> list[Row]:
    rows = [Row(t, {n: SIGNALS[n](pit, t) for n in cfg.signals}, liquidity(pit, t, cfg)) for t in tickers]
    live = [r for r in rows if not r.liquidity]
    for n in cfg.signals:
        z = zscores({r.ticker: s.score for r in live if (s := r.signals[n]).score is not None}, cfg.clip)
        for r in live:
            if r.ticker in z:
                r.z[n] = z[r.ticker]
    scored = [r for r in live if len(r.z) == len(cfg.signals)]
    for r in scored:
        r.composite = statistics.fmean(r.z.values())
    scored.sort(key=lambda r: (-(r.composite or 0.0), r.ticker))
    for i, r in enumerate(scored, 1):
        r.rank = i
    book = scored[: cfg.n_long]
    for r in book:
        r.weight = min(cfg.sleeve / len(book), cfg.max_weight)
    return sorted(rows, key=lambda r: (r.rank is None, r.rank or 0, r.ticker))


def targets(rows: Sequence[Row]) -> dict[str, float]:
    return {r.ticker: r.weight for r in rows if r.weight > 0}
