"""KCI's default assumptions and per-company analysis, version ``kci-v1``.

A port of ``legacy/kci/model_setup.js`` (``defaultLBO``, ``defaultDCF``, ``defaultScenarios`` and
``analyse`` from KCI's app). These are the defaults the audit found wanting: buyouts priced at the
market multiple with no takeover premium, and a DCF that holds today's capex ratio for ever with one
beta and a 2% size premium for every company. They are reproduced exactly so that version 2 can be
measured against them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from keers.valuation import kci_v1 as K
from keers.valuation.kci_v1 import Json, at, gt, is_num, nz, total

_UNDEF = object()


def to_fixed(v: float, dp: int) -> float:
    """JavaScript ``+v.toFixed(dp)``: round the exact binary value half away from zero."""
    q = Decimal(v).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    return float(q)


def r1(v: Any, dp: int = 4) -> Any:
    return to_fixed(v, dp) if is_num(v) else v


def _pos(v: Any) -> float | None:
    return v if is_num(v) and v > 0 else None


def profile_rates(d: Mapping[str, Any]) -> Json:
    rev = _pos(d.get("revenue"))
    ebitda, ebit, capex = d.get("ebitda"), d.get("ebit"), d.get("capex")
    da = (
        min(0.5, (ebitda - ebit) / rev) if rev and is_num(ebitda) and is_num(ebit) and ebitda > ebit else None
    )
    cx = min(0.6, capex / rev) if rev and is_num(capex) and capex >= 0 else None
    lbo_defaults = d.get("lboDefaults")
    tax = lbo_defaults["taxRate"] if lbo_defaults and is_num(lbo_defaults.get("taxRate")) else None
    return {
        "daPct": da if da is not None else 0.03,
        "capexPct": cx if cx is not None else 0.03,
        "taxRate": tax if tax is not None else 0.25,
        "daFrom": da is not None,
        "capexFrom": cx is not None,
        "taxFrom": tax is not None,
    }


def default_lbo(d: Mapping[str, Any]) -> Json:
    rev = _pos(d.get("revenue"))
    ebitda = _pos(d.get("ebitda"))
    margin = ebitda / rev if rev and ebitda else 0.15
    mult = r1(d["ev"] / ebitda, 2) if is_num(d.get("ev")) and ebitda else 8
    rg = d.get("revenueGrowth")
    g = max(-0.05, min(0.2, rg)) if is_num(rg) else 0.05
    hold = 5
    pr = profile_rates(d)
    net_debt = d.get("netDebt")
    debt_x = min(4, 0.6 * mult)
    if pr["taxFrom"]:
        tax_note = f"at {round(pr['taxRate'] * 100)}% (set on the record: {d['lboDefaults'].get('taxNote') or 'see notes'})"
    else:
        tax_note = "at 25%"
    growth_note = (
        "recorded revenue growth (capped at −5% to 20%) fading to 3% by exit"
        if is_num(rg)
        else "revenue growth of 5% fading to 3% by exit because none is recorded"
    )
    return {
        "entryRevenue": rev,
        "entryEbitda": ebitda,
        "entryMultiple": mult,
        "entryEVOverride": None,
        "existingDebt": net_debt if is_num(net_debt) and net_debt > 0 else 0,
        "existingCash": -net_debt if is_num(net_debt) and net_debt < 0 else 0,
        "transactionFeePct": 0.025,
        "financingFeePct": 0.025,
        "minCash": r1(rev * 0.02, 2) if rev else 0,
        "rollover": 0,
        # 4.0x EBITDA (3x senior, 1x subordinated), capped so debt never exceeds 60% of the price.
        "tranches": [
            {
                "name": "Senior term loan",
                "leverage": r1(0.75 * debt_x, 3),
                "rate": 0.085,
                "amortPct": 0.05,
                "sweep": True,
            },
            {
                "name": "Subordinated notes",
                "leverage": r1(0.25 * debt_x, 3),
                "rate": 0.12,
                "amortPct": 0,
                "sweep": False,
            },
        ],
        "revolver": {"commitment": r1(ebitda * 0.5, 2) if ebitda else 0, "rate": 0.08},
        "cashSweepPct": 0.75,
        "interestOnCashRate": 0.02,
        "holdYears": hold,
        # Recorded growth fades in a straight line to 3% (0% for a shrinking business) by the exit year.
        "revenueGrowth": [
            r1(g + (max(0, min(g, 0.03)) - g) * (i / (hold - 1) if hold > 1 else 0)) for i in range(hold)
        ],
        # No margin expansion by default.
        "ebitdaMargin": [r1(margin)] * hold,
        "daPct": r1(pr["daPct"]),
        "capexPct": r1(pr["capexPct"]),
        "nwcPct": 0.1,
        "taxRate": pr["taxRate"],
        "exitMultiple": mult,
        "exitFeePct": 0.01,
        "assumptionNote": (
            "Defaults are starting assumptions generated from the target profile: debt of 4.0x EBITDA or 60% of "
            f"the price if lower, margin held flat, exit at the entry multiple, {growth_note}; D&A "
            f"{'from EBITDA less EBIT' if pr['daFrom'] else 'at 3% of revenue'}, capex "
            f"{'as reported' if pr['capexFrom'] else 'at 3% of revenue'}, tax {tax_note}. "
            "Edit every input before relying on outputs."
        ),
    }


def _fit(a: Any, n: int, fill: float) -> list[Any]:
    arr = list(a[:n]) if isinstance(a, list) else []
    while len(arr) < n:
        arr.append(arr[-1] if arr else fill)
    return arr


def fit_years(lbo: Mapping[str, Any]) -> Json:
    n = lbo["holdYears"]
    return {
        **lbo,
        "revenueGrowth": _fit(lbo.get("revenueGrowth"), n, 0.05),
        "ebitdaMargin": _fit(lbo.get("ebitdaMargin"), n, 0.15),
    }


def default_scenarios(lbo: Mapping[str, Any]) -> Json:
    hold = lbo["holdYears"]
    g = total(lbo["revenueGrowth"]) / len(lbo["revenueGrowth"])
    m = lbo["ebitdaMargin"][hold - 1]
    lev = total([t["leverage"] for t in lbo["tranches"]])
    ov = lbo.get("entryEVOverride")
    eff_mult = (
        ov / lbo["entryEbitda"]
        if is_num(ov) and ov > 0 and gt(lbo.get("entryEbitda"), 0)
        else lbo["entryMultiple"]
    )
    base = {
        "revenueGrowth": g,
        "exitMargin": m,
        "entryMultiple": eff_mult,
        "exitMultiple": lbo["exitMultiple"],
        "leverage": lev,
        "rateShift": 0,
        "holdYears": hold,
        "capexPct": lbo.get("capexPct"),
        "nwcPct": lbo.get("nwcPct"),
    }
    return {
        "downside": {
            **base,
            "revenueGrowth": r1(g - 0.04),
            "exitMargin": r1(m - 0.02),
            "exitMultiple": r1(lbo["exitMultiple"] - 1.5, 2),
            "rateShift": 0.015,
            "capexPct": r1(lbo["capexPct"] + 0.005),
        },
        "base": base,
        "upside": {
            **base,
            "revenueGrowth": r1(g + 0.03),
            "exitMargin": r1(m + 0.015),
            "exitMultiple": r1(lbo["exitMultiple"] + 1, 2),
        },
    }


def default_dcf(d: Mapping[str, Any]) -> Json:
    rev, ebitda = d.get("revenue"), d.get("ebitda")
    margin = ebitda / rev if is_num(rev) and is_num(ebitda) and rev > 0 else 0.15
    rg = d.get("revenueGrowth")
    g = max(-0.2, min(0.3, rg)) if is_num(rg) else 0.05
    pr = profile_rates(d)
    return {
        "baseRevenue": rev,
        "years": 5,
        "revenueGrowth": [r1(v) for v in (g, g * 0.9, g * 0.8, g * 0.7, g * 0.6)],
        "ebitdaMargin": [r1(margin)] * 5,
        "daPct": r1(pr["daPct"]),
        "capexPct": r1(pr["capexPct"]),
        "nwcPct": 0.1,
        "taxRate": pr["taxRate"],
        "waccMode": "build",
        "wacc": 0.11,
        "riskFree": 0.045,
        "beta": 1.1,
        "erp": 0.055,
        "sizePremium": 0.02,
        "preTaxCostOfDebt": 0.08,
        "debtWeight": 0.3,
        "terminalMethod": "both",
        "terminalGrowth": 0.02,
        "exitMultiple": r1(d["ev"] / ebitda, 2) if is_num(d.get("ev")) and gt(ebitda, 0) else 8,
        "midYear": True,
        "netDebt": d.get("netDebt"),
        "otherClaims": 0,
        "nonOperatingAssets": 0,
        "sharesOutstanding": None,
    }


def fit_dcf(dcf: Mapping[str, Any]) -> Json:
    n = dcf["years"]

    def fit(a: Any) -> list[Any]:
        arr = list(a[:n]) if isinstance(a, list) else [a]
        while len(arr) < n:
            arr.append(nz(arr[-1]) if arr else 0)
        return arr

    return {
        **dcf,
        "revenueGrowth": fit(dcf.get("revenueGrowth")),
        "ebitdaMargin": fit(dcf.get("ebitdaMargin")),
    }


def dcf_wacc(dcf: Mapping[str, Any]) -> Json:
    if dcf.get("waccMode") == "direct":
        ok = is_num(dcf.get("wacc"))
        return {"ok": ok, "value": dcf.get("wacc"), "errors": [] if ok else ["WACC is required"]}
    return K.wacc(
        {
            k: dcf.get(k)
            for k in ("riskFree", "beta", "erp", "sizePremium", "preTaxCostOfDebt", "taxRate", "debtWeight")
        }
    )


SC_KEYS = [
    "revenueGrowth",
    "exitMargin",
    "entryMultiple",
    "exitMultiple",
    "leverage",
    "rateShift",
    "holdYears",
    "capexPct",
    "nwcPct",
]


def scenario_base_drift(saved: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Scenario fields whose saved base no longer matches the current LBO (JavaScript strict equality)."""
    out = []
    for k in SC_KEYS:
        a, b = saved.get(k, _UNDEF), current.get(k, _UNDEF)
        if is_num(a) and is_num(b):
            if abs(a - b) > 1e-9:
                out.append(k)
        elif a is not b and a != b:
            out.append(k)
    return out


