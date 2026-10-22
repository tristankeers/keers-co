"""A company snapshot as of a date: filed fundamentals, the share price and rates, all with provenance.

A snapshot can be *blocked*. Missing core figures, a stale price or stale fundamentals block it, and a blocked
snapshot is never passed to the valuation engine: a missing or failed check is never an approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from keers.valuation import kci_v1_defaults, kci_v2_defaults

from . import fundamentals as F
from .beta import beta
from .models import Kind, Value, missing
from .store import Store

PRICE_MAX_BUSINESS_DAYS = 3  # a price older than this many business days is stale
FUNDAMENTALS_MAX_DAYS = 135  # a quarter end plus the 10-Q deadline plus a full quarter
CORE = ("revenue", "ebitda", "ebit", "netIncome", "capex", "netDebt", "marketCap", "ev")


def business_days_between(a: date, b: date) -> int:
    """Weekdays after ``a`` up to and including ``b`` (market holidays are not modelled, so this errs strict)."""
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


@dataclass
class Snapshot:
    ticker: str
    cik: int
    name: str
    as_of: date
    values: dict[str, Value]
    blocked_by: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "cik": self.cik,
            "name": self.name,
            "asOf": self.as_of.isoformat(),
            "blocked": self.blocked,
            "blockedBy": self.blocked_by,
            "warnings": self.warnings,
            "values": {k: v.as_dict() for k, v in self.values.items()},
        }


def _derived(value: float, unit: str, method: str, parts: list[Value]) -> Value:
    ends = [p.period_end for p in parts if p.period_end]
    return Value(
        value, Kind.DERIVED, unit, method, max(ends) if ends else None,
        tuple(s for p in parts for s in p.sources), tuple(w for p in parts for w in p.warnings),
    )  # fmt: skip


def build(store: Store, ticker: str, as_of: date) -> Snapshot:
    co = store.company(ticker)
    if co is None:
        return Snapshot(
            ticker.upper(), 0, "", as_of, {}, [f"{ticker}: company not in the store; run refresh"]
        )
    fu = F.Fundamentals(store, co["cik"], as_of)
    v: dict[str, Value] = {}
    v["revenue"] = fu.flow(F.REVENUE, "revenue")
    at = v["revenue"].period_end
    if at is None:
        snap = Snapshot(co["ticker"], co["cik"], co["name"], as_of, v)
        check(snap)
        return snap
    # Every other figure must cover the same period as revenue (flows) or sit at its end (balances).
    v["ebit"] = fu.flow(F.OPERATING_INCOME, "operating income", at)
    v["da"] = fu.depreciation_amortisation(at)
    v["netIncome"] = fu.flow(F.NET_INCOME, "net income", at)
    v["capex"] = fu.flow(F.CAPEX, "capital expenditure", at)
    v["interestExpense"] = fu.flow(F.INTEREST, "interest expense", at)
    v["sbc"] = fu.flow(F.SBC, "share-based compensation", at)
    v["revenueGrowth"] = fu.flow_growth(F.REVENUE, "revenue")

    ebit, da = v["ebit"], v["da"]
    if ebit.known and da.known:
        assert ebit.value is not None and da.value is not None  # noqa: S101
        v["ebitda"] = _derived(ebit.value + da.value, "USDm", "operating income + D&A (TTM)", [ebit, da])
    else:
        v["ebitda"] = missing("USDm", f"EBITDA: {ebit.method if not ebit.known else da.method}")

    liquid = fu.liquid_assets(at)
    debt = fu.total_debt(at)
    v["liquidAssets"], v["debt"] = liquid, debt
    # Shown for comparison with data vendors that count leases as debt; not included in net debt.
    v["operatingLeases"] = fu.operating_leases(at)
    ys = fu.year_start(at)
    problem = fu.debt_check(debt, at) or (fu.debt_rollforward(at, ys) if ys else None)
    if problem:
        v["netDebt"] = missing("USDm", f"net debt: {problem}")
    elif liquid.known and debt.known:
        assert liquid.value is not None and debt.value is not None  # noqa: S101
        v["netDebt"] = _derived(
            debt.value - liquid.value, "USDm", "debt − cash − short-term investments", [debt, liquid]
        )
    else:
        v["netDebt"] = missing("USDm", f"net debt: {debt.method if not debt.known else liquid.method}")

    v["shares"] = fu.shares(at)
    px = store.last_price(ticker, as_of)
    if px is None:
        v["price"] = missing("USD", "no share price in the store")
    else:
        cite = {
            "source": px.source,
            "day": px.day.isoformat(),
            "close": px.close,
            "raw_sha256": px.raw_sha256,
        }
        v["price"] = Value(px.close, Kind.MARKET, "USD", f"{px.source} close", px.day, (cite,))
    if v["price"].known and v["shares"].known:
        assert v["price"].value is not None and v["shares"].value is not None  # noqa: S101
        v["marketCap"] = _derived(
            v["price"].value * v["shares"].value, "USDm", "price × shares", [v["price"], v["shares"]]
        )
    else:
        v["marketCap"] = missing("USDm", "market cap: price or shares missing")
    if v["marketCap"].known and v["netDebt"].known:
        assert v["marketCap"].value is not None and v["netDebt"].value is not None  # noqa: S101
        v["ev"] = _derived(
            v["marketCap"].value + v["netDebt"].value,
            "USDm",
            "market cap + net debt",
            [v["marketCap"], v["netDebt"]],
        )
    else:
        v["ev"] = missing("USDm", "enterprise value: market cap or net debt missing")

    v["beta"] = beta(store, ticker, as_of)

    for series in ("DGS10", "BAA10Y"):
        r = store.last_rate(series, as_of)
        v[f"rate_{series}"] = (
            missing("%", f"{series} not in the store")
            if r is None
            else Value(r.value, Kind.MARKET, "%", f"FRED {series}", r.day,
                       ({"source": "fred", "series": series, "day": r.day.isoformat(), "raw_sha256": r.raw_sha256},))
        )  # fmt: skip

    snap = Snapshot(co["ticker"], co["cik"], co["name"], as_of, v)
    check(snap)
    return snap


def check(snap: Snapshot) -> None:
    """Fill ``blocked_by`` and ``warnings``. Deterministic; every rule is listed here."""
    v = snap.values
    for k in CORE:
        if k not in v or not v[k].known:
            snap.blocked_by.append(f"missing {k}: {v[k].method if k in v else 'not computed'}")
    px = v.get("price")
    if px and px.known and px.period_end:
        age = business_days_between(px.period_end, snap.as_of)
        if age > PRICE_MAX_BUSINESS_DAYS:
            snap.blocked_by.append(
                f"stale price: last close {px.period_end} is {age} business days before {snap.as_of}"
            )
    rev = v.get("revenue")
    if rev and rev.known and rev.period_end:
        age = (snap.as_of - rev.period_end).days
        if age > FUNDAMENTALS_MAX_DAYS:
            snap.blocked_by.append(
                f"stale fundamentals: latest period ends {rev.period_end}, {age} days before {snap.as_of}"
            )
    ends = {k: v[k].period_end for k in ("revenue", "ebit", "netIncome", "capex") if k in v and v[k].known}
    if len(set(ends.values())) > 1:
        snap.warnings.append(f"income figures cover different periods: {ends}")
    for k, val in v.items():
        snap.warnings += [f"{k}: {w}" for w in val.warnings if f"{k}: {w}" not in snap.warnings]


def _pct(v: float | None) -> float | None:
    return v / 100 if v is not None else None


def to_kci_deal(snap: Snapshot) -> dict[str, Any]:
    """Map a snapshot to the deal record KCI's ``analyse`` reads (USD millions)."""

    def val(k: str) -> float | None:
        x = snap.values.get(k)
        return x.value if x is not None and x.known else None

    return {
        "id": f"us-{snap.ticker.lower()}",
        "name": snap.name,
        "ticker": snap.ticker,
        "currency": "USD",
        "revenue": val("revenue"),
        "ebitda": val("ebitda"),
        "ebit": val("ebit"),
        "netIncome": val("netIncome"),
        "capex": val("capex"),
        "ev": val("ev"),
        "netDebt": val("netDebt"),
        "revenueGrowth": val("revenueGrowth"),
        "beta": val("beta"),
        # FRED quotes percent; the engine takes decimals.
        "riskFree": _pct(val("rate_DGS10")),
        # Live filings are not independently cross-checked yet, so they are never labelled "verified".
        "dataStatus": "filed",
        "reportingPeriod": f"TTM to {snap.values['revenue'].period_end}"
        if val("revenue") is not None
        else None,
        "market": {
            "price": val("price"),
            "priceDate": str(snap.values["price"].period_end) if val("price") is not None else None,
            "sharesM": val("shares"),
            "marketCap": val("marketCap"),
            "cash": val("liquidAssets"),
            "debt": val("debt"),
        },
        "provenance": {k: x.as_dict() for k, x in snap.values.items()},
    }


ENGINES = {"kci-v1": kci_v1_defaults.analyse, "kci-v2": kci_v2_defaults.analyse}
DEFAULT_CALC_VERSION = "kci-v2"


def analyse(snap: Snapshot, calc_version: str = DEFAULT_CALC_VERSION) -> dict[str, Any]:
    """Run a calculation version (default ``kci-v2``) on a live snapshot, unless the snapshot is blocked."""
    if snap.blocked:
        return {
            "ticker": snap.ticker,
            "asOf": snap.as_of.isoformat(),
            "ran": False,
            "blockedBy": snap.blocked_by,
        }
    deal = to_kci_deal(snap)
    return {"ticker": snap.ticker, "asOf": snap.as_of.isoformat(), "ran": True, "deal": deal,
            "calcVersion": calc_version, "analysis": ENGINES[calc_version](deal)}  # fmt: skip
