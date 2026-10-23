"""Accounting signals for strategy C (``signals-v2``), from filed accounts as the market knew them on the as-of date.

* Value: earnings yield, operating income (EBIT, TTM) / enterprise value. Higher is cheaper.
* Quality: return on assets, operating income (EBIT, TTM) / total assets at the end of the same period.

Both come from ``keers.data.snapshot`` built as of the signal date, so only filings made by then count, and the
same debt and period checks as the valuation apply. Unknown, never guessed, when a figure is missing, the accounts
are older than the company's reporting cadence allows, enterprise value is not positive, or the company is a bank,
insurer or other financial (SIC 6000-6799), whose operating income and debt mean something else.
"""

from __future__ import annotations

from .pit import PointInTime, snapshot
from .signals import Signal


def _gate(
    pit: PointInTime, ticker: str, name: str
) -> tuple[Signal | None, float | None, float | None, float | None]:
    snap, co, assets = snapshot(pit, ticker)
    if co is None:
        return Signal(name, None, None, "company not in the store"), None, None, None
    sic = str(co.get("sic") or "").strip()
    if sic.isdigit() and 6000 <= int(sic) <= 6799:
        return (
            Signal(name, None, None, f"financial company (SIC {sic}): EBIT and EV not comparable"),
            None,
            None,
            None,
        )
    rev = snap.values.get("revenue")
    if rev is None or rev.period_end is None:
        return Signal(name, None, None, "no accounts filed by the signal date"), None, None, None
    age = (pit.as_of - rev.period_end).days
    if age > snap.max_fundamentals_days:
        return Signal(name, None, None, f"accounts to {rev.period_end} are {age} days old"), None, None, None
    ebit, ev = snap.values.get("ebit"), snap.values.get("ev")
    return (None, ebit.value if ebit is not None else None, ev.value if ev is not None else None,
            assets.value if assets.known else None)  # fmt: skip


def value(pit: PointInTime, ticker: str) -> Signal:
    bad, ebit, ev, _ = _gate(pit, ticker, "value")
    if bad is not None:
        return bad
    if ebit is None or ev is None:
        return Signal("value", None, None, "operating income or enterprise value missing")
    if ev <= 0:
        return Signal("value", None, None, "enterprise value is not positive")
    y = ebit / ev
    return Signal("value", y, y, "EBIT (TTM) / enterprise value")


def quality(pit: PointInTime, ticker: str) -> Signal:
    bad, ebit, _, assets = _gate(pit, ticker, "quality")
    if bad is not None:
        return bad
    if ebit is None or assets is None or assets <= 0:
        return Signal("quality", None, None, "operating income or total assets missing")
    r = ebit / assets
    return Signal("quality", r, r, "EBIT (TTM) / total assets")
