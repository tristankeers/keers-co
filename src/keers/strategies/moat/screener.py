"""Strategy B, step 1: a screen for durable-moat compounders, from filed annual accounts (``moat-v1``).

Deterministic. The screen reads up to ``WINDOW_YEARS`` fiscal years as the market knew them on the as-of date (only
filings made by then count), works out each year's figures with the formulas below, and grades seven checks.
Each check returns a value, the thresholds it was judged against, a level and a sentence saying why. Levels:
``pass``, ``watch``, ``fail`` or ``unknown``. An unknown check never counts as a pass.

Each fiscal year:

* NOPAT = operating income × (1 − tax rate). The tax rate is the year's effective rate (income tax ÷ pre-tax
  income) when pre-tax income is positive and the rate is between 0% and 50%; otherwise the statutory rate is
  used and labelled an assumption (US: 35% for years ending before 2018, 21% after; IFRS filers: 25%).
* Invested capital (operating approach) = total assets − cash and short-term investments − non-interest-bearing
  current liabilities (current liabilities − current borrowings). Goodwill stays in, so acquisitions are charged.
  This side of the balance sheet is used because buybacks can push book equity below zero.
* ROIC = NOPAT ÷ the average of invested capital at the start and end of the year (the end alone, flagged, when no
  balance a year earlier is filed). Not computed when invested capital is not positive.
* Free cash flow = cash from operations − capital expenditure.

Checks:

1. ``roic``: median ROIC over the window.
2. ``roicConsistency``: share of years with ROIC at or above ``ROIC_FLOOR``.
3. ``marginStability``: standard deviation of the operating margin; the latest year well below the median is
   at least a watch (margins eroding).
4. ``growth``: revenue growth per year (CAGR) from the first to the last year of the window.
5. ``cashConversion``: total free cash flow ÷ total NOPAT over the window.
6. ``balanceSheet``: net debt ÷ EBITDA on the latest TTM accounts (as the valuation snapshot builds them).
7. ``dilution``: growth per year in the diluted weighted average share count.

Status: ``compounder`` (every check passes), ``candidate`` (no fail, no unknown, one or two watches), ``weak``
(three or more watches), ``fails`` (any fail), ``incomplete`` (no fail, at least one unknown), ``insufficient
history`` (fewer than ``MIN_YEARS`` fiscal years), ``not applicable`` (banks, insurers and other financials,
SIC 6000-6799: their operating income and invested capital mean something else), ``blocked`` (stale or missing
core data).

The screen judges the business, not the price. The price is shown beside it (earnings yield and free cash flow
yield), ungraded; the margin of safety is the valuation's and the investment committee's job. This is a screen for
research, not a verdict.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from keers.data import fundamentals as F
from keers.data import snapshot
from keers.data.models import Kind, Value, missing
from keers.data.store import Store

SCREEN_VERSION = "moat-v1"

WINDOW_YEARS = 10  # fiscal years read, newest first
MIN_YEARS = 5  # years a multi-year check needs

# Thresholds. ``pass`` at or beyond the first, ``watch`` at or beyond the second, else ``fail``.
ROIC_PASS, ROIC_WATCH = 0.15, 0.10  # median ROIC
ROIC_FLOOR = 0.10  # a year "earns its keep" at or above this ROIC
CONSISTENCY_PASS, CONSISTENCY_WATCH = 0.80, 0.60  # share of years at or above the floor
MARGIN_SD_PASS, MARGIN_SD_WATCH = 0.03, 0.06  # standard deviation of the operating margin (3pp, 6pp)
MARGIN_EROSION = 0.05  # latest margin this far below the median: at least a watch
GROWTH_PASS, GROWTH_WATCH = 0.05, 0.0  # revenue CAGR
CONVERSION_PASS, CONVERSION_WATCH = 0.70, 0.40  # total FCF / total NOPAT
LEVERAGE_PASS, LEVERAGE_WATCH = 2.0, 3.0  # net debt / EBITDA (lower is better)
DILUTION_PASS, DILUTION_WATCH = 0.01, 0.03  # diluted share count CAGR (lower is better)

MIN_SHARES_M = 0.01  # fewer than 10,000 diluted shares: the filing used the wrong unit
SHARE_OUTLIER = 20.0  # a year's share count this many times off the others' median is a filing error
MAX_EFFECTIVE_TAX = 0.50
STATUTORY_US_BEFORE_2018, STATUTORY_US, STATUTORY_IFRS = 0.35, 0.21, 0.25
US_RATE_CHANGE = date(2018, 1, 1)

LEVELS = ("pass", "watch", "fail", "unknown")
STATUSES = ("compounder", "candidate", "incomplete", "weak", "fails", "insufficient history", "not applicable",
            "blocked")  # fmt: skip
PASSING = ("compounder", "candidate")  # statuses that let a Strategy B trade go to the committee


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


@dataclass
class Year:
    """One fiscal year's figures. Money in the reporting currency, millions; ratios as fractions."""

    end: date
    revenue: float | None = None
    ebit: float | None = None
    tax_rate: float | None = None
    tax_basis: str = ""  # "effective" or "statutory (assumption)"
    nopat: float | None = None
    invested_capital: float | None = None
    roic: float | None = None
    operating_margin: float | None = None
    gross_margin: float | None = None
    fcf: float | None = None
    diluted_shares: float | None = None  # millions
    notes: list[str] = field(default_factory=list)
    inputs: dict[str, Value] = field(default_factory=dict)

    def as_dict(self, detail: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "end": self.end.isoformat(), "revenue": self.revenue, "ebit": self.ebit, "taxRate": self.tax_rate,
            "taxBasis": self.tax_basis, "nopat": self.nopat, "investedCapital": self.invested_capital,
            "roic": self.roic, "operatingMargin": self.operating_margin, "grossMargin": self.gross_margin,
            "fcf": self.fcf, "dilutedShares": self.diluted_shares, "notes": self.notes,
        }  # fmt: skip
        if detail:
            out["inputs"] = {k: v.as_dict() for k, v in self.inputs.items()}
        return out


