"""Simulated fills: the reference price moved against us by a slippage margin, plus a commission.

There is no exchange and no order book. The fill is a deliberately pessimistic estimate:

    buy fill  = reference price × (1 + slippage)
    sell fill = reference price × (1 − slippage)
    commission = max(minimum, |shares × fill| × commission rate)

The defaults (10 basis points of slippage, 5 of commission, at least 1.00) are ``costs-v1``. They are meant for
liquid US shares; the liquidity limits keep trades small against daily volume so the margin stays plausible.

``costs-v2`` (``CostModel.v2()``, the paper desk's default) prices a fill the way a trading desk splits its cost:

    price move = half the bid-ask spread + market impact
    impact     = Y × daily volatility × √(shares / average daily volume)     (the square-root law)
    commission = shares × 0.005, at least 1.00 and at most 1% of the trade's value

The square-root law is from Almgren, Thum, Hauptmann and Li, "Direct estimation of equity market impact" (2005)
and Tóth, Bouchaud et al. (2011); published fits put Y between 0.5 and 1, and v2 uses 1, the costly end. The
commission is Interactive Brokers' fixed US rate. A large order in a thin, volatile name now costs more than a small
one in a liquid name, which a flat margin cannot show. When the volume or volatility is unknown the impact is
taken as ``fallback_impact_bps`` (25 basis points, on the costly side) and the fill says so. The idea of moving to
this model came from the MIT-licensed virattt/ai-hedge-fund project; the code is our own.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

BPS = 1e-4


@dataclass(frozen=True)
class CostModel:
    version: str = "costs-v1"
    slippage_bps: float = 10.0  # v1: the whole price move
    commission_bps: float = 5.0  # v1
    min_commission: float = 1.0
    model: str = "flat"  # "flat" (v1) or "impact" (v2)
    half_spread_bps: float = 0.0  # v2
    impact_coefficient: float = 0.0  # v2: Y in the square-root law
    commission_per_share: float = 0.0  # v2
    max_commission_pct: float = 0.0  # v2: cap as a share of the trade's value
    fallback_impact_bps: float = 0.0  # v2: impact when volume or volatility is unknown

    @classmethod
    def v2(cls) -> CostModel:
        return cls(version="costs-v2", slippage_bps=0.0, commission_bps=0.0, min_commission=1.0, model="impact",
                   half_spread_bps=5.0, impact_coefficient=1.0, commission_per_share=0.005,
                   max_commission_pct=0.01, fallback_impact_bps=25.0)  # fmt: skip

    @classmethod
    def named(cls, version: str) -> CostModel:
        if version in ("v1", "costs-v1"):
            return cls()
        if version in ("v2", "costs-v2"):
            return cls.v2()
        raise ValueError(f"unknown cost model {version}")

    def __post_init__(self) -> None:
        for k in (
            "slippage_bps",
            "commission_bps",
            "min_commission",
            "half_spread_bps",
            "fallback_impact_bps",
        ):
            v = getattr(self, k)
            if not isinstance(v, int | float) or not 0 <= v <= 1000:
                raise ValueError(f"{k} must be between 0 and 1000")
        if self.model not in ("flat", "impact"):
            raise ValueError("model must be flat or impact")
        for k in ("impact_coefficient", "commission_per_share", "max_commission_pct"):
            v = getattr(self, k)
            if not isinstance(v, int | float) or not 0 <= v <= 5:
                raise ValueError(f"{k} must be between 0 and 5")

    def sizing_bps(self) -> float:
        """A price-move estimate for working out how many shares a weight or amount buys (not the fill itself)."""
        return self.slippage_bps if self.model == "flat" else self.half_spread_bps + self.fallback_impact_bps

    def move(self, shares: float, adv: float | None, sigma: float | None) -> tuple[float, str]:
        """The fractional price move against the order, and how it was worked out."""
        if self.model == "flat":
            return self.slippage_bps * BPS, "flat slippage"
        if adv and adv > 0 and sigma is not None and sigma > 0:
            impact = self.impact_coefficient * sigma * math.sqrt(abs(shares) / adv)
            return self.half_spread_bps * BPS + impact, "half spread + square-root impact"
        return (
            self.half_spread_bps + self.fallback_impact_bps
        ) * BPS, "half spread + fallback impact (volume or volatility unknown)"

    def commission(self, shares: float, price: float) -> float:
        value = abs(shares * price)
        if self.model == "flat":
            return max(self.min_commission, value * self.commission_bps * BPS)
        fee = max(self.min_commission, abs(shares) * self.commission_per_share)
        return min(fee, value * self.max_commission_pct) if self.max_commission_pct else fee

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Fill:
    quantity: float  # signed: positive buys
    reference_price: float
    price: float
    commission: float
    slippage_bps: float  # the price move actually applied
    commission_bps: float  # the commission as a share of the trade's value
    cost_version: str
    basis: str = ""  # how the price move was worked out

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


def fill_price(reference: float, quantity: float, costs: CostModel, adv: float | None = None,
               sigma: float | None = None) -> float:  # fmt: skip
    if not reference > 0:
        raise ValueError("reference price must be positive")
    side = 1 if quantity > 0 else -1
    return reference * (1 + side * costs.move(quantity, adv, sigma)[0])


def simulate(quantity: float, reference: float, costs: CostModel, adv: float | None = None,
             sigma: float | None = None) -> Fill:  # fmt: skip
    """``adv`` is the average daily volume in shares and ``sigma`` the daily volatility (v2 only)."""
    if quantity == 0:
        raise ValueError("quantity must not be zero")
    if not reference > 0:
        raise ValueError("reference price must be positive")
    move, basis = costs.move(quantity, adv, sigma)
    px = reference * (1 + (1 if quantity > 0 else -1) * move)
    commission = costs.commission(quantity, px)
    return Fill(quantity, reference, px, commission, move / BPS, commission / abs(quantity * px) / BPS,
                costs.version, basis)  # fmt: skip


def cost_fraction(
    value: float, price: float, costs: CostModel, adv: float | None, sigma: float | None
) -> float:
    """What trading ``value`` (in money, either side) of one name costs, as a share of ``value``. For backtests."""
    if value <= 0 or not price > 0:
        return 0.0
    shares = value / price
    move = costs.move(shares, adv, sigma)[0]
    return move + costs.commission(shares, price * (1 + move)) / (value * (1 + move))
