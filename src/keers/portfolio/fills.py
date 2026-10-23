"""Simulated fills: the reference price moved against us by a slippage margin, plus a commission.

There is no exchange and no order book. The fill is a deliberately pessimistic estimate:

    buy fill  = reference price × (1 + slippage)
    sell fill = reference price × (1 − slippage)
    commission = max(minimum, |shares × fill| × commission rate)

The defaults (10 basis points of slippage, 5 of commission, at least 1.00) are ``costs-v1``. They are meant for
liquid US shares; the liquidity limits keep trades small against daily volume so the margin stays plausible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

BPS = 1e-4


@dataclass(frozen=True)
class CostModel:
    version: str = "costs-v1"
    slippage_bps: float = 10.0
    commission_bps: float = 5.0
    min_commission: float = 1.0

    def __post_init__(self) -> None:
        for k in ("slippage_bps", "commission_bps", "min_commission"):
            v = getattr(self, k)
            if not isinstance(v, int | float) or not 0 <= v <= 1000:
                raise ValueError(f"{k} must be between 0 and 1000")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Fill:
    quantity: float  # signed: positive buys
    reference_price: float
    price: float
    commission: float
    slippage_bps: float
    commission_bps: float
    cost_version: str

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    @property
    def cash_change(self) -> float:
        return -self.notional - self.commission

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notional"] = self.notional
        d["cashChange"] = self.cash_change
        return d


def fill_price(reference: float, quantity: float, costs: CostModel) -> float:
    if not reference > 0:
        raise ValueError("reference price must be positive")
    side = 1 if quantity > 0 else -1
    return reference * (1 + side * costs.slippage_bps * BPS)


def simulate(quantity: float, reference: float, costs: CostModel) -> Fill:
    if quantity == 0:
        raise ValueError("quantity must not be zero")
    px = fill_price(reference, quantity, costs)
    commission = max(costs.min_commission, abs(quantity * px) * costs.commission_bps * BPS)
    return Fill(quantity, reference, px, commission, costs.slippage_bps, costs.commission_bps, costs.version)