def _v(x: Value | None) -> float | None:
    return x.value if x is not None and x.known else None


def is_financial(sic: str | None) -> bool:
    s = str(sic or "").strip()
    return s.isdigit() and 6000 <= int(s) <= 6799


def statutory_rate(taxonomy: str, end: date) -> float:
    if taxonomy == "ifrs-full":
        return STATUTORY_IFRS
    return STATUTORY_US_BEFORE_2018 if end < US_RATE_CHANGE else STATUTORY_US


def current_borrowings(fu: F.Fundamentals, at: date) -> Value:
    """Borrowings due within a year shown in current liabilities. Taken as zero, with a note, when none is filed."""
    for group, label in ((F.DEBT_CURRENT_ALL, "current debt"), (F.DEBT_AND_LEASES_CURRENT, "current debt and leases"),
                         (F.IFRS_BORROWINGS_CURRENT, "current borrowings")):  # fmt: skip
        v = fu.balance(group, label, at)
        if v.known:
            return v
    parts = [fu.balance(c, "current borrowings", at)
             for c in (F.DEBT_CURRENT, F.ST_BORROWINGS, F.LINES_OF_CREDIT_CURRENT, F.CONVERTIBLE_CURRENT)]  # fmt: skip
    known = [p for p in parts if p.known]
    if known:
        return F._sum(known, " + ".join(F._name(p) for p in known))
    return Value(0.0, Kind.DERIVED, fu.money, "no current borrowings filed at this date; taken as zero", at, (),
                 ("no current borrowings filed; taken as zero, so all current liabilities count as operating",))  # fmt: skip


def invested_capital(fu: F.Fundamentals, at: date) -> Value:
    """Total assets − cash and short-term investments − (current liabilities − current borrowings), at ``at``."""
    assets = fu.balance(F.ASSETS, "total assets", at)
    cl = fu.balance(F.LIABILITIES_CURRENT, "current liabilities", at)
    liquid = fu.liquid_assets(at)
    for part in (assets, cl, liquid):
        if not part.known:
            return missing(fu.money, f"invested capital at {at}: {part.method}")
    cd = current_borrowings(fu, at)
    value = (_v(assets) or 0) - (_v(liquid) or 0) - ((_v(cl) or 0) - (_v(cd) or 0))
    parts = [assets, liquid, cl, cd]
    return Value(value, Kind.DERIVED, fu.money,
                 "total assets − cash and short-term investments − (current liabilities − current borrowings)", at,
                 tuple(s for p in parts for s in p.sources), tuple(w for p in parts for w in p.warnings))  # fmt: skip


