"""KCI calculation engine, version ``kci-v1``, ported to Python.

This is a line-by-line port of ``legacy/kci/engine.js``. It reproduces the
JavaScript results, including the weaknesses found in the 22 Oct 2026 audit.
Do not "fix" anything here: fixes belong in a new calculation version, so
that every change in output is visible and deliberate (see ADR 3).

Conventions carried over from KCI:

* Currency amounts are in millions; rates are decimals (0.25 means 25%).
* ``None`` means unknown and ``0`` means a known zero.
* Functions return explicit errors instead of NaN.
* Inputs and outputs are plain dicts using KCI's field names (``entryEbitda``,
  ``holdYears`` and so on) so they can be compared one to one with the
  JavaScript engine. Typed models arrive with the data layer.

Not ported: CSV parsing and display formatting, which are user-interface helpers.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeGuard

CALC_VERSION = "kci-v1"

Json = dict[str, Any]

EPS = 1e-6


# ---------- primitives ----------


def is_num(v: Any) -> TypeGuard[float]:
    """JavaScript ``Number.isFinite`` on a number: true for finite ints and floats, never for bools."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def num(v: Any) -> float | None:
    return v if is_num(v) else None


def safe_div(a: Any, b: Any) -> float | None:
    return a / b if is_num(a) and is_num(b) and b != 0 else None


def total(values: Sequence[Any]) -> float:
    """Sum of the numeric entries; anything else counts as zero (KCI's ``sum``)."""
    s: float = 0
    for v in values:
        if is_num(v):
            s += v
    return s


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def at(x: Any, i: int) -> Any:
    """Year ``i`` of a per-year input: lists repeat their last value; scalars apply to every year."""
    if isinstance(x, list):
        return x[min(i, len(x) - 1)] if x else None
    return x


def nz(v: Any) -> Any:
    """JavaScript ``v ?? 0``."""
    return 0 if v is None else v


def truthy(v: Any) -> bool:
    """JavaScript truthiness for the values KCI handles (None, numbers, strings, bools, containers)."""
    if v is None:
        return False
    if isinstance(v, float) and math.isnan(v):
        return False
    return bool(v) if not isinstance(v, (list, dict)) else True


def or0(v: Any) -> Any:
    """JavaScript ``v || 0``."""
    return v if truthy(v) else 0


def gt(a: Any, b: float) -> bool:
    """JavaScript ``a > b`` where ``a`` may be null (null compares as 0)."""
    if a is None:
        return 0 > b
    return bool(is_num(a) and a > b)


def fixed(v: float, dp: int) -> str:
    """JavaScript ``v.toFixed(dp)``, so text such as warnings matches KCI exactly."""
    if v == 0:
        v = 0.0  # JavaScript prints negative zero without a sign
    return str(Decimal(v).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


# ---------- time value ----------


def npv(rate: float, cfs: Sequence[float], times: Sequence[float] | None = None) -> float:
    s = 0.0
    for i, cf in enumerate(cfs):
        t = times[i] if times is not None else i
        s += cf / math.pow(1 + rate, t)
    return s


def irr(cfs: Sequence[Any], times: Sequence[float] | None = None) -> Json:
    """IRR by bracketed bisection on NPV(r) = 0. Returns ``{"value", "note"}``. Annual periods."""
    if not isinstance(cfs, (list, tuple)) or len(cfs) < 2 or not all(is_num(c) for c in cfs):
        return {"value": None, "note": "Cash flows incomplete"}
    has_neg = any(c < -EPS for c in cfs)
    has_pos = any(c > EPS for c in cfs)
    if not has_neg:
        return {"value": None, "note": "No equity invested"}
    if not has_pos:
        return {"value": -1, "note": "Total loss: no equity proceeds"}

    def f(r: float) -> float:
        return npv(r, cfs, times)

    lo, hi = -0.999999, 1.0
    flo, fhi = f(lo), f(hi)
    guard = 0
    while _sign(flo) == _sign(fhi) and guard < 60:
        hi = hi * 2 + 1
        fhi = f(hi)
        guard += 1
        if hi > 1e6:
            break
    if _sign(flo) == _sign(fhi):
        return {"value": None, "note": "IRR could not be bracketed"}
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-10 or (hi - lo) / 2 < 1e-12:
            return {"value": mid, "note": None}
        if _sign(fm) == _sign(flo):
            lo, flo = mid, fm
        else:
            hi, fhi = mid, fm
    return {"value": (lo + hi) / 2, "note": None}


# ---------- statistics ----------


def quantile(sorted_values: Sequence[float], p: float) -> float | None:
    """Linear interpolation, as Excel QUARTILE.INC / PERCENTILE.INC."""
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def stats(values: Sequence[Any]) -> Json:
    v = sorted(x for x in values if is_num(x))
    if not v:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "q1": None,
            "q3": None,
            "min": None,
            "max": None,
            "iqr": None,
            "lowerFence": None,
            "upperFence": None,
        }
    q1, q3 = quantile(v, 0.25), quantile(v, 0.75)
    assert q1 is not None and q3 is not None
    iqr_ = q3 - q1
    return {
        "n": len(v),
        "mean": total(v) / len(v),
        "median": quantile(v, 0.5),
        "q1": q1,
        "q3": q3,
        "min": v[0],
        "max": v[-1],
        "iqr": iqr_,
        "lowerFence": q1 - 1.5 * iqr_,
        "upperFence": q3 + 1.5 * iqr_,
    }


# ---------- valuation bridge ----------


def ev_bridge(
    ebitda: Any,
    multiple: Any,
    netDebt: Any,
    otherClaims: Any = 0,  # noqa: N803 (KCI names)
    nonOperatingAssets: Any = 0,
) -> Json:
    errors = []
    if not is_num(ebitda):
        errors.append("EBITDA is required")
    if not is_num(multiple):
        errors.append("EV/EBITDA multiple is required")
    if not is_num(netDebt):
        errors.append("Net debt is required (enter 0 if the business has none)")
    if errors:
        return {"ok": False, "errors": errors}
    ev = ebitda * multiple
    equity = ev - netDebt - or0(otherClaims) + or0(nonOperatingAssets)
    return {
        "ok": True,
        "errors": [],
        "ev": ev,
        "equity": equity,
        "netDebt": netDebt,
        "otherClaims": or0(otherClaims),
        "nonOperatingAssets": or0(nonOperatingAssets),
        "warnings": ["EBITDA is not positive; an EV/EBITDA valuation is not meaningful"]
        if ebitda <= 0
        else [],
    }


# ---------- comparable companies ----------

MULTIPLES: list[dict[str, str]] = [
    {"key": "evRevenue", "label": "EV / Revenue", "num": "ev", "den": "revenue"},
    {"key": "evEbitda", "label": "EV / EBITDA", "num": "ev", "den": "ebitda"},
    {"key": "evEbit", "label": "EV / EBIT", "num": "ev", "den": "ebit"},
    {"key": "pe", "label": "P / E", "num": "marketCap", "den": "netIncome"},
]


