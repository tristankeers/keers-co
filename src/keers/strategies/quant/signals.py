"""Price signals for strategy C (``signals-v1``). Each returns a value or ``None`` with the reason.

* Momentum 12-1: adjusted close 21 sessions ago / adjusted close 252 sessions ago − 1. The latest month is left
  out because one-month returns tend to reverse (Jegadeesh and Titman 1993).
* Low volatility: annualised standard deviation of daily log returns over the last 252 sessions. Lower is better,
  so the score is the negative (the low-volatility anomaly, Ang et al. 2006; Frazzini and Pedersen 2014).

Both are unknown, never guessed, when the history is too short, has gaps, or the last price is stale. An unknown
signal means the company is not ranked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from keers.data.snapshot import business_days_between

from .pit import PointInTime

SIGNALS_VERSION = "signals-v1"
LOOKBACK = 252  # sessions in a year
SKIP = 21  # sessions in a month
WINDOW_DAYS = 400  # calendar days fetched to find LOOKBACK + 1 sessions
MIN_COVERAGE = 0.95  # share of the benchmark's sessions the stock must have prices for
MAX_PRICE_AGE = 3  # business days
TRADING_DAYS = 252


@dataclass(frozen=True)
class Signal:
    name: str
    value: float | None  # the raw measure (a return, or a volatility)
    score: float | None  # higher is better
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "score": self.score, "reason": self.reason}


def _history(pit: PointInTime, ticker: str, sessions: int) -> tuple[list[float] | None, str]:
    px = pit.adjusted_closes(ticker, WINDOW_DAYS)
    if not px:
        return None, "no adjusted closes in the store"
    age = business_days_between(px[-1][0], pit.as_of)
    if age > MAX_PRICE_AGE:
        return None, f"last price {px[-1][0]} is {age} business days old"
    if len(px) < sessions + 1:
        return None, f"only {len(px)} sessions of history, needs {sessions + 1}"
    window = px[-(sessions + 1) :]
    span = business_days_between(window[0][0], window[-1][0])
    if span and len(window) - 1 < MIN_COVERAGE * span:
        return None, f"gaps in history: {len(window) - 1} sessions over {span} business days"
    return [a for _, a in window], ""


def momentum(pit: PointInTime, ticker: str) -> Signal:
    closes, why = _history(pit, ticker, LOOKBACK)
    if closes is None:
        return Signal("momentum_12_1", None, None, why)
    r = closes[-1 - SKIP] / closes[0] - 1
    return Signal("momentum_12_1", r, r, f"return from {LOOKBACK} to {SKIP} sessions ago")


def volatility(pit: PointInTime, ticker: str) -> Signal:
    closes, why = _history(pit, ticker, LOOKBACK)
    if closes is None:
        return Signal("low_volatility", None, None, why)
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(TRADING_DAYS)
    return Signal("low_volatility", vol, -vol, f"annualised volatility of {len(rets)} daily log returns")


SIGNALS = {"momentum_12_1": momentum, "low_volatility": volatility}