def year_figures(fu: F.Fundamentals, end: date, prior_end: date | None) -> Year:
    y = Year(end)
    rev = fu.flow(F.REVENUE, "revenue", end)
    ebit = fu.operating_income(end)
    pretax = fu.flow(F.PRETAX_INCOME, "pre-tax income", end)
    tax = fu.flow(F.TAX_EXPENSE, "income tax expense", end)
    cfo = fu.flow(F.OPERATING_CASH_FLOW, "cash from operations", end)
    capex = fu.capex(end)
    gross = fu.flow(F.GROSS_PROFIT, "gross profit", end)
    shares = fu.annual(F.SHARES_DILUTED, "diluted weighted average shares", end, unit="m shares")
    ic_end = invested_capital(fu, end)
    ic_start = (
        invested_capital(fu, prior_end) if prior_end is not None else missing(fu.money, "no prior year")
    )
    y.inputs = {"revenue": rev, "operatingIncome": ebit, "pretaxIncome": pretax, "incomeTax": tax,
                "operatingCashFlow": cfo, "capex": capex, "grossProfit": gross, "dilutedShares": shares,
                "investedCapitalEnd": ic_end, "investedCapitalStart": ic_start}  # fmt: skip
    y.revenue, y.ebit = _v(rev), _v(ebit)
    y.diluted_shares = _v(shares)
    if y.revenue is not None and y.revenue > 0:
        if y.ebit is not None:
            y.operating_margin = y.ebit / y.revenue
        if _v(gross) is not None:
            y.gross_margin = (_v(gross) or 0) / y.revenue
    p, t = _v(pretax), _v(tax)
    if p is not None and t is not None and p > 0 and 0 <= t / p <= MAX_EFFECTIVE_TAX:
        y.tax_rate, y.tax_basis = t / p, "effective"
    else:
        y.tax_rate, y.tax_basis = statutory_rate(fu.taxonomy, end), "statutory (assumption)"
        why = ("pre-tax income or tax not filed" if p is None or t is None else
               "pre-tax income not positive" if p <= 0 else f"effective rate {t / p:.0%} outside 0-50%")  # fmt: skip
        y.notes.append(f"statutory tax rate {y.tax_rate:.0%} used: {why}")
    if y.ebit is not None:
        y.nopat = y.ebit * (1 - y.tax_rate)
    e, s = _v(ic_end), _v(ic_start)
    if e is not None:
        if s is not None:
            y.invested_capital = (e + s) / 2
        else:
            y.invested_capital = e
            y.notes.append("ROIC on end-of-year invested capital: no balance a year earlier")
    if y.nopat is not None and y.invested_capital is not None:
        if y.invested_capital > 0:
            y.roic = y.nopat / y.invested_capital
        else:
            y.notes.append("invested capital not positive: ROIC not meaningful")
    if _v(cfo) is not None and _v(capex) is not None:
        y.fcf = (_v(cfo) or 0) - (_v(capex) or 0)
    return y


def split_adjust(years: Sequence[Year], splits: Sequence[tuple[date, date, float]]) -> None:
    """Put every year's diluted share count on today's share basis.

    A count filed before a split, and never restated, is multiplied by the split ratio. A count under 10,000
    shares (a filing that gave millions as units) is left out, with a note. Then a count more than
    ``SHARE_OUTLIER`` times away from the median of the others (a scaling error in the filing) is dropped, with a
    note, rather than allowed to swing the dilution check.
    """
    for y in years:
        src = y.inputs.get("dilutedShares")
        if y.diluted_shares is None or src is None or not src.sources:
            continue
        filed = date.fromisoformat(str(src.sources[0]["filed"])[:10])
        factor = 1.0
        for before, _after, ratio in splits:
            if filed <= before:
                factor *= ratio
        if factor != 1.0:
            y.diluted_shares *= factor
            y.notes.append(
                f"diluted shares for fiscal {y.end.year} split-adjusted x{factor:g} (filed before a split)"
            )
    for y in years:
        if y.diluted_shares is not None and y.diluted_shares < MIN_SHARES_M:
            y.notes.append(f"diluted shares for fiscal {y.end.year} filed as {y.diluted_shares * 1e6:,.0f} shares, too "
                           "few for a listed company (probably filed in millions); left out")  # fmt: skip
            y.diluted_shares = None
    known = [y for y in years if y.diluted_shares is not None]
    if len(known) < 3:
        return
    drop = []
    for y in known:
        others = [o.diluted_shares for o in known if o is not y and o.diluted_shares is not None]
        med = statistics.median(others)
        assert y.diluted_shares is not None  # noqa: S101
        if med > 0 and not 1 / SHARE_OUTLIER <= y.diluted_shares / med <= SHARE_OUTLIER:
            drop.append(y)
    for y in drop:
        y.notes.append(f"diluted shares for fiscal {y.end.year} ({y.diluted_shares:,.3f}m) are far from the other "
                       "years' and were left out")  # fmt: skip
        y.diluted_shares = None


