"""Exchange rates from official free sources: FRED (the Federal Reserve's H.10 noon rates) and the ECB.

Deterministic and never guessed. A rate is the last published day on or before the date asked for, and it is
missing when that day is more than ``MAX_AGE_DAYS`` earlier, or when no free official source publishes the
currency (Saudi riyal, Chilean peso and a few others today). Pegs are not assumed.

- FRED series are stored as published (``DEXUSUK`` is US dollars per pound, ``DEXJPUS`` yen per dollar).
- ECB reference rates are stored as ``ECB:<CUR>``: units of that currency per euro, as published. A currency
  FRED does not carry is crossed through the euro with the ECB's own dollar rate for the same day
  (``ECB:USD``), so both legs come from one source and one fixing.
- Prices quoted in minor units are converted first: GBX (pence), ZAC (South African cents) and ILA (agorot)
  are one hundredth of GBP, ZAR and ILS.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from .http import Fetcher
from .market import FRED_URL, parse_fred_csv
from .models import Kind, RatePoint, Value, missing
from .sec import ParseError
from .store import Store

MAX_AGE_DAYS = 7  # a rate older than this before the date asked for is not used

# Minor units: (major currency, divisor). Yahoo also writes pence as "GBp".
MINOR_UNITS = {"GBX": ("GBP", 100.0), "ZAC": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# FRED H.10 daily series: currency -> (series id, True when the series is US dollars per unit of the currency).
FRED_FX: dict[str, tuple[str, bool]] = {
    "EUR": ("DEXUSEU", True), "GBP": ("DEXUSUK", True), "AUD": ("DEXUSAL", True), "NZD": ("DEXUSNZ", True),
    "JPY": ("DEXJPUS", False), "CAD": ("DEXCAUS", False), "CHF": ("DEXSZUS", False), "SEK": ("DEXSDUS", False),
    "NOK": ("DEXNOUS", False), "DKK": ("DEXDNUS", False), "HKD": ("DEXHKUS", False), "CNY": ("DEXCHUS", False),
    "INR": ("DEXINUS", False), "KRW": ("DEXKOUS", False), "SGD": ("DEXSIUS", False), "TWD": ("DEXTAUS", False),
    "THB": ("DEXTHUS", False), "MYR": ("DEXMAUS", False), "BRL": ("DEXBZUS", False), "MXN": ("DEXMXUS", False),
    "ZAR": ("DEXSFUS", False),
}  # fmt: skip

# Currencies the ECB publishes reference rates for that FRED does not carry.
ECB_ONLY = ("PLN", "TRY", "ILS", "IDR", "PHP", "CZK", "HUF", "RON", "ISK", "BGN")
ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{cur}.EUR.SP00.A?format=csvdata&startPeriod={start}"
)


def major(currency: str) -> tuple[str, float]:
    """(ISO currency, divisor) for a quote currency: ("GBP", 100) for pence, (c, 1) otherwise."""
    c = currency.strip()
    if c == "GBp":
        return "GBP", 100.0
    c = c.upper()
    return MINOR_UNITS.get(c, (c, 1.0))


def series_for(currencies: Iterable[str]) -> tuple[list[str], list[str]]:
    """(FRED series, ECB currencies) needed to convert the given currencies to and from US dollars."""
    fred: set[str] = set()
    ecb: set[str] = set()
    for c in currencies:
        cur = major(c)[0]
        if cur == "USD":
            continue
        if cur in FRED_FX:
            fred.add(FRED_FX[cur][0])
        elif cur in ECB_ONLY:
            ecb |= {cur, "USD"}
    return sorted(fred), sorted(ecb)


def supported(currency: str) -> bool:
    cur = major(currency)[0]
    return cur == "USD" or cur in FRED_FX or cur in ECB_ONLY


def parse_ecb_csv(text: str, currency: str, raw_sha256: str) -> list[RatePoint]:
    """ECB SDMX ``csvdata``: one row per day, columns named in the header (TIME_PERIOD, OBS_VALUE, ...)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "TIME_PERIOD" not in rows[0] or "OBS_VALUE" not in rows[0]:
        raise ParseError(f"ecb {currency}: not an SDMX CSV (header {list(rows[0]) if rows else None})")
    out = []
    for r in rows:
        if r.get("CURRENCY") not in (None, currency) or r.get("CURRENCY_DENOM") not in (None, "EUR"):
            raise ParseError(f"ecb {currency}: row for {r.get('CURRENCY')}/{r.get('CURRENCY_DENOM')}")
        v = (r.get("OBS_VALUE") or "").strip()
        if not v or v.upper() == "NAN":
            continue
        out.append(
            RatePoint(f"ECB:{currency}", date.fromisoformat(r["TIME_PERIOD"]), float(v), "ecb", raw_sha256)
        )
    return out


