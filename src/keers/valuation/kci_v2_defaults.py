"""Default assumptions, calculation version ``kci-v2``.

The engine itself (``kci_v1``: the LBO, DCF, WACC and comps formulas) is unchanged. Version 2 replaces three
default assumptions the 22 Oct 2026 audit found wanting. ``kci_v1_defaults`` stays exactly as it was, so every
change can be measured against it.

1. **Takeover premium.** A buyer of a listed company pays more than the share price. The default buyout is
   priced at market enterprise value plus ``TAKEOVER_PREMIUM`` of the market value of equity. The exit is still
   at the market multiple, so the premium is a cost the deal has to earn back (v1 bought at market, which made
   any cheap stock look like a buyout).
2. **A beta for each company.** The DCF uses the company's own beta (``deal["beta"]``, measured from prices by
   ``keers.data.beta``) and the live 10-year Treasury yield when the deal carries one. With no beta the DCF is not
   run; there is no fallback to a house beta. The 2% size premium now applies only below $2bn market cap
   (1% from $2bn to $10bn, none above).
3. **Capex fades.** Today's capex ratio is held for year 1, then fades in a straight line to the D&A ratio by
   the final year, so the terminal year reinvests roughly what wears out. A company already spending less than
   its D&A keeps its own ratio. v1 held a peak-investment capex ratio for ever, which sent the DCF negative for
   companies in a build-out (Oracle, Meta, Alphabet).

Everything else is v1's default and is listed in ``analyse``'s ``changes`` field.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from keers.valuation import kci_v1 as K
from keers.valuation import kci_v1_defaults as V1
from keers.valuation.kci_v1 import Json, gt, is_num, total
from keers.valuation.kci_v1_defaults import dcf_wacc, fit_dcf, fit_years, r1, scenario_base_drift

CALC_VERSION = "kci-v2"
ENGINE_VERSION = K.CALC_VERSION

TAKEOVER_PREMIUM = 0.30  # of market equity value; US take-privates have typically paid about 25-40%
RISK_FREE_FALLBACK = 0.045  # v1's figure, used only when the deal carries no live Treasury yield
# (market cap floor in USD millions, size premium), checked in order
SIZE_PREMIUM_BANDS: tuple[tuple[float, float], ...] = ((10_000.0, 0.0), (2_000.0, 0.01), (0.0, 0.02))
SIZE_PREMIUM_UNKNOWN = 0.02

CHANGES = (
    f"Buyout priced at market EV plus a {TAKEOVER_PREMIUM:.0%} premium on market equity; exit at the market multiple",
    "DCF uses the company's own beta and the live 10-year Treasury yield; no beta means no DCF",
    "Size premium 0% above $10bn market cap, 1% from $2bn to $10bn, 2% below $2bn",
    "Capex fades in a straight line from today's ratio (year 1) to the D&A ratio (final year)",
)


def market_equity(d: Mapping[str, Any]) -> float | None:
    """Market value of equity: the recorded market cap, else EV less net debt. None when neither is known."""
    mc = (d.get("market") or {}).get("marketCap")
    if is_num(mc) and mc > 0:
        return float(mc)
    ev, nd = d.get("ev"), d.get("netDebt")
    if is_num(ev) and is_num(nd) and ev - nd > 0:
        return float(ev - nd)
    return None


def size_premium(market_cap: float | None) -> float:
    if market_cap is None:
        return SIZE_PREMIUM_UNKNOWN
    for floor, premium in SIZE_PREMIUM_BANDS:
        if market_cap >= floor:
            return premium
    return SIZE_PREMIUM_UNKNOWN


def capex_path(capex_pct: float, da_pct: float, years: int) -> list[float]:
    """Year-by-year capex as a share of revenue: today's ratio in year 1, D&A's ratio in the final year."""
    if capex_pct <= da_pct or years < 2:
        return [r1(capex_pct)] * years
    return [r1(capex_pct + (da_pct - capex_pct) * i / (years - 1)) for i in range(years)]


def takeover_entry(d: Mapping[str, Any]) -> Json:
    """Entry price with the premium. ``entryEV`` is None when market EV, EBITDA or equity value is unknown."""
    ev, ebitda, eq = d.get("ev"), d.get("ebitda"), market_equity(d)
    if not (is_num(ev) and is_num(ebitda) and ebitda > 0 and eq is not None):
        return {"entryEV": None, "premium": None, "marketMultiple": None, "entryMultiple": None}
    premium = TAKEOVER_PREMIUM * eq
    entry_ev = ev + premium
    return {
        "entryEV": r1(entry_ev, 2),
        "premium": r1(premium, 2),
        "marketMultiple": r1(ev / ebitda, 2),
        "entryMultiple": r1(entry_ev / ebitda, 2),
    }


def default_lbo(d: Mapping[str, Any]) -> Json:
    lbo = V1.default_lbo(d)
    entry = takeover_entry(d)
    pr = V1.profile_rates(d)
    if entry["entryEV"] is None:
        # No market price to add a premium to: leave the price unset so the LBO refuses to run.
        lbo.update(entryMultiple=None, entryEVOverride=None, exitMultiple=None)
    else:
        debt_x = min(4, 0.6 * entry["entryMultiple"])
        lbo.update(entryMultiple=entry["entryMultiple"], entryEVOverride=entry["entryEV"],
                   exitMultiple=entry["marketMultiple"])  # fmt: skip
        lbo["tranches"][0]["leverage"] = r1(0.75 * debt_x, 3)
        lbo["tranches"][1]["leverage"] = r1(0.25 * debt_x, 3)
    lbo["capexPct"] = capex_path(pr["capexPct"], pr["daPct"], lbo["holdYears"])
    lbo["takeover"] = entry
    lbo["assumptionNote"] = (
        f"kci-v2 defaults: entry at market EV plus a {TAKEOVER_PREMIUM:.0%} premium on market equity, exit at "
        "the market multiple, debt of 4.0x EBITDA or 60% of the price if lower, margin held flat, capex fading "
        "from today's ratio to D&A by exit. Edit every input before relying on outputs."
    )
    return lbo


def _shift(v: Any, by: float) -> Any:
    return [r1(x + by) for x in v] if isinstance(v, list) else r1(v + by)


def default_scenarios(lbo: Mapping[str, Any]) -> Json:
    """As v1, with the downside's extra capex applied to every year of the capex path."""
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
    exit_m = lbo["exitMultiple"]
    base = {
        "revenueGrowth": g,
        "exitMargin": m,
        "entryMultiple": eff_mult,
        "exitMultiple": exit_m,
        "leverage": lev,
        "rateShift": 0,
        "holdYears": hold,
        "capexPct": lbo.get("capexPct"),
        "nwcPct": lbo.get("nwcPct"),
    }
    known = is_num(exit_m)
    return {
        "downside": {
            **base,
            "revenueGrowth": r1(g - 0.04),
            "exitMargin": r1(m - 0.02),
            "exitMultiple": r1(exit_m - 1.5, 2) if known else None,
            "rateShift": 0.015,
            "capexPct": _shift(lbo["capexPct"], 0.005),
        },
        "base": base,
        "upside": {
            **base,
            "revenueGrowth": r1(g + 0.03),
            "exitMargin": r1(m + 0.015),
            "exitMultiple": r1(exit_m + 1, 2) if known else None,
        },
    }


