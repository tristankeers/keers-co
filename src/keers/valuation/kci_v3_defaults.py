"""Default assumptions, calculation version ``kci-v3``.

Version 3 keeps everything in ``kci-v2`` and closes the rest of the 22 Oct 2026 audit that v2 left open. The
engine formulas are unchanged except for one optional input (``interestCapPct`` in ``kci_v1.run_lbo``), which v1
and v2 never set, so their results are untouched.

1. **Interest deduction cap (audit H5).** Buyout tax follows the US section 163(j) rule in simplified form: net
   interest is deductible up to 30% of EBITDA each year, and the excess carries forward without limit. v1 and v2
   deducted all interest however high the leverage, which flattered heavily geared deals.
2. **Debt weight from the company's own balance sheet (audit H2, follow-up to v2).** The DCF's WACC weights debt
   by gross debt / (gross debt + market equity) instead of a fixed 30%. With either figure unknown the DCF is not
   run; there is no fallback to a house weight.
3. **Default-model verdict cap (audit H1).** A "Proceed" that rests on default assumptions becomes "Further
   Research": an analyst has to build the case before a buyout model can recommend proceeding.
4. **Buyout eligibility (audit M1).** Above 25x EV/EBITDA, or below a 10% EBITDA margin, a leveraged buyout on
   default assumptions is not meaningful, so the verdict is "Pass" and says why.
5. **No value per share from a negative enterprise value (audit H2).** When the DCF gives an enterprise value at
   or below zero, equity value and value per share are withheld rather than shown as negative numbers.
6. **IRR warning (audit L1).** When the sponsor's cash flows change sign more than once (equity cures followed by
   proceeds) the IRR may not be unique, and the result says so.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from keers.valuation import kci_v1 as K
from keers.valuation import kci_v2_defaults as V2
from keers.valuation.kci_v1 import Json, is_num

CALC_VERSION = "kci-v3"
ENGINE_VERSION = K.CALC_VERSION

INTEREST_CAP_PCT = 0.30  # of EBITDA (section 163(j) as restored for tax years from 2025; simplified)
MAX_BUYOUT_MULTIPLE = 25.0  # EV / EBITDA above which a default buyout is not meaningful
MIN_BUYOUT_MARGIN = 0.10  # EBITDA margin below which a default buyout is not meaningful

CHANGES = (
    *V2.CHANGES,
    f"Buyout interest deductible up to {INTEREST_CAP_PCT:.0%} of EBITDA a year; the excess carries forward",
    "DCF debt weight = gross debt / (gross debt + market equity); unknown capital structure means no DCF",
    "A Proceed on default assumptions is capped at Further Research until an analyst builds the case",
    f"No default buyout verdict above {MAX_BUYOUT_MULTIPLE:g}x EV/EBITDA or below a {MIN_BUYOUT_MARGIN:.0%} "
    "EBITDA margin (Pass: not a buyout candidate)",
    "DCF equity value and value per share withheld when enterprise value is zero or negative",
    "IRR flagged when the sponsor's cash flows change sign more than once",
)


def debt_weight(d: Mapping[str, Any]) -> float | None:
    """Gross debt / (gross debt + market equity), from the deal's market block. None when either is unknown."""
    debt = (d.get("market") or {}).get("debt")
    eq = V2.market_equity(d)
    if not is_num(debt) or debt < 0 or eq is None:
        return None
    return float(debt) / (float(debt) + eq)


def default_lbo(d: Mapping[str, Any]) -> Json:
    lbo = V2.default_lbo(d)
    lbo["interestCapPct"] = INTEREST_CAP_PCT
    lbo["assumptionNote"] += (
        f" Interest deductible up to {INTEREST_CAP_PCT:.0%} of EBITDA a year (simplified US section 163(j))."
    )
    return lbo


def default_dcf(d: Mapping[str, Any]) -> Json:
    dcf = V2.default_dcf(d)
    dcf["debtWeight"] = debt_weight(d)
    return dcf


def eligibility(d: Mapping[str, Any]) -> Json:
    """Whether a default buyout is meaningful for this company, with reasons. Unknown inputs are not a reason."""
    ev, ebitda, rev = d.get("ev"), d.get("ebitda"), d.get("revenue")
    reasons = []
    if is_num(ev) and is_num(ebitda) and ebitda > 0 and ev / ebitda > MAX_BUYOUT_MULTIPLE:
        reasons.append(f"EV/EBITDA of {ev / ebitda:.1f}x is above {MAX_BUYOUT_MULTIPLE:g}x")
    if is_num(rev) and rev > 0 and is_num(ebitda) and ebitda / rev < MIN_BUYOUT_MARGIN:
        reasons.append(f"EBITDA margin of {ebitda / rev:.2%} is below {MIN_BUYOUT_MARGIN:.0%}")
    return {"eligible": not reasons, "reasons": reasons}


