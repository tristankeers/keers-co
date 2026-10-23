"""Turn the target book into paper trade tickets for strategy C. Plans only; it never places a trade.

Each ticket is sent by the workbench through ``Workbench.paper_trade``, so the paper desk sizes and prices it and
``keers.risk`` approves or blocks it like any other trade. Sells come first so their cash is there for the buys.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MIN_TRADE = 0.0025  # of NAV; smaller differences are left alone


def plan(targets: Mapping[str, float], positions: Sequence[Mapping[str, Any]], min_trade: float = MIN_TRADE,
         note: str = "") -> tuple[list[dict[str, Any]], list[str]]:  # fmt: skip
    """``positions`` are the portfolio view's rows. Returns (tickets, problems)."""
    held: dict[str, float] = {}
    problems: list[str] = []
    skip: set[str] = set()
    for p in positions:
        if p.get("strategy") != "C" or not p.get("quantity"):
            continue
        if p.get("weight") is None:
            problems.append(f"{p['symbol']}: no current weight (no price), so it is not rebalanced")
            skip.add(str(p["symbol"]))
            continue
        held[str(p["symbol"])] = float(p["weight"])
    sells: list[dict[str, Any]] = []
    buys: list[dict[str, Any]] = []
    for sym in sorted((set(held) | set(targets)) - skip):
        now, want = held.get(sym, 0.0), targets.get(sym, 0.0)
        base = {"symbol": sym, "strategy": "C", "note": note}
        if want == 0 and now != 0:
            sells.append({**base, "side": "sell", "close": True})
        elif want - now >= min_trade:
            buys.append({**base, "side": "buy", "weight": round(want - now, 6)})
        elif now - want >= min_trade:
            sells.append({**base, "side": "sell", "weight": round(now - want, 6)})
    return sells + buys, problems