def default_dcf(d: Mapping[str, Any]) -> Json:
    dcf = V1.default_dcf(d)
    pr = V1.profile_rates(d)
    beta = d.get("beta")
    rf = d.get("riskFree")
    dcf.update(
        beta=beta if is_num(beta) else None,
        riskFree=rf if is_num(rf) else RISK_FREE_FALLBACK,
        sizePremium=size_premium(market_equity(d)),
        capexPct=capex_path(pr["capexPct"], pr["daPct"], dcf["years"]),
    )
    return dcf


def analyse(deal: Mapping[str, Any], settings: Mapping[str, Any] | None = None) -> Json:
    """v1's analysis with v2's defaults. Custom models saved on the deal are used as they are."""
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
        target={k: deal.get(k) for k in ("revenue", "ebitda", "ebit", "netIncome", "netDebt")},
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
        "calcVersion": CALC_VERSION,
        "engineVersion": ENGINE_VERSION,
        "customModel": {"lbo": bool(deal.get("lbo")), "dcf": bool(deal.get("dcf"))},
        "changes": list(CHANGES),
    }


def summarise(deals: Sequence[Mapping[str, Any]]) -> list[Json]:
    """Per-company headline outputs, in the shape of ``kci_v1_defaults.summarise`` plus the premium paid."""
    out = []
    for d in sorted(deals, key=lambda x: x["id"]):
        a = analyse(d)
        dcf, lbo = a["dcf"], a["lbo"]
        out.append(
            {
                "id": d["id"],
                "ticker": d["ticker"],
                "customModel": bool(d.get("lbo")),
                "entryMultiple": lbo["entry"]["entryMultiple"] if lbo["ok"] else None,
                "exitMultiple": a["lboIn"].get("exitMultiple"),
                "baseIrr": a["scenarios"]["base"]["result"]["returns"]["irr"]
                if a["scenarios"]["base"]["result"]["ok"]
                else None,
                "baseMoic": a["scenarios"]["base"]["result"]["returns"]["moic"]
                if a["scenarios"]["base"]["result"]["ok"]
                else None,
                "lboErrors": [] if lbo["ok"] else lbo["errors"],
                "wacc": a["wacc"]["value"] if a["wacc"]["ok"] else None,
                "dcfEv": dcf["primary"]["ev"] if dcf["ok"] else None,
                "dcfErrors": [] if dcf["ok"] else dcf["errors"],
                "score": a["score"]["total"],
                "decision": a["rec"]["decision"],
            }
        )
    return out


__all__ = [
    "CALC_VERSION",
    "TAKEOVER_PREMIUM",
    "analyse",
    "capex_path",
    "default_dcf",
    "default_lbo",
    "default_scenarios",
    "market_equity",
    "size_premium",
    "summarise",
    "takeover_entry",
]