def comp_multiples(c: Mapping[str, Any]) -> Json:
    out: Json = {}
    for m in MULTIPLES:
        n, d = num(c.get(m["num"])), num(c.get(m["den"]))
        if n is None or d is None:
            out[m["key"]] = {"value": None, "status": "missing"}
        elif d <= 0:
            out[m["key"]] = {"value": None, "status": "nm"}
        else:
            out[m["key"]] = {"value": n / d, "status": "ok"}
    return out


def comps_analysis(companies: Sequence[Mapping[str, Any]], target: Mapping[str, Any] | None = None) -> Json:
    included = [c for c in companies if c.get("include") is not False]
    rows = [{**c, "multiples": comp_multiples(c)} for c in companies]
    summary: Json = {}
    for m in MULTIPLES:
        vals = [r["multiples"][m["key"]]["value"] for r in rows if r.get("include") is not False]
        s = stats([v for v in vals if is_num(v)])
        summary[m["key"]] = s
        for r in rows:
            v = r["multiples"][m["key"]]["value"]
            r["multiples"][m["key"]]["outlier"] = bool(
                is_num(v) and s["n"] >= 4 and (v < s["lowerFence"] or v > s["upperFence"])
            )
    missing = []
    for r in rows:
        if r.get("include") is False:
            continue
        for m in MULTIPLES:
            if r["multiples"][m["key"]]["status"] != "ok":
                missing.append(
                    {
                        "name": r.get("name"),
                        "multiple": m["label"],
                        "status": r["multiples"][m["key"]]["status"],
                    }
                )
    implied: Json | None = None
    if target is not None:
        implied = {}
        for m in MULTIPLES:
            base = num(target.get("netIncome")) if m["den"] == "netIncome" else num(target.get(m["den"]))
            s = summary[m["key"]]
            if base is None or base <= 0 or not s["n"]:
                implied[m["key"]] = None
                continue
            is_equity = m["key"] == "pe"
            b = base

            def to_ev(mult: float, b: float = b, is_equity: bool = is_equity) -> float:
                return mult * b + or0(num(target.get("netDebt"))) if is_equity else mult * b

            implied[m["key"]] = {
                "low": to_ev(s["q1"]),
                "mid": to_ev(s["median"]),
                "high": to_ev(s["q3"]),
                "basis": "Equity value + net debt" if is_equity else "Enterprise value",
            }
    return {
        "rows": rows,
        "summary": summary,
        "missing": missing,
        "implied": implied,
        "includedCount": len(included),
    }


# ---------- WACC ----------


def wacc(inp: Mapping[str, Any]) -> Json:
    errors = []
    for label, key in [
        ("Risk-free rate", "riskFree"),
        ("Beta", "beta"),
        ("Equity risk premium", "erp"),
        ("Pre-tax cost of debt", "preTaxCostOfDebt"),
        ("Tax rate", "taxRate"),
        ("Debt weight", "debtWeight"),
    ]:
        if not is_num(inp.get(key)):
            errors.append(label + " is required")
    if errors:
        return {"ok": False, "errors": errors}
    dw = inp["debtWeight"]
    if dw < 0 or dw >= 1:
        return {"ok": False, "errors": ["Debt weight must be between 0% and 99%"]}
    cost_of_equity = inp["riskFree"] + inp["beta"] * inp["erp"] + or0(inp.get("sizePremium", 0))
    after_tax_cost_of_debt = inp["preTaxCostOfDebt"] * (1 - inp["taxRate"])
    value = (1 - dw) * cost_of_equity + dw * after_tax_cost_of_debt
    return {
        "ok": True,
        "errors": [],
        "costOfEquity": cost_of_equity,
        "afterTaxCostOfDebt": after_tax_cost_of_debt,
        "equityWeight": 1 - dw,
        "debtWeight": dw,
        "value": value,
    }


# ---------- DCF ----------


def _is_int(v: Any) -> TypeGuard[float]:
    return is_num(v) and float(v).is_integer()