def history(fu: F.Fundamentals, as_of: date, window: int = WINDOW_YEARS) -> list[Year]:
    """The last ``window`` fiscal years known by ``as_of``, oldest first, share counts split-adjusted."""
    ends = [e for e in fu.fiscal_year_ends() if e <= as_of]
    keep = ends[-window:]
    out = []
    for e in keep:
        i = ends.index(e)
        prior = ends[i - 1] if i > 0 and 350 <= (e - ends[i - 1]).days <= 380 else None
        out.append(year_figures(fu, e, prior))
    split_adjust(out, fu.share_splits())
    return out


# --------------------------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------------------------
def _grade(x: float, good: float, ok: float, higher_is_better: bool = True) -> str:
    if higher_is_better:
        return "pass" if x >= good else "watch" if x >= ok else "fail"
    return "pass" if x <= good else "watch" if x <= ok else "fail"


def _known(years: Sequence[Year], attr: str) -> list[tuple[date, float]]:
    return [(y.end, v) for y in years if (v := getattr(y, attr)) is not None]


def _short(name: str, have: int, what: str, t: str) -> Check:
    return Check(
        name, "unknown", None, f"{what} known for {have} fiscal years; the check needs {MIN_YEARS}", t
    )


def roic_level(years: Sequence[Year]) -> Check:
    t = f"pass at a median of {ROIC_PASS:.0%} or more, watch from {ROIC_WATCH:.0%}"
    r = _known(years, "roic")
    if len(r) < MIN_YEARS:
        return _short("roic", len(r), "ROIC", t)
    m = statistics.median(v for _, v in r)
    return Check("roic", _grade(m, ROIC_PASS, ROIC_WATCH), m,
                 f"median ROIC {m:.1%} over {len(r)} fiscal years ({r[0][0].year}-{r[-1][0].year})", t)  # fmt: skip


def roic_consistency(years: Sequence[Year]) -> Check:
    t = (f"share of years with ROIC of {ROIC_FLOOR:.0%} or more: pass from {CONSISTENCY_PASS:.0%}, "
         f"watch from {CONSISTENCY_WATCH:.0%}")  # fmt: skip
    r = _known(years, "roic")
    if len(r) < MIN_YEARS:
        return _short("roicConsistency", len(r), "ROIC", t)
    good = sum(1 for _, v in r if v >= ROIC_FLOOR)
    share = good / len(r)
    worst_day, worst = min(r, key=lambda x: x[1])
    return Check("roicConsistency", _grade(share, CONSISTENCY_PASS, CONSISTENCY_WATCH), share,
                 f"ROIC of {ROIC_FLOOR:.0%} or more in {good} of {len(r)} years; lowest {worst:.1%} "
                 f"(fiscal {worst_day.year})", t)  # fmt: skip


def margin_stability(years: Sequence[Year]) -> Check:
    t = (f"standard deviation of the operating margin: pass up to {MARGIN_SD_PASS * 100:g}pp, watch up to "
         f"{MARGIN_SD_WATCH * 100:g}pp; latest year more than {MARGIN_EROSION * 100:g}pp below the median is at "
         f"least a watch")  # fmt: skip
    m = _known(years, "operating_margin")
    if len(m) < MIN_YEARS:
        return _short("marginStability", len(m), "operating margin", t)
    vals = [v for _, v in m]
    sd, med, last = statistics.pstdev(vals), statistics.median(vals), vals[-1]
    lvl = _grade(sd, MARGIN_SD_PASS, MARGIN_SD_WATCH, higher_is_better=False)
    reason = f"operating margin {med:.1%} median, standard deviation {sd * 100:.1f}pp over {len(m)} years"
    if last < med - MARGIN_EROSION:
        reason += f"; latest year {last:.1%} is well below the median"
        lvl = "watch" if lvl == "pass" else lvl
    return Check("marginStability", lvl, sd, reason, t)