def analyse(deal: Mapping[str, Any], settings: Mapping[str, Any] | None = None) -> Json:
    """Everything KCI shows for one company: LBO, three scenarios, score, DCF, comps and the IC verdict."""
    settings = settings or {"scoring": K.DEFAULT_SCORING, "criteria": K.DEFAULT_CRITERIA}
    lbo_in = fit_years(deal.get("lbo") or default_lbo(deal))
    lbo = K.run_lbo(lbo_in)
    sc_in = deal.get("scenarios") or default_scenarios(lbo_in)
    drift = (
        scenario_base_drift(deal["scenarios"]["base"], default_scenarios(lbo_in)["base"])
        if deal.get("scenarios")
        else []
    )
    scenarios: Json = {}
    for k in ("downside", "base", "upside"):
        inp = fit_years(K.apply_scenario(lbo_in, sc_in[k]))
        scenarios[k] = {"inputs": sc_in[k], "model": inp, "result": K.run_lbo(inp)}
    score = K.score_target(deal, settings["scoring"])
    dcf_in = fit_dcf(deal.get("dcf") or default_dcf(deal))
    w = dcf_wacc(dcf_in)
    dcf = (
        K.run_dcf({**dcf_in, "wacc": w["value"]})
        if w["ok"]
        else {"ok": False, "errors": w["errors"], "warnings": []}
    )
    comps = K.comps_analysis(
        deal.get("comps") or [],
        target={
            "revenue": deal.get("revenue"),
            "ebitda": deal.get("ebitda"),
            "ebit": deal.get("ebit"),
            "netIncome": deal.get("netIncome"),
            "netDebt": deal.get("netDebt"),
        },
    )
    base, down = scenarios["base"]["result"], scenarios["downside"]["result"]
    data_status = deal.get("dataStatus")
    rec = K.recommend(
        {
            "baseIrr": base["returns"]["irr"] if base["ok"] else None,
            "baseMoic": base["returns"]["moic"] if base["ok"] else None,
            "downsideMoic": down["returns"]["moic"] if down["ok"] else None,
            "entryLeverage": base["entry"]["leverage"] if base["ok"] else None,
            "score": score["total"],
            "checksPass": bool(base["ok"] and base["allChecksPass"] and lbo["ok"] and lbo["allChecksPass"]),
            "dataVerified": data_status == "verified",
            "dataLabel": "DEMO DATA" if data_status == "demo" else data_status,
        },
        settings["criteria"],
    )
    return {
        "lboIn": lbo_in,
        "lbo": lbo,
        "scIn": sc_in,
        "scenarios": scenarios,
        "scenarioDrift": drift,
        "score": score,
        "dcfIn": dcf_in,
        "wacc": w,
        "dcf": dcf,
        "comps": comps,
        "rec": rec,
        "calcVersion": K.CALC_VERSION,
    }