def run_dcf(inp: Mapping[str, Any]) -> Json:
    """Unlevered DCF.

    Unlevered FCF = EBIT x (1 - t) + D&A - capex - change in NWC, with tax floored at zero when EBIT is
    negative. Change in NWC = NWC% x change in revenue. Terminal value is valued at the end of the final
    year under both methods. The mid-year convention discounts each year's FCF at (t - 0.5).
    """
    errors: list[str] = []
    warnings: list[str] = []
    n_years = inp.get("years")
    w_in = inp.get("wacc")
    if not _is_int(n_years) or n_years < 1 or n_years > 15:
        errors.append("Projection period must be 1 to 15 years")
    if not is_num(inp.get("baseRevenue")) or inp["baseRevenue"] <= 0:
        errors.append("Base-year revenue must be positive")
    if not is_num(w_in) or w_in <= 0:
        errors.append("WACC must be positive")
    tax_rate = inp.get("taxRate")
    if not is_num(tax_rate) or tax_rate < 0 or tax_rate >= 1:
        errors.append("Tax rate must be between 0% and 99%")
    method = inp.get("terminalMethod") or "gordon"
    if method in ("gordon", "both"):
        tg = inp.get("terminalGrowth")
        if not is_num(tg):
            errors.append("Terminal growth rate is required")
        elif is_num(w_in) and tg >= w_in:
            errors.append("Terminal growth must be below WACC")
    if method in ("exit", "both") and (not is_num(inp.get("exitMultiple")) or inp["exitMultiple"] <= 0):
        errors.append("Exit multiple must be positive")
    if _is_int(n_years):
        for i in range(int(n_years)):
            if not is_num(at(inp.get("revenueGrowth"), i)):
                errors.append(f"Revenue growth is missing for year {i + 1}")
                break
            mg = at(inp.get("ebitdaMargin"), i)
            if not is_num(mg) or mg <= -1 or mg > 1:
                errors.append(f"EBITDA margin for year {i + 1} must be a number between -100% and 100%")
                break
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    # Validation above guarantees both are numbers.
    n = int(inp["years"])
    w: float = inp["wacc"]
    rows = []
    prev_rev = inp["baseRevenue"]
    for i in range(n):
        g = nz(at(inp.get("revenueGrowth"), i))
        revenue = prev_rev * (1 + g)
        margin = nz(at(inp.get("ebitdaMargin"), i))
        ebitda = revenue * margin
        da = revenue * nz(at(inp.get("daPct"), i))
        ebit = ebitda - da
        tax = max(0, ebit * tax_rate)
        nopat = ebit - tax
        capex = revenue * nz(at(inp.get("capexPct"), i))
        d_nwc = (revenue - prev_rev) * nz(at(inp.get("nwcPct"), i))
        ufcf = nopat + da - capex - d_nwc
        t = i + 1
        disc_t = t - 0.5 if truthy(inp.get("midYear")) else t
        df = 1 / math.pow(1 + w, disc_t)
        rows.append(
            {
                "year": t,
                "revenue": revenue,
                "growth": g,
                "ebitda": ebitda,
                "margin": margin,
                "da": da,
                "ebit": ebit,
                "tax": tax,
                "nopat": nopat,
                "capex": capex,
                "dNwc": d_nwc,
                "ufcf": ufcf,
                "discountPeriod": disc_t,
                "discountFactor": df,
                "pv": ufcf * df,
            }
        )
        prev_rev = revenue
    sum_pv = total([r["pv"] for r in rows])
    last = rows[n - 1]
    # Exit multiple: a sale at the end of year N, discounted N periods. Perpetuity: with the mid-year
    # convention its cash flows also arrive mid-year, so it is discounted N - 0.5 periods.
    half = math.pow(1 + w, 0.5) if truthy(inp.get("midYear")) else 1
    df_exit = 1 / math.pow(1 + w, n)
    df_gordon = df_exit * half
    out: Json = {
        "ok": True,
        "errors": errors,
        "warnings": warnings,
        "rows": rows,
        "sumPV": sum_pv,
        "method": method,
        "terminal": {},
        "calcVersion": CALC_VERSION,
    }
    net_debt_known = is_num(inp.get("netDebt"))
    if not net_debt_known:
        warnings.append("Net debt is unknown, so equity value and value per share cannot be calculated")
    claims = (
        (inp["netDebt"] if net_debt_known else 0)
        + or0(num(inp.get("otherClaims")))
        - or0(num(inp.get("nonOperatingAssets")))
    )
    shares = inp.get("sharesOutstanding")

    def build(tv: float, df: float) -> Json:
        pv_tv = tv * df
        ev = sum_pv + pv_tv
        equity = ev - claims if net_debt_known else None
        per_share = equity / shares if is_num(equity) and is_num(shares) and shares > 0 else None
        return {
            "tv": tv,
            "pvTV": pv_tv,
            "discountFactor": df,
            "ev": ev,
            "equity": equity,
            "perShare": per_share,
            "tvShareOfEV": safe_div(pv_tv, ev),
        }

    if method in ("gordon", "both"):
        tg = inp["terminalGrowth"]
        tv = last["ufcf"] * (1 + tg) / (w - tg)
        out["terminal"]["gordon"] = {
            **build(tv, df_gordon),
            "impliedExitMultiple": (tv * half) / last["ebitda"] if last["ebitda"] > 0 else None,
        }
        if last["ufcf"] <= 0:
            warnings.append(
                "Final-year free cash flow is not positive; the perpetuity value is not meaningful"
            )
    if method in ("exit", "both"):
        tv = last["ebitda"] * inp["exitMultiple"]
        big_t = tv / half
        g_imp = safe_div(big_t * w - last["ufcf"], big_t + last["ufcf"])
        out["terminal"]["exit"] = {**build(tv, df_exit), "impliedPerpetualGrowth": g_imp}
        if last["ebitda"] <= 0:
            warnings.append(
                "Final-year EBITDA is not positive; an exit-multiple terminal value is not meaningful"
            )
    out["primary"] = out["terminal"]["exit" if method == "exit" else "gordon"]
    out["claims"] = {
        "netDebt": inp["netDebt"] if net_debt_known else None,
        "otherClaims": or0(num(inp.get("otherClaims"))),
        "nonOperatingAssets": or0(num(inp.get("nonOperatingAssets"))),
    }
    tv_share = out["primary"]["tvShareOfEV"]
    if tv_share is not None and tv_share > 0.85:
        warnings.append(
            "Terminal value is over 85% of enterprise value; the result is highly sensitive to "
            "terminal assumptions"
        )
    return out


# ---------- LBO ----------

LBO_DEFAULTS: Json = {
    "cashSweepPct": 1,
    "minCash": 0,
    "rollover": 0,
    "transactionFeePct": 0,
    "financingFeePct": 0,
    "exitFeePct": 0,
    "existingDebt": 0,
    "existingCash": 0,
    "interestOnCashRate": 0,
    "daPct": 0,
    "capexPct": 0,
    "nwcPct": 0,
    "revolver": {"commitment": 0, "rate": 0},
}


def validate_lbo(inp: Mapping[str, Any]) -> list[str]:
    e: list[str] = []
    eb, rev = inp.get("entryEbitda"), inp.get("entryRevenue")
    if not is_num(eb) or eb <= 0:
        e.append("Entry EBITDA must be positive")
    if not is_num(rev) or rev <= 0:
        e.append("Entry revenue must be positive")
    if is_num(rev) and is_num(eb) and eb > rev:
        e.append("Entry EBITDA cannot exceed revenue")
    ev_given = is_num(inp.get("entryEVOverride")) and inp["entryEVOverride"] > 0
    if not ev_given and (not is_num(inp.get("entryMultiple")) or inp["entryMultiple"] <= 0):
        e.append("Entry multiple must be positive")
    hy = inp.get("holdYears")
    if not _is_int(hy) or hy < 1 or hy > 10:
        e.append("Holding period must be a whole number of years from 1 to 10")
    if not is_num(inp.get("exitMultiple")) or inp["exitMultiple"] <= 0:
        e.append("Exit multiple must be positive")
    tr = inp.get("taxRate")
    if not is_num(tr) or tr < 0 or tr >= 1:
        e.append("Tax rate must be between 0% and 99%")
    tranches = inp.get("tranches")
    if not isinstance(tranches, list):
        e.append("Debt tranches are required")
    else:
        for i, t in enumerate(tranches):
            name = t.get("name") or f"Tranche {i + 1}"
            if not is_num(t.get("leverage")) or t["leverage"] < 0:
                e.append(name + ": leverage must be zero or more")
            if not is_num(t.get("rate")) or t["rate"] < 0:
                e.append(name + ": interest rate must be zero or more")
            if not is_num(t.get("amortPct")) or t["amortPct"] < 0 or t["amortPct"] > 1:
                e.append(name + ": amortisation must be 0% to 100% a year")
    sw = inp.get("cashSweepPct")
    if is_num(sw) and (sw < 0 or sw > 1):
        e.append("Cash sweep must be 0% to 100%")
    if is_num(inp.get("minCash")) and inp["minCash"] < 0:
        e.append("Minimum cash cannot be negative")
    if is_num(inp.get("rollover")) and inp["rollover"] < 0:
        e.append("Management rollover cannot be negative")
    if _is_int(hy):
        for i in range(int(hy)):
            if not is_num(at(inp.get("revenueGrowth"), i)):
                e.append(f"Revenue growth is missing for year {i + 1}")
                break
            mg = at(inp.get("ebitdaMargin"), i)
            if not is_num(mg) or mg <= -1 or mg > 1:
                e.append(f"EBITDA margin for year {i + 1} must be a number between -100% and 100%")
                break
    for k in ("daPct", "capexPct", "nwcPct"):
        v = inp.get(k)
        if v is not None and not isinstance(v, list) and not is_num(v):
            e.append(k + " must be a number")
    return e


