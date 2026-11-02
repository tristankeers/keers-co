"""kci-v3: interest deduction cap, debt weight, verdict rules. kci-v1 and kci-v2 must be untouched."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from keers.valuation import kci_v1 as K
from keers.valuation import kci_v2_defaults as V2
from keers.valuation import kci_v3_defaults as V3

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads((ROOT / "legacy" / "kci" / "seed.json").read_text())["deals"]
LAX = {"scoring": K.DEFAULT_SCORING, "criteria": {
    "requireVerified": False, "minBaseIrr": 0.0, "minBaseMoic": 0.0, "minDownsideMoic": 0.0,
    "maxEntryLeverage": 99.0, "minScore": 0, "passBelowIrr": -1.0, "passBelowDownsideMoic": 0.0,
}}  # fmt: skip


def deal(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": "us-exw", "ticker": "EXW", "dataStatus": "filed",
        "revenue": 1000.0, "ebitda": 200.0, "ebit": 150.0, "netIncome": 90.0, "capex": 120.0,
        "ev": 2000.0, "netDebt": 400.0, "revenueGrowth": 0.06,
        "market": {"marketCap": 1600.0, "debt": 500.0}, "beta": 1.2, "riskFree": 0.042,
    }  # fmt: skip
    d.update(over)
    return d


def lbo(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "entryEbitda": 100.0, "entryRevenue": 500.0, "entryMultiple": 10.0, "exitMultiple": 10.0, "holdYears": 5,
        "revenueGrowth": 0.03, "ebitdaMargin": 0.2, "daPct": 0.04, "capexPct": 0.04, "nwcPct": 0.0, "taxRate": 0.25,
        "tranches": [{"name": "Senior", "leverage": 6.0, "rate": 0.09, "amortPct": 0.0, "sweep": True}],
    }  # fmt: skip
    base.update(over)
    return base


# interest deduction cap ---------------------------------------------------------------------------------------
def test_unset_cap_changes_nothing() -> None:
    a, b = K.run_lbo(lbo()), K.run_lbo(lbo(interestCapPct=None))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "interestCap" not in a and "interestDisallowed" not in a["years"][0]


def test_cap_disallows_interest_above_30pct_of_ebitda_and_carries_it_forward() -> None:
    free, capped = K.run_lbo(lbo()), K.run_lbo(lbo(interestCapPct=0.30))
    y1 = capped["years"][0]
    # Year 1: interest 54 (600 x 9%) against a cap of 30% x 103 EBITDA = 30.9.
    assert y1["interestExpense"] == pytest.approx(54.0)
    assert y1["interestDisallowed"] == pytest.approx(54.0 - 0.3 * y1["ebitda"])
    assert y1["taxableIncome"] == pytest.approx(y1["ebit"] - 0.3 * y1["ebitda"])
    assert y1["tax"] > free["years"][0]["tax"]
    assert capped["returns"]["irr"] < free["returns"]["irr"]
    assert capped["allChecksPass"]
    # Nothing is lost: everything claimed is either deducted in some year or still carried at exit.
    claimed = sum(y["interestExpense"] - y["interestIncome"] for y in capped["years"])
    deducted = sum(y["ebit"] + y["interestIncome"] - y["taxableIncome"] for y in capped["years"])
    assert deducted + capped["interestCap"]["carryForwardAtExit"] == pytest.approx(claimed)


def test_carried_interest_is_deducted_once_there_is_room() -> None:
    # Heavy debt repaid fast: early years are capped, later years have room for the carried interest.
    r = K.run_lbo(lbo(interestCapPct=0.30, ebitdaMargin=0.25, revenueGrowth=0.15, tranches=[
        {"name": "Senior", "leverage": 5.0, "rate": 0.10, "amortPct": 0.0, "sweep": True}]))  # fmt: skip
    cf = [y["interestCarryForward"] for y in r["years"]]
    assert cf[0] > 0 and cf[-1] == pytest.approx(0)
    assert any(y["interestDisallowed"] < 0 for y in r["years"])  # a negative disallowance is a release


def test_cap_must_be_a_fraction() -> None:
    assert K.run_lbo(lbo(interestCapPct=1.5))["ok"] is False


def test_v3_default_buyout_uses_the_cap() -> None:
    a = V3.analyse(deal())
    assert a["lboIn"]["interestCapPct"] == 0.30
    assert "interestCap" in a["scenarios"]["base"]["result"]
    assert any("163(j)" in c for c in a["lbo"]["conventions"])


# debt weight -------------------------------------------------------------------------------------------------
def test_debt_weight_is_the_companys_own() -> None:
    assert V3.debt_weight(deal()) == pytest.approx(500 / (500 + 1600))
    a = V3.analyse(deal())
    assert a["wacc"]["ok"] and a["wacc"]["debtWeight"] == pytest.approx(500 / 2100)


def test_unknown_debt_means_no_dcf_rather_than_a_house_weight() -> None:
    a = V3.analyse(deal(market={"marketCap": 1600.0}))
    assert a["dcfIn"]["debtWeight"] is None and a["dcf"]["ok"] is False
    assert any("Debt weight" in e for e in a["dcf"]["errors"])


# verdict rules -----------------------------------------------------------------------------------------------
def test_default_model_proceed_is_capped() -> None:
    assert V2.analyse(deal(), LAX)["rec"]["decision"] == "Proceed"
    rec = V3.analyse(deal(), LAX)["rec"]
    assert rec["decision"] == "Further Research" and rec["v2Decision"] == "Proceed"
    assert rec["rulesApplied"] == ["default-model cap"]


def test_custom_model_can_still_proceed() -> None:
    custom = V3.default_lbo(deal())
    assert V3.analyse(deal(lbo=custom), LAX)["rec"]["decision"] == "Proceed"


def test_expensive_or_thin_margin_companies_are_not_buyout_candidates() -> None:
    a = V3.analyse(deal(ev=6000.0, market={"marketCap": 5600.0, "debt": 500.0}), LAX)
    assert a["eligibility"]["eligible"] is False and a["rec"]["decision"] == "Pass"
    assert "30.0x is above 25x" in a["rec"]["rationale"]
    thin = V3.eligibility(deal(ebitda=80.0))
    assert not thin["eligible"] and "8.00%" in thin["reasons"][0]
    assert V3.eligibility(deal(ev=None, ebitda=None))["eligible"]  # unknown is not a reason


def test_negative_dcf_ev_has_no_value_per_share() -> None:
    d = deal()
    dcf_in = V3.default_dcf(d)
    dcf_in.update(sharesOutstanding=100.0, terminalMethod="gordon", capexPct=0.5)
    a = V3.analyse({**d, "dcf": dcf_in})
    prim = a["dcf"]["primary"]
    assert prim["ev"] < 0 and prim["equity"] is None and prim["perShare"] is None
    assert any("no equity value" in w for w in a["dcf"]["warnings"])


def test_sign_changes() -> None:
    assert V3.sign_changes([-100, 0, 0, 0, 250]) == 1
    assert V3.sign_changes([-100, -10, 0, 0, 250]) == 1
    assert V3.sign_changes([-100, 20, -10, 250]) == 3


# versioning ---------------------------------------------------------------------------------------------------
def test_versions_are_recorded_and_inputs_untouched() -> None:
    d = deal()
    before, v2_before = copy.deepcopy(d), V2.analyse(d)
    a = V3.analyse(d)
    assert a["calcVersion"] == "kci-v3" and a["engineVersion"] == "kci-v1"
    assert d == before
    assert json.dumps(V2.analyse(d), sort_keys=True, default=str) == json.dumps(
        v2_before, sort_keys=True, default=str
    )


def test_golden_output_and_change_record_are_current() -> None:
    r = subprocess.run([sys.executable, "scripts/kci_v3_changes.py", "--check"], cwd=ROOT, capture_output=True,
                       text=True, check=False)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr


def test_seed_baseline_still_has_dropbox_custom_model() -> None:
    a = V3.analyse(SEED["us-dbx"])
    assert a["customModel"]["lbo"] and "rulesApplied" not in a["rec"]