def summarise(deals: Sequence[Mapping[str, Any]]) -> list[Json]:
    """Per-company outputs in the same shape as ``legacy/kci/harness.js``."""

    def pick(r: Mapping[str, Any]) -> Json:
        if r and r.get("ok"):
            return {
                "irr": r["returns"]["irr"],
                "moic": r["returns"]["moic"],
                "entryLeverage": r["entry"]["leverage"],
                "sponsorEquity": r["entry"]["sponsorEquity"],
                "exitEquity": r["exit"]["exitEquityRaw"],
                "exitNetDebt": r["exit"]["exitNetDebt"],
                "cures": r["returns"]["cures"],
                "allChecksPass": r["allChecksPass"],
            }
        return {"ok": False, "errors": r["errors"] if r else ["no result"]}

    out = []
    for d in sorted(deals, key=lambda x: x["id"]):
        a = analyse(d)
        dcf = a["dcf"]
        out.append(
            {
                "id": d["id"],
                "ticker": d["ticker"],
                "dataStatus": d["dataStatus"],
                "customModel": bool(d.get("lbo")),
                "lbo": pick(a["lbo"]),
                "downside": pick(a["scenarios"]["downside"]["result"]),
                "base": pick(a["scenarios"]["base"]["result"]),
                "upside": pick(a["scenarios"]["upside"]["result"]),
                "dcf": {
                    "wacc": a["wacc"]["value"],
                    "ev": dcf["primary"]["ev"],
                    "equity": dcf["primary"]["equity"],
                    "perShare": dcf["primary"]["perShare"],
                    "tvShareOfEV": dcf["primary"]["tvShareOfEV"],
                }
                if dcf["ok"]
                else {"ok": False, "errors": dcf["errors"]},
                "score": a["score"]["total"],
                "decision": a["rec"]["decision"],
            }
        )
    return out


__all__ = [
    "analyse",
    "at",
    "default_dcf",
    "default_lbo",
    "default_scenarios",
    "dcf_wacc",
    "fit_dcf",
    "fit_years",
    "profile_rates",
    "r1",
    "scenario_base_drift",
    "summarise",
    "to_fixed",
]
