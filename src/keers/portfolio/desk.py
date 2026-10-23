"""The paper trading desk: sizes a requested trade, prices a simulated fill, asks the risk module, and writes the
order to the ledger only when the risk report is approved. A blocked trade is written too, as rejected.

The desk never decides a limit. It builds the facts (cash, holdings, marks, NAV history, the proposed trade) and
hands them to ``keers.risk.checks``, which works the exposures out again on its own.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from keers.risk import checks as R
from keers.risk.limits import STRATEGIES, STRATEGY_NAMES, Limits

from . import analytics as A
from .fills import CostModel, simulate
from .ledger import Ledger, Order, Portfolio, now_utc
from .market import MarketMark

SECTOR_MAX = 60


class TradeError(ValueError):
    """The request itself is invalid (not a risk decision)."""


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    strategy: str
    side: str  # buy | sell
    shares: float | None = None
    weight: float | None = None  # share of NAV to trade
    amount: float | None = None  # base-currency amount to trade
    sector: str | None = None
    close: bool = False  # trade the whole position back to zero
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TradeRequest:
        def num(k: str) -> float | None:
            v = d.get(k)
            if v is None or v == "":
                return None
            try:
                x = float(v)
            except (TypeError, ValueError) as e:
                raise TradeError(f"{k} must be a number") from e
            if not math.isfinite(x) or x <= 0:
                raise TradeError(f"{k} must be a positive number")
            return x

        sector = " ".join(str(d.get("sector") or "").split())[:SECTOR_MAX] or None
        return cls(symbol=str(d.get("symbol") or "").strip().upper(), strategy=str(d.get("strategy") or "").strip().upper(),
                   side=str(d.get("side") or "buy").strip().lower(), shares=num("shares"), weight=num("weight"),
                   amount=num("amount"), sector=sector, close=bool(d.get("close")),
                   note=" ".join(str(d.get("note") or "").split())[:200])  # fmt: skip


MarkFn = Callable[[str], MarketMark]
ClosesFn = Callable[[str, date, date], list[tuple[date, float]]]


class Desk:
    def __init__(self, ledger: Ledger, marks: MarkFn, closes: ClosesFn, today: Callable[[], date],
                 limits: Limits | None = None, costs: CostModel | None = None) -> None:  # fmt: skip
        self.ledger = ledger
        self.marks = marks
        self.closes = closes
        self.today = today
        self.limits = limits or Limits()
        self.costs = costs or CostModel()

    # state ------------------------------------------------------------------------------------------------------
    def _portfolio(self, pid: int) -> Portfolio:
        p = self.ledger.portfolio(pid)
        if p is None:
            raise TradeError(f"no portfolio {pid}")
        return p

    def _state(self, pid: int, extra: str | None = None) -> dict[str, Any]:
        p = self._portfolio(pid)
        orders = self.ledger.orders(pid)
        filled = [o for o in orders if o.status == "filled"]
        pos = A.positions(filled)
        syms = sorted({k[0] for k, v in pos.items() if v.quantity} | ({extra} if extra else set()))
        marks = {s: self.marks(s) for s in syms}
        today = self.today()
        history = A.nav_history(p, filled, self.closes, today - timedelta(days=1))
        return {"portfolio": p, "orders": orders, "filled": filled, "positions": pos, "marks": marks,
                "history": history, "cash": A.cash(p, filled)}  # fmt: skip

    def _book(self, st: dict[str, Any]) -> R.Book:
        holdings = tuple(
            R.Holding(p.symbol, p.strategy, p.sector, p.quantity, st["marks"][p.symbol].risk_mark())
            for p in st["positions"].values() if p.quantity
        )  # fmt: skip
        return R.Book(st["cash"], holdings, tuple(h.nav for h in st["history"]))

    # views ------------------------------------------------------------------------------------------------------
    def view(self, pid: int) -> dict[str, Any]:
        st = self._state(pid)
        p = st["portfolio"]
        val = A.value(st["positions"], st["marks"], st["cash"])
        report = R.assess(self._book(st), self.limits, self.today())
        return {
            "portfolio": p.as_dict(),
            "today": self.today().isoformat(),
            "summary": A.summary(p, val, st["positions"], st["history"]),
            "positions": val.rows,
            "history": [
                {"day": h.day.isoformat(), "nav": h.nav, "drawdown": h.drawdown} for h in st["history"]
            ],
            "risk": report.as_dict(),
            "orders": [o.as_dict() for o in reversed(st["orders"][-200:])],
            "limits": self.limits.as_dict(),
            "costs": self.costs.as_dict(),
            "strategies": {k: STRATEGY_NAMES[k] for k in STRATEGIES},
        }

    # trading ----------------------------------------------------------------------------------------------------
    def _plan(self, pid: int, req: TradeRequest) -> dict[str, Any]:
        if not req.symbol or len(req.symbol) > 24:
            raise TradeError("enter a symbol")
        if req.strategy not in STRATEGIES:
            raise TradeError(f"choose a strategy: {', '.join(STRATEGIES)}")
        if req.side not in ("buy", "sell"):
            raise TradeError("side must be buy or sell")
        st = self._state(pid, req.symbol)
        m: MarketMark = st["marks"][req.symbol]
        held = st["positions"].get((req.symbol, req.strategy))
        pre_qty = held.quantity if held else 0.0

        if req.close:
            if not pre_qty:
                raise TradeError(f"no open {req.symbol} position in strategy {req.strategy}")
            qty = -pre_qty
        else:
            sizes = [x for x in (req.shares, req.weight, req.amount) if x is not None]
            if len(sizes) != 1:
                raise TradeError("give exactly one size: shares, weight of NAV, or amount")
            if req.shares is not None:
                if req.shares != int(req.shares):
                    raise TradeError("shares must be a whole number")
                n = req.shares
            else:
                if m.price is None or not m.price > 0:
                    raise TradeError(
                        f"{req.symbol}: no price, so a size by weight or amount cannot be worked out"
                    )
                ref = m.price * (1 + self.costs.slippage_bps * 1e-4 * (1 if req.side == "buy" else -1))
                if req.amount is not None:
                    target = req.amount
                else:
                    nav = A.value(st["positions"], st["marks"], st["cash"]).nav
                    if nav is None or nav <= 0:
                        raise TradeError(
                            "NAV is unknown (a holding has no price), so a weight cannot be sized"
                        )
                    assert req.weight is not None  # noqa: S101
                    if req.weight > 5:
                        raise TradeError("weight is a share of NAV, e.g. 0.05 for 5%")
                    target = req.weight * nav
                n = math.floor(target / ref)
            if n < 1:
                raise TradeError("that size is less than one share")
            qty = n if req.side == "buy" else -n
            if pre_qty and (pre_qty > 0) != (pre_qty + qty > 0) and pre_qty + qty != 0:
                raise TradeError(
                    "this trade would flip the position from long to short (or back); close it first"
                )

        sector: str | None
        if req.sector:
            sector, basis = req.sector, "entered"
        elif held and held.sector:
            sector, basis = held.sector, held.sector_basis or "position"
        else:
            sector, basis = m.sector, m.sector_basis
        fill = simulate(qty, m.price, self.costs) if m.price is not None and m.price > 0 else None
        trade = R.Trade(req.symbol, req.strategy, sector, qty, fill.price if fill else 0.0,
                        fill.commission if fill else 0.0, m.risk_mark())  # fmt: skip
        report = R.check_trade(self._book(st), trade, self.limits, self.today())
        return {"request": req, "quantity": qty, "preQuantity": pre_qty, "mark": m, "fill": fill,
                "sector": sector, "sectorBasis": basis or "", "report": report}  # fmt: skip

    def _payload(self, plan: dict[str, Any]) -> dict[str, Any]:
        f = plan["fill"]
        return {"symbol": plan["request"].symbol, "strategy": plan["request"].strategy, "quantity": plan["quantity"],
                "positionBefore": plan["preQuantity"], "positionAfter": plan["preQuantity"] + plan["quantity"],
                "sector": plan["sector"], "sectorBasis": plan["sectorBasis"], "mark": plan["mark"].as_dict(),
                "fill": f.as_dict() if f else None, "risk": plan["report"].as_dict()}  # fmt: skip

    def preview(self, pid: int, req: TradeRequest) -> dict[str, Any]:
        """What the trade would do and what the risk checks say. Writes nothing."""
        return self._payload(self._plan(pid, req))

    def execute(self, pid: int, req: TradeRequest) -> dict[str, Any]:
        """Fill the trade on paper if, and only if, the risk report approves it. Either way the attempt is logged."""
        plan = self._plan(pid, req)
        report: R.Report = plan["report"]
        m: MarketMark = plan["mark"]
        f = plan["fill"]
        approved = report.approved and f is not None
        reason = "" if approved else "; ".join(
            [f"{c.name}: {c.detail}" for c in report.breaches] + [f"{n}: check missing" for n in report.missing]
        ) or "no fill price"  # fmt: skip
        order = Order(
            portfolio_id=pid, created_at=now_utc(), trade_date=self.today().isoformat(),
            status="filled" if approved else "rejected", symbol=req.symbol, strategy=req.strategy,
            sector=plan["sector"], sector_basis=plan["sectorBasis"] or "none", quantity=plan["quantity"],
            reference_price=m.price, price_source=m.source, price_at=m.at, price_sha256=m.raw_sha256,
            fill_price=f.price if approved else None, commission=f.commission if approved else None,
            slippage_bps=self.costs.slippage_bps, commission_bps=self.costs.commission_bps,
            cost_version=self.costs.version, currency=m.currency, limits_version=self.limits.version,
            risk=report.as_dict(), reason=reason, note=req.note,
        )  # fmt: skip
        saved = self.ledger.append(order)
        return {**self._payload(plan), "order": saved.as_dict(), "filled": approved}