def refresh(fetcher: Fetcher, store: Store, currencies: Iterable[str], start: date) -> dict[str, Any]:
    """Fetch what is needed to convert ``currencies`` to US dollars. Raises on a failed source."""
    fred, ecb = series_for(currencies)
    out: dict[str, Any] = {}
    for s in fred:
        resp = fetcher.get(FRED_URL.format(series=s))
        store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        points = parse_fred_csv(resp.body.decode(), s, resp.sha256)
        if not points:
            raise ParseError(f"fred {s}: no observations")
        store.put_rates(points)
        out[s] = {"rows": len(points), "last": str(points[-1].day), "value": points[-1].value}
    for c in ecb:
        resp = fetcher.get(ECB_URL.format(cur=c, start=start.isoformat()))
        store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        points = parse_ecb_csv(resp.body.decode("utf-8-sig"), c, resp.sha256)
        if not points:
            raise ParseError(f"ecb {c}: no observations since {start}")
        store.put_rates(points)
        out[f"ECB:{c}"] = {"rows": len(points), "last": str(points[-1].day), "value": points[-1].value}
    return out


def _cite(p: RatePoint) -> dict[str, Any]:
    return {"source": p.source, "series": p.series, "day": p.day.isoformat(), "value": p.value,
            "raw_sha256": p.raw_sha256}  # fmt: skip


def _usd_per(store: Store, cur: str, day: date) -> tuple[float, list[RatePoint], str] | str:
    """US dollars per one unit of ``cur`` on ``day`` (last published on or before it), or why not."""
    if cur == "USD":
        return 1.0, [], ""
    lo = day - timedelta(days=MAX_AGE_DAYS)
    if cur in FRED_FX:
        series, usd_per_unit = FRED_FX[cur]
        p = store.last_rate(series, day)
        if p is None or p.day < lo:
            return f"FRED {series} has no rate between {lo} and {day}"
        if p.value <= 0:
            return f"FRED {series} on {p.day} is not positive"
        return (p.value if usd_per_unit else 1 / p.value), [p], f"FRED {series}"
    if cur in ECB_ONLY:
        x, u = store.last_rate(f"ECB:{cur}", day), store.last_rate("ECB:USD", day)
        if x is None or u is None or x.day < lo or u.day < lo:
            return f"ECB has no {cur} and USD rates between {lo} and {day}"
        if x.day != u.day:
            # Cross on the same fixing only; take the USD rate for the currency's day.
            same = store.rates("ECB:USD", x.day, x.day)
            if not same:
                return f"ECB {cur} on {x.day} has no USD rate the same day"
            u = same[0]
        return u.value / x.value, [x, u], f"ECB {cur} per EUR crossed with ECB USD per EUR on {x.day}"
    return f"no free official exchange-rate source for {cur}"


def rate(store: Store, frm: str, to: str, day: date) -> Value:
    """Units of ``to`` per one unit of ``frm`` on ``day``, with the published rates it came from.

    Minor units are handled: ``rate(store, "GBX", "USD", d)`` is the pound rate divided by 100.
    """
    f_cur, f_div = major(frm)
    t_cur, t_div = major(to)
    if f_cur == t_cur:
        return Value(t_div / f_div, Kind.DERIVED, f"{to}/{frm}", "same currency" if f_div == t_div else
                     f"{frm} is 1/{f_div:g} of {f_cur}", day)  # fmt: skip
    a = _usd_per(store, f_cur, day)
    b = _usd_per(store, t_cur, day)
    for res in (a, b):
        if isinstance(res, str):
            return missing(f"{to}/{frm}", f"exchange rate {frm}→{to} on {day}: {res}")
    assert not isinstance(a, str) and not isinstance(b, str)  # noqa: S101
    value = a[0] / b[0] * t_div / f_div
    used = a[1] + b[1]
    how = " and ".join(x for x in (a[2], b[2]) if x)
    if f_div != 1 or t_div != 1:
        how += "; minor unit converted (÷100)"
    last = min((p.day for p in used), default=day)
    return Value(value, Kind.MARKET, f"{to}/{frm}", f"{to} per {frm} from {how}", last,
                 tuple(_cite(p) for p in used))  # fmt: skip


def daily_usd_per(store: Store, currency: str, start: date, end: date) -> dict[date, float] | str:
    """US dollars per one unit of ``currency`` for each published day in [start, end], or why not."""
    cur, div = major(currency)
    if cur == "USD":
        return {}
    if cur in FRED_FX:
        series, usd_per_unit = FRED_FX[cur]
        pts = store.rates(series, start, end)
        if not pts:
            return f"FRED {series} has no rates from {start} to {end}"
        return {p.day: (p.value if usd_per_unit else 1 / p.value) / div for p in pts if p.value > 0}
    if cur in ECB_ONLY:
        xs = {p.day: p.value for p in store.rates(f"ECB:{cur}", start, end)}
        us = {p.day: p.value for p in store.rates("ECB:USD", start, end)}
        days = sorted(set(xs) & set(us))
        if not days:
            return f"ECB has no {cur} and USD rates from {start} to {end}"
        return {d: us[d] / xs[d] / div for d in days if xs[d] > 0}
    return f"no free official exchange-rate source for {cur}"
