"""Pre-trade and portfolio risk checks. Deterministic, and separate from anything that proposes trades.

This module imports nothing from the portfolio, the strategies or the web layer. It is handed plain facts (cash,
holdings with their prices, average volumes and sectors, the NAV history, and the proposed trade), works out the
exposures itself, and returns one ``Check`` per rule.

Approval is strict:

- every check has a status: ``pass``, ``warn`` (passes, but flagged), ``fail`` or ``unknown``;
- ``unknown`` (a figure the rule needs is missing) never passes;
- a check that raises an error counts as ``fail``;
- a report is approved only when every required check is present and every check passed or warned. A check that
  did not run is a missing check, and a missing check blocks the trade.

A limit that is already breached does not trap the book: a trade that brings a breached figure down passes that
check (it says so), while any trade that pushes it further up fails.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from .limits import STRATEGIES, Limits

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
OK = (PASS, WARN)

# Every pre-trade report must contain at least one check of each of these, or it is not approved.
TRADE_CHECKS = (
    "data.strategy",
    "data.trade_price",
    "data.currency",
    "data.book_prices",
    "data.sector",
    "position.size",
    "concentration.sector",
    "concentration.strategy",
    "liquidity.trade",
    "liquidity.position",
    "exposure.gross",
    "exposure.net",
    "cash.no_borrowing",
    "drawdown",
)
BOOK_CHECKS = (
    "data.book_prices",
    "exposure.gross",
    "exposure.net",
    "cash.no_borrowing",
    "drawdown",
)


# --------------------------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Mark:
    """What is known about one security's market data."""

    price: float | None
    day: date | None  # trading date of the price
    currency: str | None
    adv_shares: float | None = None  # average daily volume in shares; None when too little history
    warnings: tuple[str, ...] = ()  # from the source, e.g. "stale: market is open but …"


@dataclass(frozen=True)
class Holding:
    symbol: str
    strategy: str
    sector: str | None
    quantity: float  # shares; negative is short
    mark: Mark


@dataclass(frozen=True)
class Book:
    cash: float
    holdings: tuple[Holding, ...] = ()
    nav_history: tuple[float, ...] = ()  # end-of-day NAV, oldest first, before today


@dataclass(frozen=True)
class Trade:
    symbol: str
    strategy: str
    sector: str | None
    quantity: float  # signed change in shares: positive buys, negative sells or shorts
    price: float  # expected fill price, after slippage
    cost: float  # commissions and fees, in the base currency
    mark: Mark


# --------------------------------------------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    subject: str = ""  # which position, sector or strategy
    value: float | None = None
    limit: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "subject": self.subject,
                "value": self.value, "limit": self.limit}  # fmt: skip


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]
    required: tuple[str, ...]
    limits_version: str
    figures: dict[str, Any] = field(default_factory=dict)  # the exposures the checks used, for display

    @property
    def missing(self) -> list[str]:
        ran = {c.name for c in self.checks}
        return [r for r in self.required if r not in ran]

    @property
    def approved(self) -> bool:
        return bool(self.checks) and not self.missing and all(c.status in OK for c in self.checks)

    @property
    def breaches(self) -> list[Check]:
        return [c for c in self.checks if c.status not in OK]

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "limitsVersion": self.limits_version,
            "missing": self.missing,
            "checks": [c.as_dict() for c in self.checks],
            "breaches": [c.as_dict() for c in self.breaches],
            "warnings": [c.as_dict() for c in self.checks if c.status == WARN],
            "figures": self.figures,
        }