def _cagr(first: tuple[date, float], last: tuple[date, float]) -> float | None:
    yrs = (last[0] - first[0]).days / 365.25
    if yrs <= 0 or first[1] <= 0 or last[1] <= 0:
        return None
    return float((last[1] / first[1]) ** (1 / yrs) - 1)


def growth(years: Sequence[Year]) -> Check:
    t = f"revenue growth per year: pass from {GROWTH_PASS:.0%}, watch from {GROWTH_WATCH:.0%}"
    r = _known(years, "revenue")
    if len(r) < MIN_YEARS:
        return _short("growth", len(r), "revenue", t)
    g = _cagr(r[0], r[-1])
    if g is None:
        return Check("growth", "unknown", None, "revenue not positive at the start or end of the window", t)
    return Check("growth", _grade(g, GROWTH_PASS, GROWTH_WATCH), g,
                 f"revenue grew {g:.1%} a year from fiscal {r[0][0].year} to {r[-1][0].year} "
                 f"({r[0][1]:,.0f}m to {r[-1][1]:,.0f}m)", t)  # fmt: skip


def cash_conversion(years: Sequence[Year]) -> Check:
    t = f"total free cash flow / total NOPAT: pass from {CONVERSION_PASS:.0%}, watch from {CONVERSION_WATCH:.0%}"
    both = [(y.fcf, y.nopat) for y in years if y.fcf is not None and y.nopat is not None]
    if len(both) < MIN_YEARS:
        return _short("cashConversion", len(both), "free cash flow and NOPAT", t)
    fcf, nopat = sum(a for a, _ in both), sum(b for _, b in both)
    if nopat <= 0:
        return Check("cashConversion", "fail", None,
                     f"no after-tax operating profit over {len(both)} years (NOPAT {nopat:,.0f}m)", t)  # fmt: skip
    x = fcf / nopat
    return Check("cashConversion", _grade(x, CONVERSION_PASS, CONVERSION_WATCH), x,
                 f"free cash flow of {fcf:,.0f}m against NOPAT of {nopat:,.0f}m over {len(both)} years ({x:.0%})",
                 t)  # fmt: skip


def balance_sheet(net_debt: Value, ebitda: Value) -> Check:
    t = (f"net debt / EBITDA: pass up to {LEVERAGE_PASS:g}x, watch up to {LEVERAGE_WATCH:g}x; net cash passes; "
         "net debt with EBITDA not positive fails")  # fmt: skip
    inp = {"netDebt": net_debt.as_dict(), "ebitda": ebitda.as_dict()}
    nd, eb = _v(net_debt), _v(ebitda)
    if nd is None:
        return Check("balanceSheet", "unknown", None, f"net debt unknown: {net_debt.method}", t, inp)
    if nd <= 0:
        return Check(
            "balanceSheet", "pass", nd / eb if eb and eb > 0 else None, f"net cash of {-nd:,.0f}m", t, inp
        )
    if eb is None:
        return Check("balanceSheet", "unknown", None, f"EBITDA unknown: {ebitda.method}", t, inp)
    if eb <= 0:
        return Check(
            "balanceSheet", "fail", None, f"net debt of {nd:,.0f}m against EBITDA of {eb:,.0f}m", t, inp
        )
    x = nd / eb
    return Check("balanceSheet", _grade(x, LEVERAGE_PASS, LEVERAGE_WATCH, higher_is_better=False), x,
                 f"net debt is {x:.1f}x EBITDA ({nd:,.0f}m / {eb:,.0f}m)", t, inp)  # fmt: skip


def dilution(years: Sequence[Year]) -> Check:
    t = (f"growth per year in diluted shares: pass up to {DILUTION_PASS:.0%}, watch up to {DILUTION_WATCH:.0%} "
         "(buybacks show as negative)")  # fmt: skip
    s = _known(years, "diluted_shares")
    if len(s) < MIN_YEARS:
        return _short("dilution", len(s), "diluted share count", t)
    g = _cagr(s[0], s[-1])
    if g is None:
        return Check("dilution", "unknown", None, "share count not positive", t)
    return Check("dilution", _grade(g, DILUTION_PASS, DILUTION_WATCH, higher_is_better=False), g,
                 f"diluted shares changed {g:+.1%} a year ({s[0][1]:,.1f}m to {s[-1][1]:,.1f}m)", t)  # fmt: skip


