"""Strategy A analysis: how long the cash lasts, whether the next year's debt can be met, and what is left for
the shareholders if the business is sold at a distressed price.

Deterministic, from filed accounts and the last close. Three parts, each with a level (``severe``, ``watch``,
``clear`` or ``unknown``) and a sentence saying why, like the distress screen's checks:

1. **Liquidity runway**: cash, short-term investments and undrawn committed credit lines, divided by the monthly
   cash burn. Burn is free cash flow (cash from operations less capital expenditure, TTM) when it is negative.
   A company with positive free cash flow is not burning cash, so its runway is not limited by burn.
2. **Funding gap** over the next twelve months: cash, short-term investments, undrawn credit lines and the last
   twelve months' free cash flow, less the debt due within a year. Negative means the company needs new money
   or a refinancing within the year. This assumes the next year's free cash flow repeats the last year's.
3. **Recovery value**: the business valued at stated EV / EBITDA multiples (``RECOVERY_MULTIPLES``), plus cash,
   set against total debt. Debt is paid first; equity gets what is left. It gives the share of debt covered and
   the value per share against the last close, in a low, mid and high case. The multiples are a generic
   assumption for a distressed sale, the same for every company; they are not sector multiples. When EBITDA is
   not positive an earnings multiple says nothing, so the recovery value is unknown.

Not modelled: the ranking of debt (secured before unsecured), leases outside debt, pensions, restructuring
costs, the time a restructuring takes, and new shares issued in one. These all lower what equity would get, so
the per-share values here are best cases for the equity at each multiple.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from keers.data import fundamentals as F
from keers.data import snapshot
from keers.data.models import Value
from keers.data.store import Store

from ..distress import _debt_due

ANALYSIS_VERSION = "a-analysis-v1"

RUNWAY_SEVERE, RUNWAY_WATCH = 12.0, 24.0  # months of cash at the current burn
RECOVERY_MULTIPLES = {"low": 4.0, "mid": 6.0, "high": 8.0}  # EV / EBITDA for a distressed sale (assumption)


def _v(x: Value | None) -> float | None:
    return x.value if x is not None and x.known else None


def _inp(**vals: Value | None) -> dict[str, Any]:
    return {k: (v.as_dict() if v is not None else None) for k, v in vals.items()}


def _part(name: str, level: str, value: float | None, reason: str, thresholds: str, unit: str,
          inputs: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:  # fmt: skip
    return {"name": name, "level": level, "value": value, "unit": unit, "reason": reason,
            "thresholds": thresholds, "inputs": inputs or {}, **extra}  # fmt: skip


def free_cash_flow(cfo: Value, capex: Value) -> tuple[float | None, str]:
    """Cash from operations less capital expenditure (TTM), with why it is unknown."""
    c, k = _v(cfo), _v(capex)
    if c is None:
        return None, f"cash from operations unknown: {cfo.method}"
    if k is None:
        return None, f"capital expenditure unknown: {capex.method}"
    return c - k, ""


def runway(cfo: Value, capex: Value, liquid: Value, revolver: Value | None, money: str) -> dict[str, Any]:
    t = f"severe below {RUNWAY_SEVERE:g} months, watch below {RUNWAY_WATCH:g} months of cash at the current burn"
    inp = _inp(operatingCashFlow=cfo, capex=capex, liquidAssets=liquid, undrawnCreditLines=revolver)
    fcf, why = free_cash_flow(cfo, capex)
    if fcf is None:
        return _part("runway", "unknown", None, why, t, "months", inp)
    if fcf >= 0:
        return _part("runway", "clear", None, f"free cash flow is positive ({fcf:,.0f}m TTM), so no cash is burned",
                     t, "months", inp, freeCashFlow=fcf)  # fmt: skip
    cash = _v(liquid)
    if cash is None:
        return _part(
            "runway", "unknown", None, f"cash unknown: {liquid.method}", t, "months", inp, freeCashFlow=fcf
        )
    lines = _v(revolver) or 0.0
    room = cash + lines
    burn = -fcf / 12
    months = room / burn
    lvl = "severe" if months < RUNWAY_SEVERE else "watch" if months < RUNWAY_WATCH else "clear"
    extra = f" and {lines:,.0f}m of undrawn credit lines" if lines else " (no undrawn credit lines filed)"
    return _part("runway", lvl, months,
                 f"{months:.0f} months: {cash:,.0f}m of cash and short-term investments{extra} against a burn of "
                 f"{burn:,.0f}m a month (free cash flow {fcf:,.0f}m TTM, {money})", t, "months", inp,
                 freeCashFlow=fcf, monthlyBurn=burn)  # fmt: skip


def funding_gap(
    cfo: Value, capex: Value, liquid: Value, revolver: Value | None, due: Value
) -> dict[str, Any]:
    t = "severe when cash, undrawn credit lines and a repeat of the last year's free cash flow fall short of the debt due within a year"
    inp = _inp(operatingCashFlow=cfo, capex=capex, liquidAssets=liquid, undrawnCreditLines=revolver,
               debtDueWithinYear=due)  # fmt: skip
    d = _v(due)
    if d is None:
        return _part(
            "fundingGap", "unknown", None, f"debt due within a year unknown: {due.method}", t, "money", inp
        )
    fcf, why = free_cash_flow(cfo, capex)
    if fcf is None:
        return _part("fundingGap", "unknown", None, why, t, "money", inp)
    cash = _v(liquid)
    if cash is None:
        return _part("fundingGap", "unknown", None, f"cash unknown: {liquid.method}", t, "money", inp)
    lines = _v(revolver) or 0.0
    gap = cash + lines + fcf - d
    if gap < 0:
        return _part("fundingGap", "severe", gap,
                     f"short by {-gap:,.0f}m: {d:,.0f}m of debt is due within a year against {cash:,.0f}m of cash, "
                     f"{lines:,.0f}m of undrawn credit lines and free cash flow of {fcf:,.0f}m", t, "money", inp)  # fmt: skip
    return _part("fundingGap", "clear", gap,
                 f"{gap:,.0f}m to spare after {d:,.0f}m of debt due within a year (cash {cash:,.0f}m, undrawn credit "
                 f"lines {lines:,.0f}m, free cash flow {fcf:,.0f}m)", t, "money", inp)  # fmt: skip


def recovery(ebitda: Value, debt: Value, net_debt: Value, liquid: Value, shares: Value, price: Value,
             same_currency: bool) -> dict[str, Any]:  # fmt: skip
    """Debt covered and value per share at each multiple. Level: ``severe`` when equity is worth nothing at the
    mid multiple, ``watch`` when it is worth less than the price at the mid multiple, else ``clear``."""
    mults = ", ".join(f"{k} {m:g}x" for k, m in RECOVERY_MULTIPLES.items())
    t = (f"business sold at EV / EBITDA of {mults} (a generic distressed-sale assumption); severe when equity is "
         "worth nothing at the mid multiple, watch when it is worth less than the last close")  # fmt: skip
    inp = _inp(ebitda=ebitda, debt=debt, netDebt=net_debt, liquidAssets=liquid, shares=shares, price=price)
    eb, dt, cash, sh, px = _v(ebitda), _v(debt), _v(liquid), _v(shares), _v(price)
    if _v(net_debt) is None:  # the debt figure failed its own checks (roll-forward, completeness)
        return _part("recovery", "unknown", None, f"debt not trusted: {net_debt.method}", t, "ratio", inp)
    if eb is None:
        return _part("recovery", "unknown", None, f"EBITDA unknown: {ebitda.method}", t, "ratio", inp)
    if eb <= 0:
        return _part("recovery", "unknown", None, f"EBITDA is not positive ({eb:,.0f}m), so an earnings multiple "
                     "gives no value; an asset-based value is not modelled", t, "ratio", inp, ebitdaNegative=True)  # fmt: skip
    if dt is None or cash is None:
        return _part("recovery", "unknown", None, "debt or cash unknown", t, "ratio", inp)
    if sh is None or sh <= 0 or px is None or px <= 0:
        return _part(
            "recovery", "unknown", None, "shares outstanding or share price unknown", t, "ratio", inp
        )
    if not same_currency:
        return _part("recovery", "unknown", None, "the accounts and the share price are in different currencies; "
                     "no conversion is made here", t, "ratio", inp)  # fmt: skip
    cases: dict[str, dict[str, float]] = {}
    for k, m in RECOVERY_MULTIPLES.items():
        value = eb * m + cash  # what the creditors and then the shareholders share
        equity = max(0.0, value - dt)
        per_share = equity / sh
        cases[k] = {"multiple": m, "value": value, "debtCovered": min(1.0, value / dt) if dt > 0 else 1.0,
                    "equity": equity, "perShare": per_share, "vsPrice": per_share / px - 1}  # fmt: skip
    mid = cases["mid"]
    lvl = "severe" if mid["equity"] <= 0 else "watch" if mid["vsPrice"] < 0 else "clear"
    reason = (f"at {mid['multiple']:g}x EBITDA the business and cash are worth {mid['value']:,.0f}m against "
              f"{dt:,.0f}m of debt ({mid['debtCovered']:.0%} of the debt covered), leaving {mid['perShare']:,.2f} "
              f"a share against a last close of {px:,.2f} ({mid['vsPrice']:+.0%})")  # fmt: skip
    return _part("recovery", lvl, mid["vsPrice"], reason, t, "ratio", inp, cases=cases)


def analyse(store: Store, ticker: str, as_of: date) -> dict[str, Any]:
    """All three parts for one company, from the same filed period as the distress screen."""
    snap = snapshot.build(store, ticker, as_of)
    v = snap.values
    base: dict[str, Any] = {"ticker": snap.ticker, "asOf": as_of.isoformat(), "analysisVersion": ANALYSIS_VERSION,
                            "recoveryMultiples": dict(RECOVERY_MULTIPLES), "currency": snap.currency}  # fmt: skip
    # Same rule as the screen: stale or missing core data blocks; a missing ratio input only makes a part unknown.
    stale = [b for b in snap.blocked_by if b.startswith(("stale", f"{ticker}: company not"))]
    if stale or "revenue" not in v or not v["revenue"].known:
        return base | {
            "ran": False,
            "blockedBy": stale or snap.blocked_by or ["no filed revenue"],
            "parts": [],
        }
    at = v["revenue"].period_end
    assert at is not None  # noqa: S101
    fu = F.Fundamentals(store, snap.cik, as_of)
    cfo = fu.flow(F.OPERATING_CASH_FLOW, "cash from operations", at)
    revolver = fu.revolver_available(at)
    parts = [
        runway(cfo, v["capex"], v["liquidAssets"], revolver, fu.money),
        funding_gap(cfo, v["capex"], v["liquidAssets"], revolver, _debt_due(fu, at)),
        recovery(v["ebitda"], v["debt"], v["netDebt"], v["liquidAssets"], v["shares"], v["price"],
                 snap.price_currency == snap.currency),
    ]  # fmt: skip
    return base | {"ran": True, "periodEnd": at.isoformat(), "money": fu.money, "parts": parts}
