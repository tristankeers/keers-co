"""Positions, cash, P&L, exposures and drawdown, rebuilt from the filled orders every time.

Methods:

- A position is keyed by symbol and strategy, so the same share can sit in two sleeves.
- Average cost: adding to a position re-weights the average fill price; reducing it realises
  (fill − average cost) × shares sold for a long, the reverse for a short. A trade may not flip a long into a
  short in one step (the desk refuses it), so this rule is always enough.
- Commissions are kept apart from trading P&L: total P&L = realised + unrealised − commissions.
- Cash = starting cash − Σ(shares × fill price) − Σ commissions. Short sales credit cash. No interest, borrow
  fees or dividends are modelled yet.
- NAV = cash + Σ(shares × mark price). Daily NAV history uses each day's stored close (the last earlier close
  when a day is missing, the fill price before the first close), and today's NAV uses the live marks.
- Drawdown = 1 − NAV ÷ highest NAV so far.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .ledger import Order, Portfolio
from .market import MarketMark

Key = tuple[str, str]  # (symbol, strategy)


@dataclass
class Position:
    symbol: str
    strategy: str
    sector: str | None = None
    sector_basis: str = ""
    quantity: float = 0.0
    avg_cost: float = 0.0
    realised: float = 0.0
    commissions: float = 0.0
    opened: str | None = None
    last_trade: str | None = None
    trades: int = 0


def apply(p: Position, o: Order) -> None:
    """Fold one filled order into its position (average-cost method)."""
    assert o.fill_price is not None and o.commission is not None  # noqa: S101
    q, dq, px = p.quantity, o.quantity, o.fill_price
    if q == 0 or (q > 0) == (dq > 0):
        new = q + dq
        p.avg_cost = (q * p.avg_cost + dq * px) / new if new else 0.0
        p.quantity = new
        if q == 0:
            p.opened = o.trade_date
    else:
        closed = min(abs(dq), abs(q))
        p.realised += closed * (px - p.avg_cost) * (1 if q > 0 else -1)
        p.quantity = q + dq
        if abs(p.quantity) < 1e-9:
            p.quantity, p.avg_cost = 0.0, 0.0
    p.commissions += o.commission
    p.sector, p.sector_basis = o.sector or p.sector, o.sector_basis if o.sector else p.sector_basis
    p.last_trade = o.trade_date
    p.trades += 1


def positions(orders: Iterable[Order]) -> dict[Key, Position]:
    out: dict[Key, Position] = {}
    for o in orders:
        if o.status != "filled":
            continue
        p = out.setdefault((o.symbol, o.strategy), Position(o.symbol, o.strategy))
        apply(p, o)
    return out


def cash(portfolio: Portfolio, orders: Iterable[Order]) -> float:
    c = portfolio.starting_cash
    for o in orders:
        if o.status == "filled" and o.fill_price is not None and o.commission is not None:
            c -= o.quantity * o.fill_price + o.commission
    return c


@dataclass
class NavPoint:
    day: date
    nav: float
    drawdown: float


def nav_history(portfolio: Portfolio, orders: list[Order], closes: Callable[[str, date, date], list[tuple[date, float]]],
                end: date) -> list[NavPoint]:  # fmt: skip
    """End-of-day NAV for each weekday from the first fill up to ``end`` (inclusive), from stored closes."""
    filled = [o for o in orders if o.status == "filled"]
    if not filled:
        return []
    start = min(date.fromisoformat(o.trade_date) for o in filled)
    if start > end:
        return []
    symbols = sorted({o.symbol for o in filled})
    series = {s: dict(closes(s, start - timedelta(days=10), end)) for s in symbols}
    last_px: dict[str, float] = {}
    for s in symbols:
        before = [px for d, px in sorted(series[s].items()) if d < start]
        if before:
            last_px[s] = before[-1]
    qty: dict[str, float] = {}
    c = portfolio.starting_cash
    i, out, peak = 0, [], None
    d = start
    filled.sort(key=lambda o: (o.trade_date, o.id or 0))
    while d <= end:
        while i < len(filled) and date.fromisoformat(filled[i].trade_date) <= d:
            o = filled[i]
            assert o.fill_price is not None and o.commission is not None  # noqa: S101
            qty[o.symbol] = qty.get(o.symbol, 0.0) + o.quantity
            c -= o.quantity * o.fill_price + o.commission
            last_px.setdefault(o.symbol, o.fill_price)
            i += 1
        for s in symbols:
            if d in series[s]:
                last_px[s] = series[s][d]
        if d.weekday() < 5:
            nav = c + sum(q * last_px.get(s, 0.0) for s, q in qty.items())
            peak = nav if peak is None else max(peak, nav)
            out.append(NavPoint(d, nav, 0.0 if peak <= 0 else max(0.0, 1 - nav / peak)))
        d += timedelta(days=1)
    return out


@dataclass
class Valuation:
    cash: float
    nav: float | None
    long: float
    short: float
    rows: list[dict[str, Any]] = field(default_factory=list)


def value(pos: dict[Key, Position], marks: dict[str, MarketMark], cash_: float) -> Valuation:
    rows = []
    long = short = 0.0
    priced = True
    for (sym, strat), p in sorted(pos.items()):
        m = marks.get(sym)
        px = m.price if m else None
        mv = p.quantity * px if px is not None else None
        unreal = (
            (px - p.avg_cost) * p.quantity
            if px is not None and p.quantity
            else (0.0 if not p.quantity else None)
        )
        if p.quantity and mv is None:
            priced = False
        if mv is not None:
            long += max(mv, 0.0)
            short += min(mv, 0.0)
        rows.append({
            "symbol": sym, "strategy": strat, "sector": p.sector, "sectorBasis": p.sector_basis,
            "quantity": p.quantity, "avgCost": p.avg_cost if p.quantity else None, "price": px,
            "priceSource": m.source if m else None, "priceAt": m.at if m else None,
            "priceWarnings": list(m.warnings) if m else ["no price"], "marketValue": mv,
            "unrealised": unreal, "realised": p.realised, "commissions": p.commissions,
            "pnl": (p.realised + unreal - p.commissions) if unreal is not None else None,
            "open": bool(p.quantity), "opened": p.opened, "lastTrade": p.last_trade, "trades": p.trades,
            "advShares": m.adv_shares if m else None,
        })  # fmt: skip
    nav = cash_ + long + short if priced else None
    for r in rows:
        mv_ = r["marketValue"]
        r["weight"] = mv_ / nav if nav and isinstance(mv_, float | int) else None
    return Valuation(cash_, nav, long, short, rows)


def summary(
    portfolio: Portfolio, val: Valuation, pos: dict[Key, Position], history: list[NavPoint]
) -> dict[str, Any]:
    nav = val.nav
    realised = sum(p.realised for p in pos.values())
    commissions = sum(p.commissions for p in pos.values())
    unreal = [r["unrealised"] for r in val.rows if r["open"]]
    unrealised = sum(unreal) if all(u is not None for u in unreal) else None
    navs = [h.nav for h in history] + ([nav] if nav is not None else [])
    peak = max(navs) if navs else None
    dd = (0.0 if not peak or peak <= 0 else max(0.0, 1 - nav / peak)) if nav is not None else None
    max_dd = max([h.drawdown for h in history] + ([dd] if dd is not None else []), default=0.0)

    def share(x: float | None) -> float | None:
        return x / nav if x is not None and nav else None

    by_strategy: dict[str, float] = {}
    by_sector: dict[str, float] = {}
    for r in val.rows:
        if r["marketValue"] is None:
            continue
        by_strategy[r["strategy"]] = by_strategy.get(r["strategy"], 0.0) + abs(r["marketValue"])
        s = r["sector"] or "unknown"
        by_sector[s] = by_sector.get(s, 0.0) + abs(r["marketValue"])
    gross = val.long - val.short
    return {
        "nav": nav, "cash": val.cash, "startingCash": portfolio.starting_cash,
        "totalReturn": nav / portfolio.starting_cash - 1 if nav is not None else None,
        "pnl": nav - portfolio.starting_cash if nav is not None else None,
        "realised": realised, "unrealised": unrealised, "commissions": commissions,
        "long": val.long, "short": val.short, "gross": gross, "net": val.long + val.short,
        "grossPct": share(gross), "netPct": share(val.long + val.short), "cashPct": share(val.cash),
        "byStrategy": {k: {"value": v, "pct": share(v)} for k, v in sorted(by_strategy.items())},
        "bySector": {k: {"value": v, "pct": share(v)} for k, v in sorted(by_sector.items())},
        "peakNav": peak, "drawdown": dd, "maxDrawdown": max_dd,
    }  # fmt: skip
