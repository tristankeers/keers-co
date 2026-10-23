"""Strategy A, step 1: a screen for signs of financial distress, from live filings and prices.

Deterministic. Each check returns a value, the thresholds it was judged against, a level and a sentence
saying why. Levels: ``severe``, ``watch``, ``clear`` or ``unknown``. An unknown check never counts as clear:
a company with an unknown check and no severe flag is ``incomplete``, never ``no flags``.

Checks:

1. Interest cover: operating income (EBIT, TTM) / interest expense (TTM).
2. Net leverage: net debt / EBITDA.
3. Debt due within a year against cash and short-term investments.
4. Altman Z-score. Manufacturers (SIC 2000-3999) use the original 1968 model with market equity; everyone
   else uses Altman's 1995 Z'' model for non-manufacturers, which leaves out sales / assets.
5. Share price against its 52-week high (adjusted closes).

The Baa corporate bond spread over Treasuries (FRED BAA10Y) is reported as market backdrop. It is the same for
every company, so it does not change any company's level. Company-specific bond prices are not available in the
data layer yet.

This is a screen for research, not a verdict: it lists companies worth a closer look.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from keers.data import fundamentals as F
from keers.data import snapshot
from keers.data.models import Value, missing
from keers.data.store import Store

SCREEN_VERSION = "distress-v1"

# Thresholds. A value beyond ``severe`` is a severe flag; beyond ``watch`` a watch flag.
COVER_SEVERE, COVER_WATCH = 1.0, 2.0  # EBIT / interest, times
LEVERAGE_SEVERE, LEVERAGE_WATCH = 6.0, 4.0  # net debt / EBITDA, times
DUE_SEVERE, DUE_WATCH = 1.0, 0.5  # debt due within a year / cash and short-term investments
Z_ORIGINAL = (1.81, 2.99)  # distress below the first, grey zone up to the second
Z_DOUBLE_PRIME = (1.10, 2.60)
DRAWDOWN_SEVERE, DRAWDOWN_WATCH = -0.60, -0.40  # price / 52-week high - 1
DRAWDOWN_MIN_DAYS = 200  # trading days of history needed for a 52-week high
SPREAD_WATCH = 3.0  # Baa - 10y Treasury, percentage points
SPREAD_WIDENING_WATCH = 0.75  # rise over three months, percentage points

LEVELS = ("severe", "watch", "clear", "unknown")


@dataclass
class Check:
    name: str
    level: str
    value: float | None
    reason: str
    thresholds: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "level": self.level, "value": self.value, "reason": self.reason,
            "thresholds": self.thresholds, "inputs": self.inputs,
        }  # fmt: skip


def _v(x: Value | None) -> float | None:
    return x.value if x is not None and x.known else None


def _inputs(**vals: Value | None) -> dict[str, Any]:
    return {k: (v.as_dict() if v is not None else None) for k, v in vals.items()}


def _level(x: float, severe: float, watch: float, below: bool) -> str:
    """Level for x: with ``below``, low values are bad (x < severe is severe); otherwise high values are bad."""
    if below:
        return "severe" if x < severe else "watch" if x < watch else "clear"
    return "severe" if x > severe else "watch" if x > watch else "clear"


def interest_cover(ebit: Value, interest: Value, debt: Value) -> Check:
    t = f"severe below {COVER_SEVERE:g}x, watch below {COVER_WATCH:g}x"
    inp = _inputs(ebit=ebit, interestExpense=interest, debt=debt)
    e, i, d = _v(ebit), _v(interest), _v(debt)
    if e is None:
        return Check("interestCover", "unknown", None, f"operating income unknown: {ebit.method}", t, inp)
    if i is None or i <= 0:
        if d is not None and d <= 0:
            return Check("interestCover", "clear", None, "no debt and no interest expense", t, inp)
        return Check("interestCover", "unknown", None, f"interest expense unknown: {interest.method}", t, inp)
    cover = e / i
    lvl = _level(cover, COVER_SEVERE, COVER_WATCH, below=True)
    return Check("interestCover", lvl, cover,
                 f"operating income covers interest {cover:.1f} times ({e:,.0f}m / {i:,.0f}m)", t, inp)  # fmt: skip


def net_leverage(net_debt: Value, ebitda: Value) -> Check:
    t = f"severe above {LEVERAGE_SEVERE:g}x, watch above {LEVERAGE_WATCH:g}x; severe if EBITDA is not positive with net debt"
    inp = _inputs(netDebt=net_debt, ebitda=ebitda)
    nd, eb = _v(net_debt), _v(ebitda)
    if nd is None:
        return Check("netLeverage", "unknown", None, f"net debt unknown: {net_debt.method}", t, inp)
    if eb is None:
        return Check("netLeverage", "unknown", None, f"EBITDA unknown: {ebitda.method}", t, inp)
    if nd <= 0:
        return Check("netLeverage", "clear", nd / eb if eb > 0 else None,
                     f"net cash of {-nd:,.0f}m", t, inp)  # fmt: skip
    if eb <= 0:
        return Check(
            "netLeverage", "severe", None, f"net debt of {nd:,.0f}m against EBITDA of {eb:,.0f}m", t, inp
        )
    x = nd / eb
    return Check("netLeverage", _level(x, LEVERAGE_SEVERE, LEVERAGE_WATCH, below=False), x,
                 f"net debt is {x:.1f}x EBITDA ({nd:,.0f}m / {eb:,.0f}m)", t, inp)  # fmt: skip


def debt_due(due: Value, liquid: Value, revolver: Value | None = None) -> Check:
    """Debt due within a year against cash, short-term investments and undrawn committed credit lines."""
    t = (f"severe when debt due exceeds cash, short-term investments and undrawn credit lines; watch above "
         f"{DUE_WATCH:.0%} of them")  # fmt: skip
    inp = _inputs(debtDueWithinYear=due, liquidAssets=liquid, undrawnCreditLines=revolver)
    d, c = _v(due), _v(liquid)
    r = _v(revolver) or 0.0
    if d is None:
        return Check("debtDue", "unknown", None, f"debt due within a year unknown: {due.method}", t, inp)
    if d <= 0:
        return Check("debtDue", "clear", 0.0, "no debt due within a year", t, inp)
    if c is None:
        return Check("debtDue", "unknown", None, f"cash unknown: {liquid.method}", t, inp)
    room = c + r
    lines = f" and {r:,.0f}m of undrawn credit lines" if r else " (no undrawn credit lines filed)"
    if room <= 0:
        return Check("debtDue", "severe", None, f"{d:,.0f}m due within a year and no cash or credit lines reported",
                     t, inp)  # fmt: skip
    x = d / room
    return Check("debtDue", _level(x, DUE_SEVERE, DUE_WATCH, below=False), x,
                 f"{d:,.0f}m due within a year against {c:,.0f}m of cash and short-term investments{lines} ({x:.0%})",
                 t, inp)  # fmt: skip


def is_manufacturer(sic: str | None) -> bool:
    return sic is not None and sic.isdigit() and 2000 <= int(sic) <= 3999


def altman_z(
    sic: str | None, assets: Value, current_assets: Value, current_liabilities: Value, retained: Value,
    ebit: Value, liabilities: Value, equity_book: Value, market_cap: Value, revenue: Value,
) -> Check:  # fmt: skip
    inp = _inputs(assets=assets, currentAssets=current_assets, currentLiabilities=current_liabilities,
                  retainedEarnings=retained, ebit=ebit, liabilities=liabilities, bookEquity=equity_book,
                  marketCap=market_cap, revenue=revenue)  # fmt: skip
    manu = is_manufacturer(sic)
    lo, hi = Z_ORIGINAL if manu else Z_DOUBLE_PRIME
    model = "original Z (1968, manufacturers)" if manu else "Z'' (1995, non-manufacturers)"
    t = f"{model}: severe below {lo}, watch below {hi}"
    need = {"total assets": assets, "current assets": current_assets, "current liabilities": current_liabilities,
            "retained earnings": retained, "operating income": ebit, "total liabilities": liabilities}  # fmt: skip
    need |= {"market cap": market_cap, "revenue": revenue} if manu else {"book equity": equity_book}
    gaps = [k for k, v in need.items() if _v(v) is None]
    if gaps:
        return Check("altmanZ", "unknown", None, f"{model} needs {', '.join(gaps)}", t, inp)
    ta, tl = _v(assets), _v(liabilities)
    assert ta is not None and tl is not None  # noqa: S101
    if ta <= 0 or tl <= 0:
        return Check("altmanZ", "unknown", None, "total assets or liabilities not positive", t, inp)
    x1 = ((_v(current_assets) or 0) - (_v(current_liabilities) or 0)) / ta
    x2 = (_v(retained) or 0) / ta
    x3 = (_v(ebit) or 0) / ta
    if manu:
        x4 = (_v(market_cap) or 0) / tl
        x5 = (_v(revenue) or 0) / ta
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    else:
        x4 = (_v(equity_book) or 0) / tl
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    lvl = _level(z, lo, hi, below=True)
    zone = {"severe": "distress zone", "watch": "grey zone", "clear": "safe zone"}[lvl]
    return Check("altmanZ", lvl, z, f"{model} of {z:.2f}, in the {zone}", t, inp)


def drawdown(store: Store, ticker: str, as_of: date) -> Check:
    t = f"severe below {DRAWDOWN_SEVERE:.0%}, watch below {DRAWDOWN_WATCH:.0%} of the 52-week high"
    rows = [
        (d, a) for d, a in store.adjusted_closes(ticker, as_of - timedelta(days=365), as_of) if a is not None
    ]
    if len(rows) < DRAWDOWN_MIN_DAYS:
        return Check("drawdown", "unknown", None,
                     f"{len(rows)} days of adjusted prices in the last year, needs {DRAWDOWN_MIN_DAYS}", t)  # fmt: skip
    hi_day, hi = max(rows, key=lambda r: r[1])
    last_day, last = rows[-1]
    x = last / hi - 1
    inp = {"high": hi, "highDay": hi_day.isoformat(), "last": last, "lastDay": last_day.isoformat()}
    return Check("drawdown", _level(x, DRAWDOWN_SEVERE, DRAWDOWN_WATCH, below=True), x,
                 f"{x:.0%} from the 52-week high on {hi_day}", t, inp)  # fmt: skip


def credit_backdrop(store: Store, as_of: date) -> dict[str, Any]:
    now = store.last_rate("BAA10Y", as_of)
    then = store.last_rate("BAA10Y", as_of - timedelta(days=91))
    if now is None:
        return {"level": "unknown", "reason": "Baa spread (FRED BAA10Y) not in the store"}
    out: dict[str, Any] = {"spread": now.value, "day": now.day.isoformat()}
    flags = []
    if now.value > SPREAD_WATCH:
        flags.append(f"spread {now.value:.2f}pp is above {SPREAD_WATCH}pp")
    if then is not None:
        out |= {"spread3mAgo": then.value, "day3mAgo": then.day.isoformat(), "change": now.value - then.value}
        if now.value - then.value > SPREAD_WIDENING_WATCH:
            flags.append(f"spread widened {now.value - then.value:.2f}pp in three months")
    out["level"] = "watch" if flags else "clear"
    out["reason"] = "; ".join(flags) or f"Baa spread {now.value:.2f}pp on {now.day}"
    return out


def overall(checks: Sequence[Check]) -> str:
    n = {lvl: sum(1 for c in checks if c.level == lvl) for lvl in LEVELS}
    if n["severe"] >= 2:
        return "distressed"
    if n["severe"] == 1 or n["watch"] >= 2:
        return "stressed"
    if n["unknown"]:
        return "incomplete"
    return "watch" if n["watch"] else "no flags"


def _liabilities(fu: F.Fundamentals, at: date) -> Value:
    direct = fu.balance(F.LIABILITIES, "total liabilities", at)
    if direct.known:
        return direct
    # Many companies file only "liabilities and equity"; subtract total equity (including minority interests).
    le = fu.balance(F.LIABILITIES_AND_EQUITY, "liabilities and equity", at)
    eq = fu.balance(F.EQUITY_INCL_NCI + F.EQUITY, "total equity", at)
    if le.known and eq.known:
        return F._sum([le, Value(-(eq.value or 0), eq.kind, eq.unit, eq.method, eq.period_end, eq.sources)],
                      "liabilities and equity − total equity")  # fmt: skip
    return direct


def _debt_due(fu: F.Fundamentals, at: date) -> Value:
    sched = fu.balance(F.DEBT_DUE_12M, "debt maturing within twelve months", at)
    if sched.known:
        return sched  # a maturity schedule covers every borrowing, company-specific lines included
    own, problem = fu.company_lines(at, ("debt-current",))
    if problem:
        return missing("USDm", f"debt due within a year: {problem}")
    cur = fu.balance(F.DEBT_CURRENT_ALL, "current debt", at)
    cur_dl = fu.balance(F.DEBT_AND_LEASES_CURRENT, "current debt and finance leases", at)
    if cur.known or cur_dl.known:
        base = [cur if cur.known else cur_dl]
    else:
        base = [fu.balance(F.DEBT_CURRENT, "current long-term debt", at),
                fu.balance(F.ST_BORROWINGS, "short-term borrowings", at)]  # fmt: skip
        if not base[1].known:  # short-term borrowings, where filed, already include drawn credit lines
            base.append(fu.balance(F.LINES_OF_CREDIT_CURRENT, "current credit facility borrowings", at))
        base.append(fu.balance(F.CONVERTIBLE_CURRENT, "current convertible notes", at))
    known = [p for p in base if p.known]
    if known:
        known += own
        if len(known) == 1:
            return known[0]
        return F._sum(known, " + ".join(F._name(p) for p in known))
    if own:
        return F._sum(own, " + ".join(F._name(p) for p in own))
    if not fu.ever_filed(F.DEBT_CONCEPTS):
        return Value(
            0.0, sched.kind, "USDm", "no debt ever filed", at, (), ("no debt ever filed; taken as zero",)
        )
    return sched


def screen(store: Store, ticker: str, as_of: date) -> dict[str, Any]:
    """Run every check for one company. Stale or missing core data blocks the result rather than clearing it."""
    snap = snapshot.build(store, ticker, as_of)
    v = snap.values
    stale = [b for b in snap.blocked_by if b.startswith(("stale", f"{ticker}: company not"))]
    base = {
        "ticker": snap.ticker,
        "name": snap.name,
        "asOf": as_of.isoformat(),
        "screenVersion": SCREEN_VERSION,
    }
    if stale or "revenue" not in v or not v["revenue"].known:
        return base | {"status": "blocked", "blockedBy": stale or snap.blocked_by, "checks": []}
    at = v["revenue"].period_end
    assert at is not None  # noqa: S101
    co = store.company(ticker) or {}
    fu = F.Fundamentals(store, snap.cik, as_of)
    checks = [
        interest_cover(v["ebit"], v["interestExpense"], v["debt"]),
        net_leverage(v["netDebt"], v["ebitda"]),
        debt_due(_debt_due(fu, at), v["liquidAssets"], fu.revolver_available(at)),
        altman_z(co.get("sic"), fu.balance(F.ASSETS, "total assets", at),
                 fu.balance(F.ASSETS_CURRENT, "current assets", at),
                 fu.balance(F.LIABILITIES_CURRENT, "current liabilities", at),
                 fu.balance(F.RETAINED_EARNINGS, "retained earnings", at), v["ebit"], _liabilities(fu, at),
                 fu.balance(F.EQUITY, "shareholders' equity", at), v["marketCap"], v["revenue"]),
        drawdown(store, ticker, as_of),
    ]  # fmt: skip
    return base | {
        "status": overall(checks),
        "periodEnd": at.isoformat(),
        "sic": co.get("sic"),
        "checks": [c.as_dict() for c in checks],
        "dataWarnings": snap.blocked_by,
    }


def run(store: Store, tickers: Sequence[str], as_of: date) -> dict[str, Any]:
    order = {"distressed": 0, "stressed": 1, "watch": 2, "incomplete": 3, "no flags": 4, "blocked": 5}
    rows = sorted((screen(store, t, as_of) for t in tickers), key=lambda r: (order[r["status"]], r["ticker"]))
    return {"asOf": as_of.isoformat(), "screenVersion": SCREEN_VERSION, "credit": credit_backdrop(store, as_of),
            "companies": rows}  # fmt: skip