def run_lbo(raw: Mapping[str, Any]) -> Json:
    """Annual LBO model.

    Conventions: entry at t = 0, cash flows at year ends, exit at the end of year N. Cash-free, debt-free
    purchase. Interest on opening balances (no circularity); interest income on opening cash. Tax on positive
    pre-tax profit after loss carry-forwards (losses carried forward indefinitely, no interest cap). Cash order
    each year: operating FCF, mandatory amortisation, revolver draw to minimum cash, sponsor equity cure for
    any remaining deficit, then excess repays the revolver and the sweep % prepays sweep-eligible tranches in
    order. Equity cures do not change ownership. Exit equity is floored at zero.
    """
    inp: Json = {**LBO_DEFAULTS, **raw}
    errors = validate_lbo(inp)
    if errors:
        return {"ok": False, "errors": errors, "warnings": []}
    warnings: list[str] = []
    n = int(inp["holdYears"])
    min_cash = or0(inp["minCash"])
    revolver_cfg = inp["revolver"] or {}
    rate_shift = or0(inp.get("rateShift"))

    # Entry
    entry_ebitda = inp["entryEbitda"]
    override = inp.get("entryEVOverride")
    entry_ev = override if is_num(override) and override > 0 else entry_ebitda * inp["entryMultiple"]
    entry_multiple_eff = entry_ev / entry_ebitda
    tranches = [
        {**t, "name": t.get("name") or f"Tranche {i + 1}", "amount": t["leverage"] * entry_ebitda}
        for i, t in enumerate(inp["tranches"])
    ]
    new_debt = total([t["amount"] for t in tranches])
    transaction_fees = entry_ev * or0(inp["transactionFeePct"])
    financing_fees = new_debt * or0(inp["financingFeePct"])
    existing_net_debt = or0(inp["existingDebt"]) - or0(inp["existingCash"])
    equity_purchase_price = entry_ev - existing_net_debt
    uses = [
        {"label": "Purchase of equity", "value": equity_purchase_price},
        {"label": "Refinance existing debt", "value": or0(inp["existingDebt"])},
        {"label": "Less: cash acquired with the business", "value": -or0(inp["existingCash"])},
        {"label": "Transaction fees", "value": transaction_fees},
        {"label": "Financing fees", "value": financing_fees},
        {"label": "Cash to balance sheet", "value": inp["minCash"]},
    ]
    total_uses = total([u["value"] for u in uses])
    rollover = or0(inp["rollover"])
    sponsor_equity = (
        total_uses - new_debt - inp["rollover"] if is_num(inp["rollover"]) else total_uses - new_debt
    )
    sources = [
        *({"label": t["name"], "value": t["amount"], "multiple": t["leverage"]} for t in tranches),
        {"label": "Management rollover", "value": inp["rollover"]},
        {"label": "Sponsor equity", "value": sponsor_equity},
    ]
    total_sources = total([s["value"] for s in sources])
    if equity_purchase_price < 0:
        errors.append("Existing net debt exceeds the purchase enterprise value")
    if sponsor_equity <= 0:
        errors.append(
            "Debt and rollover fund all uses, so sponsor equity is zero or negative. Reduce leverage."
        )
    if errors:
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
            "entry": {"entryEV": entry_ev, "newDebt": new_debt, "totalUses": total_uses},
        }
    total_equity_in = sponsor_equity + rollover
    sponsor_share = sponsor_equity / total_equity_in
    if new_debt / entry_ebitda > 7:
        warnings.append("Entry leverage above 7.0x EBITDA is aggressive for lower-middle-market credit")

    # Projection
    bal = [t["amount"] for t in tranches]
    revolver = 0.0
    cash = min_cash
    nol = 0.0
    prev_rev = inp["entryRevenue"]
    years: list[Json] = []
    checks: list[Json] = []
    commitment = or0(revolver_cfg.get("commitment"))
    tax_rate = inp["taxRate"]
    for i in range(n):
        y = i + 1
        g = nz(at(inp["revenueGrowth"], i))
        revenue = prev_rev * (1 + g)
        margin = at(inp["ebitdaMargin"], i)
        ebitda = revenue * margin
        da = revenue * nz(at(inp["daPct"], i))
        ebit = ebitda - da
        open_bal = list(bal)
        open_revolver = revolver
        open_cash = cash
        tranche_interest = [open_bal[k] * (t["rate"] + rate_shift) for k, t in enumerate(tranches)]
        revolver_interest = open_revolver * (or0(revolver_cfg.get("rate")) + rate_shift)
        interest_expense = total(tranche_interest) + revolver_interest
        interest_income = max(0, open_cash) * or0(inp["interestOnCashRate"])
        ebt = ebit - interest_expense + interest_income
        taxable, nol_used = ebt, 0.0
        if ebt < 0:
            nol += -ebt
            taxable = 0
        else:
            nol_used = min(nol, ebt)
            nol -= nol_used
            taxable = ebt - nol_used
        tax = taxable * tax_rate
        net_income = ebt - tax
        capex = revenue * nz(at(inp["capexPct"], i))
        d_nwc = (revenue - prev_rev) * nz(at(inp["nwcPct"], i))
        fcf = net_income + da - capex - d_nwc
        cash_conversion = (ebitda - capex - d_nwc) / ebitda if ebitda > 0 else None

        cash = open_cash + fcf
        mandatory = [min(open_bal[k], t["amortPct"] * t["amount"]) for k, t in enumerate(tranches)]
        for k, m in enumerate(mandatory):
            bal[k] -= m
        cash -= total(mandatory)
        revolver_draw, equity_cure = 0.0, 0.0
        if cash < min_cash:
            capacity = max(0, commitment - revolver)
            revolver_draw = min(min_cash - cash, capacity)
            revolver += revolver_draw
            cash += revolver_draw
        if cash < 0:
            equity_cure = -cash
            cash = 0
        revolver_repay = 0.0
        sweep = [0.0 for _ in tranches]
        excess = max(0, cash - min_cash)
        if excess > 0 and revolver > 0:
            revolver_repay = min(revolver, excess)
            revolver -= revolver_repay
            cash -= revolver_repay
            excess -= revolver_repay
        sweep_pool = excess * or0(inp["cashSweepPct"])
        for k, t in enumerate(tranches):
            if not truthy(t.get("sweep")) or sweep_pool <= 0:
                continue
            pay = min(bal[k], sweep_pool)
            sweep[k] = pay
            bal[k] -= pay
            sweep_pool -= pay
            cash -= pay
        total_debt = total(bal) + revolver
        net_debt = total_debt - cash

        for k, t in enumerate(tranches):
            diff = open_bal[k] - mandatory[k] - sweep[k] - bal[k]
            checks.append(
                {
                    "year": y,
                    "check": t["name"] + " balance roll-forward",
                    "ok": abs(diff) < EPS and bal[k] > -EPS,
                    "diff": diff,
                }
            )
        cash_roll = (
            open_cash
            + fcf
            - total(mandatory)
            - total(sweep)
            - revolver_repay
            + revolver_draw
            + equity_cure
            - cash
        )
        checks.append(
            {"year": y, "check": "Cash roll-forward", "ok": abs(cash_roll) < EPS, "diff": cash_roll}
        )
        checks.append(
            {"year": y, "check": "Cash balance not negative", "ok": cash > -EPS, "diff": min(0, cash)}
        )
        rev_roll = open_revolver + revolver_draw - revolver_repay - revolver
        checks.append(
            {
                "year": y,
                "check": "Revolver roll-forward and within commitment",
                "ok": abs(rev_roll) < EPS and revolver <= commitment + EPS,
                "diff": rev_roll,
            }
        )

        if revolver_draw > 0:
            warnings.append(f"Year {y}: revolver drawn {fixed(revolver_draw, 1)} to maintain minimum cash")
        if equity_cure > 0:
            warnings.append(
                f"Year {y}: cash shortfall of {fixed(equity_cure, 1)} funded by a sponsor equity cure"
            )
        if cash < min_cash - EPS and equity_cure == 0:
            warnings.append(f"Year {y}: cash below the minimum balance")

        years.append(
            {
                "year": y,
                "revenue": revenue,
                "growth": g,
                "ebitda": ebitda,
                "margin": margin,
                "da": da,
                "ebit": ebit,
                "trancheInterest": tranche_interest,
                "revolverInterest": revolver_interest,
                "interestExpense": interest_expense,
                "interestIncome": interest_income,
                "ebt": ebt,
                "nolUsed": nol_used,
                "tax": tax,
                "netIncome": net_income,
                "capex": capex,
                "dNwc": d_nwc,
                "fcf": fcf,
                "cashConversion": cash_conversion,
                "openBal": open_bal,
                "mandatory": mandatory,
                "sweep": sweep,
                "closeBal": list(bal),
                "openRevolver": open_revolver,
                "revolverDraw": revolver_draw,
                "revolverRepay": revolver_repay,
                "closeRevolver": revolver,
                "openCash": open_cash,
                "equityCure": equity_cure,
                "closeCash": cash,
                "totalDebt": total_debt,
                "netDebt": net_debt,
                "leverage": total_debt / ebitda if ebitda > 0 else None,
                "netLeverage": net_debt / ebitda if ebitda > 0 else None,
                "interestCoverage": safe_div(ebitda, interest_expense),
                "debtRepaid": total(mandatory) + total(sweep) + revolver_repay - revolver_draw,
            }
        )
        prev_rev = revenue

    # Exit
    last = years[n - 1]
    exit_ebitda = last["ebitda"]
    exit_ev = exit_ebitda * inp["exitMultiple"]
    exit_fees = exit_ev * or0(inp["exitFeePct"])
    exit_debt = last["totalDebt"]
    exit_cash = last["closeCash"]
    exit_net_debt = exit_debt - exit_cash
    exit_equity_raw = exit_ev - exit_fees - exit_net_debt
    exit_equity = max(0, exit_equity_raw)
    if exit_equity_raw < 0:
        warnings.append("Exit enterprise value is below net debt; equity is wiped out")
    sponsor_proceeds = exit_equity * sponsor_share
    management_proceeds = exit_equity - sponsor_proceeds
    cures = [yr["equityCure"] for yr in years]
    sponsor_cash_flows = [-sponsor_equity] + [
        -cures[i] + (sponsor_proceeds if i == n - 1 else 0) for i in range(n)
    ]
    sponsor_invested = sponsor_equity + total(cures)
    moic = sponsor_proceeds / sponsor_invested
    irr_res = irr(sponsor_cash_flows)

    entry_net_debt = new_debt - min_cash
    bridge: Json = {
        "ebitdaGrowth": (exit_ebitda - entry_ebitda) * entry_multiple_eff,
        "multipleExpansion": (inp["exitMultiple"] - entry_multiple_eff) * exit_ebitda,
        "debtPaydown": entry_net_debt - exit_net_debt,
        "fees": -(transaction_fees + financing_fees + exit_fees),
        "equityCures": -total(cures),
    }
    bridge["total"] = (
        bridge["ebitdaGrowth"]
        + bridge["multipleExpansion"]
        + bridge["debtPaydown"]
        + bridge["fees"]
        + bridge["equityCures"]
    )
    value_created = exit_equity_raw - total_equity_in - total(cures)

    checks.insert(
        0,
        {
            "year": 0,
            "check": "Sources equal uses",
            "ok": abs(total_sources - total_uses) < EPS,
            "diff": total_sources - total_uses,
        },
    )
    checks.append(
        {
            "year": n,
            "check": "Exit equity = EV − exit fees − net debt",
            "ok": abs(exit_equity_raw - (exit_ev - exit_fees - exit_debt + exit_cash)) < EPS,
            "diff": 0,
        }
    )
    checks.append(
        {
            "year": n,
            "check": "Sponsor + management proceeds = exit equity",
            "ok": abs(sponsor_proceeds + management_proceeds - exit_equity) < EPS,
            "diff": sponsor_proceeds + management_proceeds - exit_equity,
        }
    )
    checks.append(
        {
            "year": n,
            "check": "Value-creation bridge reconciles",
            "ok": exit_equity_raw < 0
            or abs(bridge["total"] - value_created) < 1e-6 * max(1, abs(value_created)),
            "diff": bridge["total"] - value_created,
        }
    )

    return {
        "ok": True,
        "errors": [],
        "warnings": warnings,
        "calcVersion": CALC_VERSION,
        "entry": {
            "entryEV": entry_ev,
            "entryMultiple": entry_multiple_eff,
            "entryEbitda": entry_ebitda,
            "entryRevenue": inp["entryRevenue"],
            "newDebt": new_debt,
            "leverage": new_debt / entry_ebitda,
            "transactionFees": transaction_fees,
            "financingFees": financing_fees,
            "equityPurchasePrice": equity_purchase_price,
            "existingNetDebt": existing_net_debt,
            "sponsorEquity": sponsor_equity,
            "rollover": inp["rollover"],
            "totalEquityIn": total_equity_in,
            "sponsorShare": sponsor_share,
            "debtShare": new_debt / total_uses,
            "equityShare": total_equity_in / total_uses,
        },
        "sources": sources,
        "uses": uses,
        "totalSources": total_sources,
        "totalUses": total_uses,
        "tranches": tranches,
        "years": years,
        "exit": {
            "exitEbitda": exit_ebitda,
            "exitMultiple": inp["exitMultiple"],
            "exitEV": exit_ev,
            "exitFees": exit_fees,
            "exitDebt": exit_debt,
            "exitCash": exit_cash,
            "exitNetDebt": exit_net_debt,
            "exitEquity": exit_equity,
            "exitEquityRaw": exit_equity_raw,
            "sponsorProceeds": sponsor_proceeds,
            "managementProceeds": management_proceeds,
        },
        "returns": {
            "moic": moic,
            "irr": irr_res["value"],
            "irrNote": irr_res["note"],
            "sponsorCashFlows": sponsor_cash_flows,
            "sponsorInvested": sponsor_invested,
            "cures": total(cures),
        },
        "bridge": bridge,
        "valueCreated": value_created,
        "checks": checks,
        "allChecksPass": all(c["ok"] for c in checks),
        "conventions": [
            f"Annual periods; sponsor equity invested at t = 0 and proceeds received at the end of year {n}.",
            "Cash-free, debt-free purchase: uses equal entry EV plus fees plus cash funded to the balance sheet.",
            "Interest charged on opening balances; no circular average-balance interest.",
            "Tax on positive pre-tax profit after loss carry-forwards; losses carried forward indefinitely.",
            "Mandatory amortisation first, then revolver repayment, then cash sweep in tranche order.",
            "Shortfalls below minimum cash draw the revolver; any remaining deficit is funded by a sponsor "
            "equity cure.",
            "IRR solved by bisection on annual sponsor cash flows; MOIC = sponsor proceeds ÷ total sponsor equity "
            "invested.",
        ],
    }


