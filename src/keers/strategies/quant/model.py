"""Rank companies on the signals and pick the long book.

``quant-c-v2`` (the default):

1. Signals in the composite: momentum 12-1, value (EBIT / EV) and quality (EBIT / assets). Each becomes a
   cross-sectional z-score among the companies where it is known, after clipping the raw scores at the 5th and
   95th percentiles so one outlier cannot dominate. The composite is the mean of the known z-scores; a company
   needs at least ``min_signals`` of them, otherwise it is not ranked.
2. Low volatility is not a return bet any more (it lost money in the first backtest). It sizes the book instead:
   weights are proportional to 1 / volatility, so a calm name gets more and a wild one less.
3. Eligible companies must pass a liquidity screen (price and median traded value over 20 sessions).
4. The book is the top ``n_long`` by composite, ``sleeve`` of NAV in total, at most ``max_weight`` a name, long
   only.

``quant-c-v1`` (kept for comparison): momentum and low volatility in the composite, equal weights.

Every signal is computed and reported, whether or not it is in the composite, so the backtest can show each one.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import fundamental
from .pit import PointInTime
from .signals import SIGNALS as PRICE_SIGNALS
from .signals import Signal

SIGNALS = {**PRICE_SIGNALS, "value": fundamental.value, "quality": fundamental.quality}

# Paper trading of strategy C stays off until a backtest of the current model beats both SPY and the equal-weight
# book, and the person approves it in a reviewed change to this line.
APPROVED_FOR_PAPER = False


@dataclass(frozen=True)
class Config:
    version: str = "quant-c-v2"
    n_long: int = 15
    sleeve: float = 0.30  # share of NAV for the whole book (strategy C's risk cap is 40%)
    max_weight: float = 0.05  # one name, share of NAV (the risk cap is 10%)
    min_price: float = 5.0
    min_traded_value: float = 5e6  # median close × volume over 20 sessions, USD
    signals: tuple[str, ...] = ("momentum_12_1", "value", "quality")
    min_signals: int = 2
    sizing: str = "inverse_volatility"  # or "equal"
    clip: float = 0.05

    def __post_init__(self) -> None:
        if self.n_long < 1 or not 0 < self.sleeve <= 0.40 or not 0 < self.max_weight <= 0.10:
            raise ValueError(
                "n_long must be at least 1, sleeve at most 0.40 and max_weight at most 0.10 of NAV"
            )
        unknown = set(self.signals) - set(SIGNALS)
        if unknown or not self.signals:
            raise ValueError(f"unknown signals: {sorted(unknown)}")
        if not 1 <= self.min_signals <= len(self.signals):
            raise ValueError("min_signals must be between 1 and the number of signals")
        if self.sizing not in ("inverse_volatility", "equal"):
            raise ValueError("sizing must be inverse_volatility or equal")

    @classmethod
    def preset(cls, name: str) -> Config:
        if name == "v1":
            return cls(version="quant-c-v1", signals=("momentum_12_1", "low_volatility"), min_signals=2,
                       sizing="equal")  # fmt: skip
        if name == "v2":
            return cls()
        raise ValueError(f"unknown preset {name}")

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "nLong": self.n_long, "sleeve": self.sleeve, "maxWeight": self.max_weight,
                "minPrice": self.min_price, "minTradedValue": self.min_traded_value, "signals": list(self.signals),
                "minSignals": self.min_signals, "sizing": self.sizing, "clip": self.clip}  # fmt: skip


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


def capped(raw: dict[str, float], total: float, cap: float) -> dict[str, float]:
    """Scale ``raw`` to sum to ``total`` with no weight above ``cap``; the excess goes to the uncapped names."""
    out: dict[str, float] = {}
    free = dict(raw)
    left = total
    while free:
        s = sum(free.values())
        scaled = {k: left * v / s for k, v in free.items()}
        over = {k for k, v in scaled.items() if v > cap}
        if not over:
            out.update(scaled)
            break
        for k in over:
            out[k] = cap
            left -= cap
            del free[k]
    return out


def rank(pit: PointInTime, tickers: Sequence[str], cfg: Config) -> list[Row]:
    names = list(dict.fromkeys([*cfg.signals, *SIGNALS]))
    rows = [Row(t, {n: SIGNALS[n](pit, t) for n in names}, liquidity(pit, t, cfg)) for t in tickers]
    live = [r for r in rows if not r.liquidity]
    for n in cfg.signals:
        z = zscores({r.ticker: s.score for r in live if (s := r.signals[n]).score is not None}, cfg.clip)
        for r in live:
            if r.ticker in z:
                r.z[n] = z[r.ticker]
    scored = [r for r in live if len(r.z) >= cfg.min_signals]
    for r in scored:
        r.composite = statistics.fmean(r.z.values())
    scored.sort(key=lambda r: (-(r.composite or 0.0), r.ticker))
    for i, r in enumerate(scored, 1):
        r.rank = i
    if cfg.sizing == "inverse_volatility":
        # A name needs a known volatility to be sized; without one it is skipped, never given a guessed weight.
        book = [r for r in scored if (r.signals["low_volatility"].value or 0) > 0][: cfg.n_long]
        raw = {r.ticker: 1 / (r.signals["low_volatility"].value or 1) for r in book}
    else:
        book = scored[: cfg.n_long]
        raw = {r.ticker: 1.0 for r in book}
    total = min(cfg.sleeve, cfg.max_weight * len(book))
    w = capped(raw, total, cfg.max_weight) if book else {}
    for r in book:
        r.weight = w[r.ticker]
    return sorted(rows, key=lambda r: (r.rank is None, r.rank or 0, r.ticker))


def targets(rows: Sequence[Row]) -> dict[str, float]:
    return {r.ticker: r.weight for r in rows if r.weight > 0}
