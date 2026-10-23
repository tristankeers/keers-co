"""A company snapshot as of a date: filed fundamentals, the share price and rates, all with provenance.

A snapshot can be *blocked*. Missing core figures, a stale price or stale fundamentals block it, and a blocked
snapshot is never passed to the valuation engine: a missing or failed check is never an approval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from keers.valuation import kci_v1_defaults, kci_v2_defaults

from . import fundamentals as F
from . import fx, yahoo
from .beta import beta
from .models import Kind, Value, missing
from .store import Store, is_sec_cik

PRICE_MAX_BUSINESS_DAYS = 3  # a price older than this many business days is stale
FUNDAMENTALS_MAX_DAYS = F.CADENCE_MAX_DAYS["quarterly"]  # 10-Q filers; see F.CADENCE_MAX_DAYS for the others
CORE = ("revenue", "ebitda", "ebit", "netIncome", "capex", "netDebt", "marketCap", "ev")
MONEY_KEYS = ("revenue", "ebitda", "ebit", "da", "netIncome", "capex", "sbc", "netDebt", "debt", "liquidAssets",
              "interestExpense", "operatingLeases", "marketCap", "ev")  # fmt: skip


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
    currency: str = "USD"  # reporting currency of the accounts; money figures are in millions of it
    price_currency: str | None = "USD"  # the currency the share price is quoted in (GBX is pence)
    cadence: str = "quarterly"  # how often the accounts arrive (fundamentals.Fundamentals.cadence)
    max_fundamentals_days: int = FUNDAMENTALS_MAX_DAYS
    source: str = "sec"  # "sec" (SEC EDGAR) or "esef" (filings.xbrl.org)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "cik": self.cik if is_sec_cik(self.cik) else None,
            "name": self.name,
            "asOf": self.as_of.isoformat(),
            "currency": self.currency,
            "priceCurrency": self.price_currency,
            "cadence": self.cadence,
            "accountsSource": self.source,
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


def price_currency(store: Store, ticker: str, source: str) -> str | None:
    """The currency a stored price is in: as the source stated it, else USD for a US symbol. None if unknown."""
    stated = store.price_currency(ticker, source)
    if stated:
        return stated
    return None if yahoo.is_foreign(ticker) else "USD"


def _latest_annual_form(store: Store, cik: int, as_of: date) -> str | None:
    forms = [f for f in store.filings(cik, ("10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A"))
             if f.filing_date <= as_of]  # fmt: skip
    return forms[-1].form.split("/")[0] if forms else None


def build(store: Store, ticker: str, as_of: date) -> Snapshot:
    co = store.company(ticker)
    if co is None:
        return Snapshot(
            ticker.upper(), 0, "", as_of, {}, [f"{ticker}: company not in the store; run refresh"]
        )
    cik = co["cik"]
    fu = F.Fundamentals(store, cik, as_of)
    cur, money = fu.currency, fu.money
    cadence = fu.cadence()
    max_days = F.CADENCE_MAX_DAYS[cadence]
    fu.shares_max_age = max(F.SHARES_MAX_AGE_DAYS, max_days)
    px = store.last_price(ticker, as_of)
    if px is not None:
        pcur = price_currency(store, ticker, px.source)
    else:
        pcur = None if yahoo.is_foreign(ticker) else "USD"
    usd = fx.rate(store, cur, "USD", px.day if px else as_of) if cur != "USD" else None
    fu.usd_per_unit = usd.value if usd is not None and usd.known else (1.0 if cur == "USD" else None)
    snap_meta: dict[str, Any] = dict(currency=cur, price_currency=pcur, cadence=cadence, max_fundamentals_days=max_days,
                                     source="sec" if is_sec_cik(cik) else "esef")  # fmt: skip
    notes: list[str] = []
    link = co.get("link")
    if link:
        notes.append(f"accounts linked to this listing ({link['basis']}): {link['detail']}")
        if link["basis"] != "manual":
            notes.append(
                "the link was made automatically by name; confirm it with: python -m keers.data link"
            )
    v: dict[str, Value] = {}
    v["revenue"] = fu.flow(F.REVENUE, "revenue")
    at = v["revenue"].period_end
    if at is None:
        snap = Snapshot(co["ticker"], cik, co["name"], as_of, v, warnings=notes, **snap_meta)
        check(snap)
        return snap
    # Every other figure must cover the same period as revenue (flows) or sit at its end (balances).
    v["ebit"] = fu.operating_income(at)
    v["da"] = fu.depreciation_amortisation(at)
    v["netIncome"] = fu.flow(F.NET_INCOME, "net income", at)
    v["capex"] = fu.flow(F.CAPEX, "capital expenditure", at)
    if v["capex"].known and v["capex"].method.startswith(F.CAPEX_COMBINED):
        c = v["capex"]
        v["capex"] = Value(c.value, c.kind, c.unit, c.method, c.period_end, c.sources, c.warnings + (
            "capex is the combined line for property, plant and equipment, intangibles and other non-current "
            "assets",))  # fmt: skip
    v["sbc"] = fu.flow(F.SBC, "share-based compensation", at)
    v["revenueGrowth"] = fu.flow_growth(F.REVENUE, "revenue")

    ebit, da = v["ebit"], v["da"]
    if ebit.known and da.known:
        assert ebit.value is not None and da.value is not None  # noqa: S101
        v["ebitda"] = _derived(ebit.value + da.value, money, "operating income + D&A (TTM)", [ebit, da])
    else:
        v["ebitda"] = missing(money, f"EBITDA: {ebit.method if not ebit.known else da.method}")

    liquid = fu.liquid_assets(at)
    debt = fu.total_debt(at)
    v["liquidAssets"], v["debt"] = liquid, debt
    v["interestExpense"] = fu.interest_expense(at, debt)
    # Shown for comparison with data vendors that count leases as debt; not included in net debt (US GAAP).
    # IFRS 16 lease liabilities are already inside debt, so there is no separate line.
    if fu.taxonomy != "ifrs-full":
        v["operatingLeases"] = fu.operating_leases(at)
    ys = fu.year_start(at)
    problem = fu.debt_check(debt, at) or (fu.debt_rollforward(at, ys) if ys else None)
    if problem:
        v["netDebt"] = missing(money, f"net debt: {problem}")
    elif liquid.known and debt.known:
        assert liquid.value is not None and debt.value is not None  # noqa: S101
        v["netDebt"] = _derived(
            debt.value - liquid.value, money, "debt − cash − short-term investments", [debt, liquid]
        )
    else:
        v["netDebt"] = missing(money, f"net debt: {debt.method if not debt.known else liquid.method}")

    v["shares"] = fu.shares(at)
    if px is None:
        v["price"] = missing(pcur or "?", "no share price in the store")
    else:
        cite = {
            "source": px.source,
            "day": px.day.isoformat(),
            "close": px.close,
            "currency": pcur,
            "raw_sha256": px.raw_sha256,
        }
        v["price"] = Value(
            px.close, Kind.MARKET, pcur or "unknown currency", f"{px.source} close", px.day, (cite,)
        )
    v["marketCap"] = _market_cap(store, v["price"], v["shares"], pcur, cur, money, cik, ticker, as_of)
    if v["marketCap"].known and v["netDebt"].known:
        assert v["marketCap"].value is not None and v["netDebt"].value is not None  # noqa: S101
        v["ev"] = _derived(
            v["marketCap"].value + v["netDebt"].value,
            money,
            "market cap + net debt",
            [v["marketCap"], v["netDebt"]],
        )
    else:
        v["ev"] = missing(money, "enterprise value: market cap or net debt missing")
    if usd is not None:
        v["fx_USD"] = usd  # US dollars per unit of the reporting currency, used to translate for the engines
    if px is not None and pcur and fx.major(pcur) != ("USD", 1.0):
        v["fx_price_USD"] = fx.rate(store, pcur, "USD", px.day)  # US dollars per unit of the price currency

    v["beta"] = beta(store, ticker, as_of, currency=pcur or "")

    for series in ("DGS10", "BAA10Y"):
        r = store.last_rate(series, as_of)
        v[f"rate_{series}"] = (
            missing("%", f"{series} not in the store")
            if r is None
            else Value(r.value, Kind.MARKET, "%", f"FRED {series}", r.day,
                       ({"source": "fred", "series": series, "day": r.day.isoformat(), "raw_sha256": r.raw_sha256},))
        )  # fmt: skip

    if cadence != "quarterly":
        notes.append(f"{cadence} accounts only (latest period ends {at}); figures are the latest "
                     f"{'fiscal year' if cadence == 'annual' else 'twelve months'} filed, not a recent quarter")  # fmt: skip
    if not is_sec_cik(cik):
        notes.append("accounts are ESEF annual reports from filings.xbrl.org, as tagged by the company; they are "
                     "not cross-checked against the printed report")  # fmt: skip
    snap = Snapshot(co["ticker"], cik, co["name"], as_of, v, warnings=notes, **snap_meta)
    check(snap)
    return snap


def _market_cap(store: Store, price: Value, shares: Value, pcur: str | None, cur: str, money: str, cik: int,
                ticker: str, as_of: date) -> Value:  # fmt: skip
    """Price × shares, converted from the price currency to the reporting currency at the price date."""
    if not (price.known and shares.known):
        return missing(money, "market cap: price or shares missing")
    assert price.value is not None and shares.value is not None and price.period_end is not None  # noqa: S101
    if pcur is None:
        return missing(money, f"market cap: the currency of {ticker}'s price is not known")
    if is_sec_cik(cik) and not yahoo.is_foreign(ticker) and _latest_annual_form(store, cik, as_of) == "20-F":
        # A US listing of a 20-F filer is usually an American depositary share, which can stand for several (or a
        # fraction of one) ordinary shares. The ratio is not in the XBRL facts, so price × shares is unknown.
        return missing(money, f"market cap: {ticker} is a US listing of a 20-F filer, so its price may be per "
                              "depositary share while the share count is ordinary shares, and the ratio is not "
                              "filed; use the home-market listing linked to this filer instead")  # fmt: skip
    r = fx.rate(store, pcur, cur, price.period_end)
    if not r.known:
        return missing(money, f"market cap: {r.method}")
    assert r.value is not None  # noqa: S101
    method = "price × shares" if r.value == 1 and pcur == cur else (
        f"price × shares × {cur} per {pcur} on {price.period_end} ({r.method})")  # fmt: skip
    return _derived(price.value * shares.value * r.value, money, method, [price, shares, r])


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
        if age > snap.max_fundamentals_days:
            snap.blocked_by.append(
                f"stale fundamentals: latest period ends {rev.period_end}, {age} days before {snap.as_of} "
                f"(limit {snap.max_fundamentals_days} days for {snap.cadence} accounts)"
            )
        elif age > FUNDAMENTALS_MAX_DAYS:
            snap.warnings.append(f"fundamentals are {age} days old ({snap.cadence} accounts; limit "
                                 f"{snap.max_fundamentals_days} days)")  # fmt: skip
    if snap.currency != "USD" and "revenue" in v and not (v.get("fx_USD") and v["fx_USD"].known):
        why = v["fx_USD"].method if v.get("fx_USD") else f"no {snap.currency} rate"
        snap.blocked_by.append(
            f"no exchange rate to translate {snap.currency} accounts to USD for the engines: {why}"
        )
    ends = {k: v[k].period_end for k in ("revenue", "ebit", "netIncome", "capex") if k in v and v[k].known}
    if len(set(ends.values())) > 1:
        snap.warnings.append(f"income figures cover different periods: {ends}")
    for k, val in v.items():
        snap.warnings += [f"{k}: {w}" for w in val.warnings if f"{k}: {w}" not in snap.warnings]


def _pct(v: float | None) -> float | None:
    return v / 100 if v is not None else None


def to_kci_deal(snap: Snapshot) -> dict[str, Any]:
    """Map a snapshot to the deal record KCI's ``analyse`` reads (USD millions).

    Accounts in another currency are translated to US dollars at one rate: the spot rate on the price date
    (``fx_USD``). Ratios are unchanged by this, the size-premium bands and the US risk-free rate then apply as for
    a US company, and the record says what was translated and at what rate. Without that rate the deal has no
    money figures, so the engines refuse to run.
    """

    def val(k: str) -> float | None:
        x = snap.values.get(k)
        return x.value if x is not None and x.known else None

    rate = 1.0
    translation: dict[str, Any] | None = None
    if snap.currency != "USD":
        r = snap.values.get("fx_USD")
        rate = r.value if r is not None and r.known and r.value else math.nan
        translation = {"from": snap.currency, "to": "USD", "usdPerUnit": r.value if r is not None else None,
                       "day": str(r.period_end) if r is not None and r.period_end else None,
                       "method": r.method if r is not None else f"no {snap.currency} rate",
                       "rule": "every money figure × one spot rate on the price date"}  # fmt: skip

    def usd(k: str) -> float | None:
        x = val(k)
        if x is None:
            return None
        y = x * rate
        return None if math.isnan(y) else y

    # The share price in dollars, for per-share comparisons with the engines' outputs.
    price_usd = val("price")
    if price_usd is not None and "fx_price_USD" in snap.values:
        pr = val("fx_price_USD")
        price_usd = price_usd * pr if pr is not None else None
    return {
        "id": f"us-{snap.ticker.lower()}",
        "name": snap.name,
        "ticker": snap.ticker,
        "currency": "USD",
        "reportingCurrency": snap.currency,
        "translation": translation,
        "revenue": usd("revenue"),
        "ebitda": usd("ebitda"),
        "ebit": usd("ebit"),
        "netIncome": usd("netIncome"),
        "capex": usd("capex"),
        "ev": usd("ev"),
        "netDebt": usd("netDebt"),
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
            "price": price_usd,
            "priceDate": str(snap.values["price"].period_end) if val("price") is not None else None,
            "sharesM": val("shares"),
            "marketCap": usd("marketCap"),
            "cash": usd("liquidAssets"),
            "debt": usd("debt"),
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