def break_even(lbo_result: Mapping[str, Any] | None, target_irr: float) -> Json | None:
    """Exit multiple and exit EBITDA needed for a target sponsor IRR, holding the plan and debt path fixed."""
    if not lbo_result or not lbo_result.get("ok"):
        return None
    n = len(lbo_result["years"])
    cfs = lbo_result["returns"]["sponsorCashFlows"]
    ex = lbo_result["exit"]
    fv = 0.0
    for t in range(n + 1):
        contrib = -(cfs[t] - ex["sponsorProceeds"]) if t == n else -cfs[t]
        fv += contrib * math.pow(1 + target_irr, n - t)
    required_equity = fv / lbo_result["entry"]["sponsorShare"]
    exit_fee_pct = ex["exitFees"] / ex["exitEV"] if ex["exitEV"] > 0 else 0
    required_ev = (required_equity + ex["exitNetDebt"]) / (1 - exit_fee_pct)
    return {
        "targetIrr": target_irr,
        "requiredSponsorProceeds": fv,
        "requiredEquity": required_equity,
        "requiredEV": required_ev,
        "requiredExitMultiple": safe_div(required_ev, ex["exitEbitda"]),
        "requiredExitEbitda": safe_div(required_ev, ex["exitMultiple"]),
        "currentExitMultiple": ex["exitMultiple"],
        "currentExitEbitda": ex["exitEbitda"],
    }