# --------------------------------------------------------------------------------------------------------------
# Arithmetic the checks share
# --------------------------------------------------------------------------------------------------------------
def business_days_between(a: date, b: date) -> int:
    """Weekdays after ``a`` up to and including ``b`` (holidays not modelled, so this errs strict)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def price_problem(mark: Mark, today: date, lim: Limits) -> str | None:
    """Why this price cannot be used, or None when it is present and fresh."""
    if mark.price is None:
        return "no price"
    if not mark.price > 0:
        return f"price {mark.price} is not positive"
    if mark.day is None:
        return "price has no date"
    age = business_days_between(mark.day, today)
    if age > lim.max_price_age_days:
        return f"price from {mark.day} is {age} business days old (limit {lim.max_price_age_days})"
    stale = [w for w in mark.warnings if w.lower().startswith("stale")]
    if stale:
        return stale[0]
    return None


@dataclass(frozen=True)
class Exposure:
    nav: float | None  # None when any holding has no price
    gross: float
    net: float
    by_position: dict[tuple[str, str], float]  # (symbol, strategy) -> market value
    by_sector: dict[str, float]  # absolute market value; sector None counted under ""
    by_strategy: dict[str, float]  # absolute market value

    def weight(self, mv: float) -> float | None:
        return abs(mv) / self.nav if self.nav is not None and self.nav > 0 else None

    def as_dict(self) -> dict[str, Any]:
        w = self.weight
        return {
            "nav": self.nav,
            "gross": self.gross,
            "net": self.net,
            "grossPct": w(self.gross),
            "netPct": (self.net / self.nav) if self.nav else None,
            "sectors": {k or "unknown": w(v) for k, v in sorted(self.by_sector.items())},
            "strategies": {k: w(v) for k, v in sorted(self.by_strategy.items())},
        }


def exposure(book: Book) -> Exposure:
    pos: dict[tuple[str, str], float] = {}
    sec: dict[str, float] = {}
    strat: dict[str, float] = {}
    gross = net = 0.0
    priced = True
    for h in book.holdings:
        if h.quantity == 0:
            continue
        if h.mark.price is None:
            priced = False
            continue
        mv = h.quantity * h.mark.price
        pos[(h.symbol, h.strategy)] = pos.get((h.symbol, h.strategy), 0.0) + mv
        gross += abs(mv)
        net += mv
    # Sector and strategy totals add each position's absolute value (a long and a short do not offset).
    for h in book.holdings:
        if h.quantity == 0 or h.mark.price is None:
            continue
        a = abs(h.quantity * h.mark.price)
        sec[h.sector or ""] = sec.get(h.sector or "", 0.0) + a
        strat[h.strategy] = strat.get(h.strategy, 0.0) + a
    return Exposure(book.cash + net if priced else None, gross, net, pos, sec, strat)


def after(book: Book, t: Trade) -> Book:
    """The book once ``t`` has filled at its expected price and cost."""
    holdings = list(book.holdings)
    for i, h in enumerate(holdings):
        if h.symbol == t.symbol and h.strategy == t.strategy:
            holdings[i] = replace(
                h, quantity=h.quantity + t.quantity, mark=t.mark, sector=t.sector or h.sector
            )
            break
    else:
        holdings.append(Holding(t.symbol, t.strategy, t.sector, t.quantity, t.mark))
    return Book(book.cash - t.quantity * t.price - t.cost, tuple(holdings), book.nav_history)


def drawdown(history: Iterable[float], nav: float | None) -> float | None:
    """Fall of ``nav`` from the highest NAV so far (0 at a new high). None when NAV is unknown."""
    if nav is None:
        return None
    peak = max([*history, nav])
    return 0.0 if peak <= 0 else max(0.0, 1 - nav / peak)


def _pct(x: float | None) -> str:
    return "unknown" if x is None else f"{x * 100:.1f}%"


def _cap(name: str, subject: str, pre: float | None, post: float | None, limit: float, what: str) -> Check:
    """A post-trade share against its cap; a trade that lowers a breached share passes."""
    if post is None:
        return Check(
            name, UNKNOWN, f"{what}: cannot be measured (NAV unknown or not positive)", subject, None, limit
        )
    if post <= limit + 1e-12:
        return Check(name, PASS, f"{what} {_pct(post)} within {_pct(limit)}", subject, post, limit)
    if pre is not None and post < pre - 1e-12:
        return Check(name, WARN, f"{what} {_pct(post)} is above {_pct(limit)}, but this trade lowers it from {_pct(pre)}",
                     subject, post, limit)  # fmt: skip
    return Check(name, FAIL, f"{what} {_pct(post)} exceeds the {_pct(limit)} limit", subject, post, limit)


def _safely(name: str, fn: Callable[[], list[Check]]) -> list[Check]:
    try:
        out = fn()
    except Exception as e:  # a check that cannot run must block, never pass
        return [Check(name, FAIL, f"check could not run: {type(e).__name__}: {e}")]
    return out or [Check(name, UNKNOWN, "check produced no result")]


# --------------------------------------------------------------------------------------------------------------
# Pre-trade checks
# --------------------------------------------------------------------------------------------------------------
def check_trade(book: Book, t: Trade, lim: Limits, today: date) -> Report:
    pre_x = exposure(book)
    post_book = after(book, t)
    post_x = exposure(post_book)
    key = (t.symbol, t.strategy)
    pre_qty = sum(h.quantity for h in book.holdings if (h.symbol, h.strategy) == key)
    post_qty = pre_qty + t.quantity
    adds_gross = post_x.gross > pre_x.gross + 1e-9

    def strategy() -> list[Check]:
        if t.strategy in STRATEGIES:
            return [Check("data.strategy", PASS, f"strategy {t.strategy}", t.strategy)]
        return [
            Check("data.strategy", FAIL, f"strategy must be one of {', '.join(STRATEGIES)}", str(t.strategy))
        ]

    def trade_price() -> list[Check]:
        why = price_problem(t.mark, today, lim)
        if why:
            return [Check("data.trade_price", FAIL, f"{t.symbol}: {why}", t.symbol)]
        if not t.price > 0:
            return [
                Check("data.trade_price", FAIL, f"{t.symbol}: fill price {t.price} is not positive", t.symbol)
            ]
        return [
            Check("data.trade_price", PASS, f"{t.symbol}: price {t.mark.price} on {t.mark.day}", t.symbol)
        ]

    def currency() -> list[Check]:
        c = (t.mark.currency or "").upper()
        if not c:
            return [Check("data.currency", UNKNOWN, f"{t.symbol}: currency unknown", t.symbol)]
        if c != lim.base_currency:
            return [Check("data.currency", FAIL, f"{t.symbol} trades in {c}; only {lim.base_currency} is supported "
                          "until FX conversion exists", t.symbol)]  # fmt: skip
        return [Check("data.currency", PASS, f"{t.symbol} in {c}", t.symbol)]

    def book_prices() -> list[Check]:
        return _book_price_checks(book, lim, today, skip=key)

    def sector() -> list[Check]:
        if t.sector and t.sector.strip():
            return [Check("data.sector", PASS, f"sector {t.sector}", t.symbol)]
        return [Check("data.sector", UNKNOWN, f"{t.symbol}: no sector, so sector concentration cannot be checked",
                      t.symbol)]  # fmt: skip

    def position() -> list[Check]:
        pre = pre_x.weight(pre_x.by_position.get(key, 0.0))
        post = post_x.weight(post_x.by_position.get(key, 0.0))
        return [_cap("position.size", f"{t.symbol}/{t.strategy}", pre, post, lim.max_position, "position")]

    def sector_cap() -> list[Check]:
        if not (t.sector and t.sector.strip()):
            return [Check("concentration.sector", UNKNOWN, "sector unknown", t.symbol, None, lim.max_sector)]
        pre = pre_x.weight(pre_x.by_sector.get(t.sector, 0.0))
        post = post_x.weight(post_x.by_sector.get(t.sector, 0.0))
        return [_cap("concentration.sector", t.sector, pre, post, lim.max_sector, f"sector {t.sector}")]

    def strategy_cap() -> list[Check]:
        cap = lim.max_strategy.get(t.strategy)
        if cap is None:
            return [
                Check(
                    "concentration.strategy", FAIL, f"no limit for strategy {t.strategy!r}", str(t.strategy)
                )
            ]
        pre = pre_x.weight(pre_x.by_strategy.get(t.strategy, 0.0))
        post = post_x.weight(post_x.by_strategy.get(t.strategy, 0.0))
        return [_cap("concentration.strategy", t.strategy, pre, post, cap, f"strategy {t.strategy} sleeve")]

    def liq_trade() -> list[Check]:
        adv = t.mark.adv_shares
        if adv is None or adv <= 0:
            return [Check("liquidity.trade", UNKNOWN, f"{t.symbol}: average daily volume unknown "
                          f"(needs {lim.min_adv_days} sessions with volume)", t.symbol, None, lim.max_trade_adv)]  # fmt: skip
        share = abs(t.quantity) / adv
        what = f"trade of {abs(t.quantity):,.0f} shares is {_pct(share)} of ADV {adv:,.0f};"
        if share > lim.max_trade_adv and abs(post_qty) < abs(pre_qty):
            # An exit is never blocked for being large; in practice it would be worked over several days.
            return [Check("liquidity.trade", WARN, f"{what} above {_pct(lim.max_trade_adv)}, allowed because it "
                          "reduces the position (a real exit this size would take several days)", t.symbol, share,
                          lim.max_trade_adv)]  # fmt: skip
        return [_cap("liquidity.trade", t.symbol, None, share, lim.max_trade_adv, what)]

    def liq_position() -> list[Check]:
        adv = t.mark.adv_shares
        if adv is None or adv <= 0:
            return [Check("liquidity.position", UNKNOWN, f"{t.symbol}: average daily volume unknown", t.symbol,
                          None, lim.max_position_adv)]  # fmt: skip
        pre, post = abs(pre_qty) / adv, abs(post_qty) / adv
        return [_cap("liquidity.position", t.symbol, pre, post, lim.max_position_adv,
                     f"position of {abs(post_qty):,.0f} shares is {_pct(post)} of ADV;")]  # fmt: skip

    def gross() -> list[Check]:
        return [_cap("exposure.gross", "book", pre_x.weight(pre_x.gross), post_x.weight(post_x.gross), lim.max_gross,
                     "gross exposure")]  # fmt: skip

    def net() -> list[Check]:
        return [_net_check(pre_x, post_x, lim)]

    def cash() -> list[Check]:
        if post_book.cash >= -1e-6:
            return [Check("cash.no_borrowing", PASS, f"cash after the trade {post_book.cash:,.2f}", "book",
                          post_book.cash, 0.0)]  # fmt: skip
        if t.quantity < 0:
            return [Check("cash.no_borrowing", WARN, "cash is negative, but this sale raises it", "book",
                          post_book.cash, 0.0)]  # fmt: skip
        return [Check("cash.no_borrowing", FAIL, f"the trade needs {-post_book.cash:,.2f} more cash than the book "
                      "has; paper portfolios do not borrow", "book", post_book.cash, 0.0)]  # fmt: skip

    def dd() -> list[Check]:
        return [_drawdown_check(book, pre_x, lim, adds_gross)]

    checks: list[Check] = []
    for name, fn in (
        ("data.strategy", strategy), ("data.trade_price", trade_price), ("data.currency", currency),
        ("data.book_prices", book_prices), ("data.sector", sector), ("position.size", position),
        ("concentration.sector", sector_cap), ("concentration.strategy", strategy_cap),
        ("liquidity.trade", liq_trade), ("liquidity.position", liq_position), ("exposure.gross", gross),
        ("exposure.net", net), ("cash.no_borrowing", cash), ("drawdown", dd),
    ):  # fmt: skip
        checks += _safely(name, fn)
    figures = {"before": pre_x.as_dict(), "after": post_x.as_dict(), "cashAfter": post_book.cash,
               "drawdown": drawdown(book.nav_history, pre_x.nav)}  # fmt: skip
    return Report(tuple(checks), TRADE_CHECKS, lim.version, figures)


def _book_price_checks(
    book: Book, lim: Limits, today: date, skip: tuple[str, str] | None = None
) -> list[Check]:
    bad = []
    for h in book.holdings:
        if h.quantity == 0 or (h.symbol, h.strategy) == skip:
            continue
        why = price_problem(h.mark, today, lim)
        if why is None and (h.mark.currency or "").upper() != lim.base_currency:
            why = f"currency {h.mark.currency or 'unknown'} is not {lim.base_currency}"
        if why:
            bad.append(Check("data.book_prices", FAIL, f"{h.symbol}: {why}; the book's exposures cannot be trusted",
                             h.symbol))  # fmt: skip
    return bad or [Check("data.book_prices", PASS, "every holding has a fresh price", "book")]


def _net_check(pre_x: Exposure, post_x: Exposure, lim: Limits) -> Check:
    if post_x.nav is None or post_x.nav <= 0:
        return Check(
            "exposure.net", UNKNOWN, "net exposure cannot be measured (NAV unknown or not positive)", "book"
        )
    post = post_x.net / post_x.nav
    pre = pre_x.net / pre_x.nav if pre_x.nav else None
    if lim.min_net - 1e-12 <= post <= lim.max_net + 1e-12:
        return Check("exposure.net", PASS, f"net exposure {_pct(post)} within {_pct(lim.min_net)} to {_pct(lim.max_net)}",
                     "book", post, lim.max_net if post >= 0 else lim.min_net)  # fmt: skip
    limit = lim.max_net if post > lim.max_net else lim.min_net
    if pre is not None and abs(post - limit) < abs(pre - limit) and (post - limit) * (pre - limit) > 0:
        return Check("exposure.net", WARN, f"net exposure {_pct(post)} is outside the limit, but this trade moves it "
                     f"back from {_pct(pre)}", "book", post, limit)  # fmt: skip
    return Check("exposure.net", FAIL, f"net exposure {_pct(post)} is outside {_pct(lim.min_net)} to {_pct(lim.max_net)}",
                 "book", post, limit)  # fmt: skip


def _drawdown_check(book: Book, x: Exposure, lim: Limits, adds_gross: bool) -> Check:
    d = drawdown(book.nav_history, x.nav)
    if d is None:
        return Check(
            "drawdown", UNKNOWN, "drawdown cannot be measured (NAV unknown)", "book", None, lim.drawdown_halt
        )
    if d >= lim.drawdown_halt:
        if adds_gross:
            return Check("drawdown", FAIL, f"drawdown {_pct(d)} has reached the {_pct(lim.drawdown_halt)} halt: only "
                         "trades that reduce exposure are allowed", "book", d, lim.drawdown_halt)  # fmt: skip
        return Check("drawdown", WARN, f"drawdown {_pct(d)} is past the halt, and this trade reduces exposure",
                     "book", d, lim.drawdown_halt)  # fmt: skip
    if d >= lim.drawdown_warn:
        return Check("drawdown", WARN, f"drawdown {_pct(d)} is past the {_pct(lim.drawdown_warn)} warning level",
                     "book", d, lim.drawdown_warn)  # fmt: skip
    return Check("drawdown", PASS, f"drawdown {_pct(d)}", "book", d, lim.drawdown_warn)


# --------------------------------------------------------------------------------------------------------------
# The book as it stands
# --------------------------------------------------------------------------------------------------------------
def assess(book: Book, lim: Limits, today: date) -> Report:
    """Every limit applied to the current book, with no trade. Anything not ``pass`` or ``warn`` is a breach."""
    x = exposure(book)

    def positions() -> list[Check]:
        out = []
        for h in book.holdings:
            if h.quantity == 0:
                continue
            if h.mark.price is None:
                out.append(
                    Check("position.size", UNKNOWN, f"{h.symbol}: no price", f"{h.symbol}/{h.strategy}")
                )
                continue
            w = x.weight(h.quantity * h.mark.price)
            out.append(
                _cap("position.size", f"{h.symbol}/{h.strategy}", None, w, lim.max_position, "position")
            )
        return out or [Check("position.size", PASS, "no positions", "book")]

    def sectors() -> list[Check]:
        out = []
        for s, v in sorted(x.by_sector.items()):
            if not s:
                out.append(Check("concentration.sector", UNKNOWN, "some positions have no sector", "unknown"))
                continue
            out.append(_cap("concentration.sector", s, None, x.weight(v), lim.max_sector, f"sector {s}"))
        return out or [Check("concentration.sector", PASS, "no positions", "book")]

    def strategies() -> list[Check]:
        out = []
        for s, v in sorted(x.by_strategy.items()):
            cap = lim.max_strategy.get(s)
            if cap is None:
                out.append(Check("concentration.strategy", FAIL, f"unknown strategy {s!r}", s))
                continue
            out.append(_cap("concentration.strategy", s, None, x.weight(v), cap, f"strategy {s} sleeve"))
        return out or [Check("concentration.strategy", PASS, "no positions", "book")]

    def liquidity() -> list[Check]:
        out = []
        for h in book.holdings:
            if h.quantity == 0:
                continue
            adv = h.mark.adv_shares
            if adv is None or adv <= 0:
                out.append(
                    Check(
                        "liquidity.position", UNKNOWN, f"{h.symbol}: average daily volume unknown", h.symbol
                    )
                )
                continue
            share = abs(h.quantity) / adv
            out.append(_cap("liquidity.position", h.symbol, None, share, lim.max_position_adv,
                            f"{h.symbol} position is"))  # fmt: skip
        return out or [Check("liquidity.position", PASS, "no positions", "book")]

    def gross() -> list[Check]:
        return [_cap("exposure.gross", "book", None, x.weight(x.gross), lim.max_gross, "gross exposure")]

    def net() -> list[Check]:
        return [_net_check(x, x, lim)]

    def cash() -> list[Check]:
        st = PASS if book.cash >= -1e-6 else FAIL
        return [Check("cash.no_borrowing", st, f"cash {book.cash:,.2f}", "book", book.cash, 0.0)]

    def dd() -> list[Check]:
        return [_drawdown_check(book, x, lim, adds_gross=False)]

    checks: list[Check] = []
    for name, fn in (
        ("data.book_prices", lambda: _book_price_checks(book, lim, today)), ("position.size", positions),
        ("concentration.sector", sectors), ("concentration.strategy", strategies),
        ("liquidity.position", liquidity), ("exposure.gross", gross), ("exposure.net", net),
        ("cash.no_borrowing", cash), ("drawdown", dd),
    ):  # fmt: skip
        checks += _safely(name, fn)
    # At the halt level the book itself is in breach, not only new trades.
    d = drawdown(book.nav_history, x.nav)
    if d is not None and d >= lim.drawdown_halt:
        checks = [c if c.name != "drawdown" else replace(c, status=FAIL, detail=f"drawdown {_pct(d)} has reached the "
                  f"{_pct(lim.drawdown_halt)} halt: new risk is blocked") for c in checks]  # fmt: skip
    figures = {"exposure": x.as_dict(), "drawdown": d}
    return Report(tuple(checks), BOOK_CHECKS, lim.version, figures)