def sign_changes(flows: Sequence[float]) -> int:
    signs = [f > 0 for f in flows if f != 0]
    return sum(1 for a, b in zip(signs, signs[1:], strict=False) if a != b)


def _withhold_negative_ev(dcf: Json) -> None:
    if not dcf.get("ok"):
        return
    for key, t in list(dcf.get("terminal", {}).items()):
        if isinstance(t, dict) and is_num(t.get("ev")) and t["ev"] <= 0:
            t.update(equity=None, perShare=None)
            dcf["warnings"].append(
                f"{key} terminal method: enterprise value is {t['ev']:,.1f}, so no equity value or value per "
                "share is shown"
            )
    prim = dcf.get("primary")
    if isinstance(prim, dict) and is_num(prim.get("ev")) and prim["ev"] <= 0:
        prim.update(equity=None, perShare=None)


def analyse(deal: Mapping[str, Any], settings: Mapping[str, Any] | None = None) -> Json:
    """v2's analysis with v3's defaults and verdict rules. Custom models saved on the deal are used as they are."""
    a = V2.analyse_with(deal, settings, default_lbo, V2.default_scenarios, default_dcf, CALC_VERSION, CHANGES)
    _withhold_negative_ev(a["dcf"])
    base = a["scenarios"]["base"]["result"]
    if base.get("ok") and sign_changes(base["returns"]["sponsorCashFlows"]) > 1:
        base["warnings"].append(
            "Sponsor cash flows change sign more than once (equity cures, then proceeds), so the IRR may not be "
            "unique; read it with the MOIC"
        )
    rec = copy.deepcopy(a["rec"])
    custom = bool(deal.get("lbo"))
    elig = eligibility(deal)
    a["eligibility"] = elig
    rules = []
    if not custom and not elig["eligible"]:
        rec["decision"] = "Pass"
        rec["rationale"] = (
            "Not a buyout candidate on default assumptions: " + "; ".join(elig["reasons"]) + "."
        )
        rules.append("eligibility")
    elif not custom and rec["decision"] == "Proceed":
        rec["decision"] = "Further Research"
        rec["rationale"] = (
            "Every criterion is met, but on default assumptions only; an analyst has to build the case before "
            "this can be a Proceed."
        )
        rules.append("default-model cap")
    if rules:
        rec["v2Decision"] = a["rec"]["decision"]
        rec["rulesApplied"] = rules
    a["rec"] = rec
    return a


def summarise(deals: Sequence[Mapping[str, Any]]) -> list[Json]:
    """Per-company headline outputs, as ``kci_v2_defaults.summarise`` plus tax paid and eligibility."""
    out = []
    for d in sorted(deals, key=lambda x: x["id"]):
        a = analyse(d)
        dcf, lbo = a["dcf"], a["lbo"]
        base = a["scenarios"]["base"]["result"]
        out.append(
            {
                "id": d["id"],
                "ticker": d["ticker"],
                "customModel": bool(d.get("lbo")),
                "entryMultiple": lbo["entry"]["entryMultiple"] if lbo["ok"] else None,
                "exitMultiple": a["lboIn"].get("exitMultiple"),
                "baseIrr": base["returns"]["irr"] if base["ok"] else None,
                "baseMoic": base["returns"]["moic"] if base["ok"] else None,
                "taxPaid": sum(y["tax"] for y in base["years"]) if base["ok"] else None,
                "interestCarriedForward": base["interestCap"]["carryForwardAtExit"]
                if base["ok"] and "interestCap" in base
                else None,
                "lboErrors": [] if lbo["ok"] else lbo["errors"],
                "wacc": a["wacc"]["value"] if a["wacc"]["ok"] else None,
                "debtWeight": a["dcfIn"].get("debtWeight"),
                "dcfEv": dcf["primary"]["ev"] if dcf["ok"] else None,
                "dcfErrors": [] if dcf["ok"] else dcf["errors"],
                "score": a["score"]["total"],
                "eligible": a["eligibility"]["eligible"],
                "decision": a["rec"]["decision"],
            }
        )
    return out


__all__ = [
    "CALC_VERSION",
    "INTEREST_CAP_PCT",
    "analyse",
    "debt_weight",
    "default_dcf",
    "default_lbo",
    "eligibility",
    "sign_changes",
    "summarise",
]