def _effective_entry_multiple(base: Mapping[str, Any]) -> Any:
    ov = base.get("entryEVOverride")
    return ov / base["entryEbitda"] if gt(ov, 0) else base.get("entryMultiple")


def apply_scenario(base: Mapping[str, Any], sc: Mapping[str, Any]) -> Json:
    """LBO inputs for a scenario. Scenarios shift the base plan, so the base case reproduces the LBO exactly."""
    hold: int = sc["holdYears"] if sc.get("holdYears") is not None else base["holdYears"]
    bh = base["holdYears"]
    bg = [nz(at(base["revenueGrowth"], i)) for i in range(hold)]
    bm = [nz(at(base["ebitdaMargin"], i)) for i in range(hold)]
    avg_g = total([nz(at(base["revenueGrowth"], i)) for i in range(bh)]) / bh
    base_exit_m = nz(at(base["ebitdaMargin"], bh - 1))
    d_g = sc["revenueGrowth"] - avg_g if is_num(sc.get("revenueGrowth")) else 0
    d_m = sc["exitMargin"] - base_exit_m if is_num(sc.get("exitMargin")) else 0
    base_lev = total([t["leverage"] for t in base["tranches"]])
    scale = sc["leverage"] / base_lev if is_num(sc.get("leverage")) and base_lev > 0 else 1
    mult_changed = (
        is_num(sc.get("entryMultiple")) and abs(sc["entryMultiple"] - _effective_entry_multiple(base)) > 1e-9
    )

    missing = object()  # JavaScript undefined: the key is left out, as JSON would drop it

    def pick(key: str) -> Any:
        return sc[key] if sc.get(key) is not None else base.get(key, missing)

    out = {
        **base,
        "holdYears": hold,
        "revenueGrowth": [g + d_g for g in bg],
        "ebitdaMargin": [m + d_m * (i + 1) / hold for i, m in enumerate(bm)],
        "entryMultiple": pick("entryMultiple"),
        "entryEVOverride": None if mult_changed else base.get("entryEVOverride", missing),
        "exitMultiple": pick("exitMultiple"),
        "tranches": [{**t, "leverage": t["leverage"] * scale} for t in base["tranches"]],
        "rateShift": or0(base.get("rateShift")) + or0(sc.get("rateShift")),
        "capexPct": pick("capexPct"),
        "nwcPct": pick("nwcPct"),
    }
    return {k: v for k, v in out.items() if v is not missing}


def chart_series(r: Mapping[str, Any] | None) -> Json | None:
    if not r or not r.get("ok"):
        return None
    fee_pct = r["exit"]["exitFees"] / r["exit"]["exitEV"] if r["exit"]["exitEV"] else 0
    share = r["entry"]["sponsorShare"]
    equity_by_exit_year = [
        max(0, y["ebitda"] * r["exit"]["exitMultiple"] * (1 - fee_pct) - y["netDebt"]) * share
        for y in r["years"]
    ]
    bridge = [
        {"label": "Equity invested", "value": r["entry"]["totalEquityIn"], "total": True},
        {"label": "EBITDA growth", "value": r["bridge"]["ebitdaGrowth"]},
        {"label": "Multiple change", "value": r["bridge"]["multipleExpansion"]},
        {"label": "Debt paydown", "value": r["bridge"]["debtPaydown"] - r["returns"]["cures"]},
        {"label": "Fees & costs", "value": r["bridge"]["fees"]},
        *([{"label": "Equity cures", "value": r["returns"]["cures"]}] if r["returns"]["cures"] else []),
        {"label": "Exit equity", "value": r["exit"]["exitEquityRaw"], "total": True},
    ]
    return {
        "equityByExitYear": equity_by_exit_year,
        "bridge": bridge,
        "debtByTranche": [
            {"name": t["name"], "values": [t["amount"], *(y["closeBal"][k] for y in r["years"])]}
            for k, t in enumerate(r["tranches"])
        ],
        "revolver": [0, *(y["closeRevolver"] for y in r["years"])],
        "leverage": [r["entry"]["leverage"], *(y["leverage"] for y in r["years"])],
        "interestCover": [y["interestCoverage"] for y in r["years"]],
    }