def overall(checks: Sequence[Check]) -> str:
    n = {lvl: sum(1 for c in checks if c.level == lvl) for lvl in LEVELS}
    if n["fail"]:
        return "fails"
    if n["unknown"]:
        return "incomplete"
    if n["watch"] >= 3:
        return "weak"
    return "candidate" if n["watch"] else "compounder"


def _price(snap: snapshot.Snapshot, years: Sequence[Year]) -> dict[str, Any]:
    """Ungraded: what the business costs. Earnings yield on the TTM snapshot; FCF yield on the latest fiscal year."""
    v = snap.values
    ebit, ev, mcap = _v(v.get("ebit")), _v(v.get("ev")), _v(v.get("marketCap"))
    out: dict[str, Any] = {"earningsYield": None, "fcfYield": None}
    if ebit is not None and ev is not None and ev > 0:
        out["earningsYield"] = ebit / ev
        out["earningsYieldMethod"] = "operating income (TTM) / enterprise value"
    last = next((y for y in reversed(years) if y.fcf is not None), None)
    if last is not None and mcap is not None and mcap > 0 and last.fcf is not None:
        out["fcfYield"] = last.fcf / mcap
        out["fcfYieldMethod"] = f"free cash flow (fiscal {last.end.year}) / market capitalisation"
    return out


def screen(store: Store, ticker: str, as_of: date, detail: bool = True) -> dict[str, Any]:
    """Run every check for one company. Stale or missing core data blocks the result rather than passing it."""
    snap = snapshot.build(store, ticker, as_of)
    v = snap.values
    stale = [b for b in snap.blocked_by if b.startswith(("stale", f"{ticker}: company not"))]
    base: dict[str, Any] = {
        "ticker": snap.ticker, "name": snap.name, "asOf": as_of.isoformat(), "screenVersion": SCREEN_VERSION,
        "currency": snap.currency, "accounts": {"source": snap.source, "cadence": snap.cadence},
    }  # fmt: skip
    co = store.company(ticker) or {}
    if is_financial(co.get("sic")):  # before staleness: a bank is never graded, however fresh its accounts
        return base | {"sic": co.get("sic"), "status": "not applicable", "checks": [], "years": [],
                       "reason": f"financial company (SIC {co.get('sic')}): operating income and invested capital "
                                 "do not measure a bank's or insurer's returns"}  # fmt: skip
    if stale or "revenue" not in v or not v["revenue"].known:
        return base | {"status": "blocked", "blockedBy": stale or snap.blocked_by, "checks": [], "years": []}
    base["sic"] = co.get("sic")
    fu = F.Fundamentals(store, snap.cik, as_of)
    years = history(fu, as_of)
    base["years"] = [y.as_dict(detail) for y in years]
    if len(years) < MIN_YEARS:
        return base | {"status": "insufficient history", "checks": [],
                       "reason": f"{len(years)} fiscal years filed by {as_of}; the screen needs {MIN_YEARS}"}  # fmt: skip
    checks = [roic_level(years), roic_consistency(years), margin_stability(years), growth(years),
              cash_conversion(years), balance_sheet(v["netDebt"], v["ebitda"]), dilution(years)]  # fmt: skip
    roic = next(c.value for c in checks if c.name == "roic")
    return base | {
        "status": overall(checks), "periodEnd": years[-1].end.isoformat(),
        "window": f"fiscal {years[0].end.year}-{years[-1].end.year}", "medianRoic": roic,
        "checks": [c.as_dict() for c in checks], "price": _price(snap, years),
        "dataWarnings": snap.blocked_by + sorted({n for y in years for n in y.notes}),
    }  # fmt: skip


def run(store: Store, tickers: Sequence[str], as_of: date) -> dict[str, Any]:
    """Screen a list. Best first: by status, then by median ROIC."""
    order = {s: i for i, s in enumerate(STATUSES)}
    rows = [screen(store, t, as_of) for t in tickers]
    rows.sort(key=lambda r: (order[r["status"]], -(r.get("medianRoic") or -9e9), r["ticker"]))
    return {"asOf": as_of.isoformat(), "screenVersion": SCREEN_VERSION, "companies": rows}