def max_entry_multiple(lbo_in: Mapping[str, Any], target_irr: float) -> float | None:
    """Highest entry EV/EBITDA at which the sponsor IRR still exceeds ``target_irr``."""

    def irr_at(m: float) -> float | None:
        r = run_lbo({**lbo_in, "entryMultiple": m, "entryEVOverride": None})
        return r["returns"]["irr"] if r["ok"] and is_num(r["returns"]["irr"]) else None

    ov = lbo_in.get("entryEVOverride")
    start = (
        ov / lbo_in["entryEbitda"]
        if is_num(ov) and ov > 0 and gt(lbo_in.get("entryEbitda"), 0)
        else lbo_in.get("entryMultiple")
    )
    if not is_num(start) or start <= 0:
        return None
    lo = hi = start
    v0 = irr_at(start)
    if v0 is None:
        return None
    if v0 > target_irr:
        for _ in range(60):
            hi *= 1.15
            v = irr_at(hi)
            if v is None or v <= target_irr:
                break
            lo = hi
            if hi > 200:
                return None
    else:
        for _ in range(60):
            prev = lo
            lo /= 1.15
            v = irr_at(lo)
            if v is None:
                # Below some price the deal cannot be modelled; find that floor between lo and prev.
                bad, good = lo, prev
                for _k in range(80):
                    mid = (bad + good) / 2
                    if irr_at(mid) is not None:
                        good = mid
                    else:
                        bad = mid
                lo, hi = good, prev
                break
            if v > target_irr:
                break
            hi = lo
    v_lo = irr_at(lo)
    if not (v_lo is not None and v_lo > target_irr):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        v = irr_at(mid)
        if v is not None and v > target_irr:
            lo = mid
        else:
            hi = mid
    return lo


def sensitivity(
    row_vals: Sequence[Any], col_vals: Sequence[Any], fn: Callable[[Any, Any], Any]
) -> list[list[Any]]:
    out = []
    for r in row_vals:
        row = []
        for c in col_vals:
            try:
                row.append(fn(r, c))
            except Exception:  # noqa: BLE001 (KCI returns null for any failing cell)
                row.append(None)
        out.append(row)
    return out


# ---------- screener ----------

DEFAULT_SCORING: Json = {
    "weights": {"quality": 25, "growth": 20, "valuation": 20, "cash": 15, "resilience": 10, "fit": 10},
    "ranges": {
        "margin": [0.05, 0.30],
        "growth": [-0.05, 0.20],
        "multiple": [14, 5],
        "conversion": [0.2, 0.9],
        "leverage": [5, 0],
    },
    "targetSectors": [],
    "targetGeographies": [],
}


def lerp_score(v: float, rng: Sequence[float]) -> float:
    a, b = rng
    return clamp(((v - a) / (b - a)) * 100, 0, 100)


def score_target(d: Mapping[str, Any], cfg: Mapping[str, Any] = DEFAULT_SCORING) -> Json:
    w_cfg, r_cfg = cfg["weights"], cfg["ranges"]
    ebitda = d.get("ebitda")
    margin = safe_div(ebitda, d.get("revenue"))
    mult = safe_div(d.get("ev"), ebitda) if d.get("ev") is not None and gt(ebitda, 0) else None
    lev = safe_div(d.get("netDebt"), ebitda) if d.get("netDebt") is not None and gt(ebitda, 0) else None
    conv = safe_div(ebitda - d["capex"], ebitda) if d.get("capex") is not None and gt(ebitda, 0) else None

    def pct(x: float) -> str:
        return fixed(x * 100, 1) + "%"

    comps: list[Json] = []
    if margin is None:
        comps.append(
            {
                "key": "quality",
                "label": "Business quality & profitability",
                "score": None,
                "why": "Revenue or EBITDA missing",
            }
        )
    else:
        comps.append(
            {
                "key": "quality",
                "label": "Business quality & profitability",
                "score": lerp_score(margin, r_cfg["margin"]),
                "value": margin,
                "why": f"EBITDA margin {pct(margin)} against a {pct(r_cfg['margin'][0])}–"
                f"{pct(r_cfg['margin'][1])} scale",
            }
        )
    rg = d.get("revenueGrowth")
    if not is_num(rg):
        comps.append({"key": "growth", "label": "Growth", "score": None, "why": "Revenue growth missing"})
    else:
        comps.append(
            {
                "key": "growth",
                "label": "Growth",
                "score": lerp_score(rg, r_cfg["growth"]),
                "value": rg,
                "why": f"Revenue growth {pct(rg)} against a {pct(r_cfg['growth'][0])}–"
                f"{pct(r_cfg['growth'][1])} scale",
            }
        )
    if mult is None:
        why = (
            "EBITDA not positive; multiple not meaningful"
            if ebitda is not None and not gt(ebitda, 0)
            else "Enterprise value or EBITDA missing"
        )
        comps.append({"key": "valuation", "label": "Valuation attractiveness", "score": None, "why": why})
    else:
        comps.append(
            {
                "key": "valuation",
                "label": "Valuation attractiveness",
                "score": lerp_score(mult, r_cfg["multiple"]),
                "value": mult,
                "why": f"{fixed(mult, 1)}x EV/EBITDA; {r_cfg['multiple'][1]}x scores 100, "
                f"{r_cfg['multiple'][0]}x scores 0",
            }
        )
    if conv is None:
        comps.append({"key": "cash", "label": "Cash generation", "score": None, "why": "Capex not provided"})
    else:
        comps.append(
            {
                "key": "cash",
                "label": "Cash generation",
                "score": lerp_score(conv, r_cfg["conversion"]),
                "value": conv,
                "why": f"Cash conversion (EBITDA − capex) / EBITDA of {pct(conv)}",
            }
        )
    if lev is None:
        comps.append(
            {
                "key": "resilience",
                "label": "Financial resilience",
                "score": None,
                "why": "Net debt or EBITDA missing",
            }
        )
    else:
        comps.append(
            {
                "key": "resilience",
                "label": "Financial resilience",
                "score": lerp_score(lev, r_cfg["leverage"]),
                "value": lev,
                "why": "Net cash position" if lev <= 0 else f"Net leverage {fixed(lev, 1)}x EBITDA",
            }
        )
    fit: float | None = None
    sectors = cfg.get("targetSectors") or []
    geos = cfg.get("targetGeographies") or []
    if is_num(d.get("strategicFit")):
        fit = clamp((d["strategicFit"] - 1) / 4 * 100, 0, 100)
        fit_why = f"Analyst fit rating {d['strategicFit']} of 5"
    elif sectors or geos:
        s = (50 if d.get("sector") in sectors else 0) if sectors else 25
        g = (50 if d.get("geography") in geos else 0) if geos else 25
        fit = s + g

        def word(x: int) -> str:
            return "in" if x == 50 else "outside" if x == 0 else "not set for"

        fit_why = f"Sector {word(s)} focus list; geography {word(g)} focus list"
    else:
        fit_why = "No fit rating and no focus sectors or geographies set"
    comps.append({"key": "fit", "label": "Strategic acquisition fit", "score": fit, "why": fit_why})
    w_sum = s_sum = w_all = 0.0
    for c in comps:
        c["weight"] = or0(w_cfg.get(c["key"]))
        w_all += c["weight"]
        if c["score"] is not None:
            w_sum += c["weight"]
            s_sum += c["weight"] * c["score"]
    for c in comps:
        c["contribution"] = (
            (c["weight"] * c["score"]) / w_sum if c["score"] is not None and w_sum > 0 else None
        )
    return {
        "total": s_sum / w_sum if w_sum > 0 else None,
        "components": comps,
        "coverage": w_sum / w_all if w_all > 0 else 0,
    }


def passes_filters(d: Mapping[str, Any], f: Mapping[str, Any]) -> Json:
    reasons: list[str] = []

    def chk(label: str, v: Any, test: Callable[[float], bool]) -> None:
        if v is None or not is_num(v):
            reasons.append(label + ": no data")
            return
        if not test(v):
            reasons.append(label)

    ebitda = d.get("ebitda")
    margin = safe_div(ebitda, d.get("revenue"))
    mult = safe_div(d.get("ev"), ebitda) if gt(ebitda, 0) else None
    lev = safe_div(d.get("netDebt"), ebitda) if gt(ebitda, 0) else None
    if is_num(f.get("minRevenue")):
        chk("Revenue below minimum", d.get("revenue"), lambda v: v >= f["minRevenue"])
    if is_num(f.get("maxRevenue")):
        chk("Revenue above maximum", d.get("revenue"), lambda v: v <= f["maxRevenue"])
    if is_num(f.get("minEbitda")):
        chk("EBITDA below minimum", ebitda, lambda v: v >= f["minEbitda"])
    if is_num(f.get("minMargin")):
        chk("Margin below minimum", margin, lambda v: v >= f["minMargin"])
    if is_num(f.get("minGrowth")):
        chk("Growth below minimum", d.get("revenueGrowth"), lambda v: v >= f["minGrowth"])
    if is_num(f.get("maxMultiple")):
        chk("Entry multiple above maximum", mult, lambda v: v <= f["maxMultiple"])
    if is_num(f.get("maxLeverage")):
        chk("Net leverage above maximum", lev, lambda v: v <= f["maxLeverage"])
    if f.get("sectors") and d.get("sector") not in f["sectors"]:
        reasons.append("Sector not selected")
    if f.get("geographies") and d.get("geography") not in f["geographies"]:
        reasons.append("Geography not selected")
    if f.get("verifiedOnly") and d.get("dataStatus") != "verified":
        reasons.append("Not verified data")
    # Known v1 defect (audit M2): compares free-text reporting periods as strings.
    if f.get("minPeriod") and (not d.get("reportingPeriod") or str(d["reportingPeriod"]) < f["minPeriod"]):
        reasons.append("Reporting period older than " + f["minPeriod"])
    return {"pass": not reasons, "reasons": reasons}


# ---------- IC recommendation ----------

DEFAULT_CRITERIA: Json = {
    "requireVerified": True,
    "minBaseIrr": 0.20,
    "minBaseMoic": 2.0,
    "minDownsideMoic": 1.0,
    "maxEntryLeverage": 6.0,
    "minScore": 55,
    "passBelowIrr": 0.12,
    "passBelowDownsideMoic": 0.7,
}


def recommend(m: Mapping[str, Any], c: Mapping[str, Any] = DEFAULT_CRITERIA) -> Json:
    """Proceed only when every criterion is met; missing data never counts as a pass."""
    checks: list[Json] = []

    def add(label: str, ok: bool, detail: str) -> None:
        checks.append({"label": label, "ok": ok, "detail": detail})

    def p(x: Any) -> str:
        return fixed(x * 100, 1) + "%" if is_num(x) else "n/a"

    def x(v: Any) -> str:
        return fixed(v, 2) + "x" if is_num(v) else "n/a"

    base_irr, base_moic, down_moic = m.get("baseIrr"), m.get("baseMoic"), m.get("downsideMoic")
    lev, score = m.get("entryLeverage"), m.get("score")
    add(
        "Base-case IRR at or above hurdle",
        is_num(base_irr) and base_irr >= c["minBaseIrr"],
        f"{p(base_irr)} vs {p(c['minBaseIrr'])} hurdle",
    )
    add(
        "Base-case MOIC at or above target",
        is_num(base_moic) and base_moic >= c["minBaseMoic"],
        f"{x(base_moic)} vs {x(c['minBaseMoic'])}",
    )
    add(
        "Downside MOIC protects capital",
        is_num(down_moic) and down_moic >= c["minDownsideMoic"],
        f"{x(down_moic)} vs {x(c['minDownsideMoic'])}",
    )
    add(
        "Entry leverage within limit",
        is_num(lev) and lev <= c["maxEntryLeverage"],
        f"{x(lev)} vs {x(c['maxEntryLeverage'])} maximum",
    )
    add(
        "Attractiveness score at or above minimum",
        is_num(score) and score >= c["minScore"],
        f"{fixed(score, 0)} vs {c['minScore']}" if is_num(score) else f"n/a vs {c['minScore']}",
    )
    add(
        "All model checks reconcile",
        bool(m.get("checksPass")),
        "Sources/uses, debt and cash reconcile"
        if m.get("checksPass")
        else "One or more reconciliation checks failed",
    )
    if c.get("requireVerified"):
        add(
            "Key figures checked against their sources",
            bool(m.get("dataVerified")),
            "Data label: verified"
            if m.get("dataVerified")
            else "Data label: " + (m.get("dataLabel") or "unverified"),
        )
    pass_trig = []
    if is_num(base_irr) and base_irr < c["passBelowIrr"]:
        pass_trig.append(f"base IRR {p(base_irr)} is below the {p(c['passBelowIrr'])} pass threshold")
    if is_num(down_moic) and down_moic < c["passBelowDownsideMoic"]:
        pass_trig.append(f"downside MOIC {x(down_moic)} is below {x(c['passBelowDownsideMoic'])}")
    if pass_trig:
        decision, rationale = "Pass", "Pass because " + " and ".join(pass_trig) + "."
    elif all(k["ok"] for k in checks):
        decision, rationale = "Proceed", f"All {len(checks)} criteria are met."
    else:
        failed = [k["label"].lower() for k in checks if not k["ok"]]
        decision, rationale = "Further Research", "Further work needed: " + "; ".join(failed) + "."
    return {"decision": decision, "rationale": rationale, "checks": checks}
