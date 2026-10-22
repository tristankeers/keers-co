"""kci-v2 defaults: takeover premium, per-company beta, capex fade. kci-v1 must be untouched."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from keers.valuation import kci_v1_defaults as V1
from keers.valuation import kci_v2_defaults as V2

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads((ROOT / "legacy" / "kci" / "seed.json").read_text())["deals"]


def deal(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": "us-exw", "ticker": "EXW", "dataStatus": "filed",
        "revenue": 1000.0, "ebitda": 200.0, "ebit": 150.0, "netIncome": 90.0, "capex": 120.0,
        "ev": 2000.0, "netDebt": 400.0, "revenueGrowth": 0.06,
        "market": {"marketCap": 1600.0}, "beta": 1.2, "riskFree": 0.042,
    }  # fmt: skip
    d.update(over)
    return d


# takeover premium ---------------------------------------------------------------------------------------------
def test_buyout_pays_a_premium_and_exits_at_the_market_multiple() -> None:
    lbo = V2.default_lbo(deal())
    assert lbo["entryEVOverride"] == pytest.approx(2000 + 0.30 * 1600)
    assert lbo["entryMultiple"] == pytest.approx(2480 / 200)
    assert lbo["exitMultiple"] == pytest.approx(10.0)  # market EV / EBITDA, not the price paid
    # Debt is sized on the price paid: min(4x, 60% of 12.4x) = 4x, split 3x / 1x.
    assert [t["leverage"] for t in lbo["tranches"]] == [3.0, 1.0]
    r = V2.analyse(deal())["lbo"]
    assert r["ok"] and r["entry"]["entryEV"] == pytest.approx(2480)


def test_equity_value_falls_back_to_ev_less_net_debt() -> None:
    lbo = V2.default_lbo(deal(market={}))
    assert lbo["entryEVOverride"] == pytest.approx(2000 + 0.30 * (2000 - 400))


def test_no_market_value_means_no_buyout_price_rather_than_v1s_8x() -> None:
    assert V1.default_lbo(deal(ev=None))["entryMultiple"] == 8  # v1 invents a multiple
    a = V2.analyse(deal(ev=None, market={}))
    assert a["lbo"]["ok"] is False
    assert a["rec"]["decision"] != "Proceed"


def test_audit_proceeds_no_longer_proceed() -> None:
    # The audit found v1 says Proceed on EQT, Expand Energy and Gen Digital only because it buys at market.
    for t in ("eqt", "exe", "gen"):
        d = SEED[f"us-{t}"]
        assert V1.analyse(d)["rec"]["decision"] == "Proceed"
        assert V2.analyse(d)["rec"]["decision"] != "Proceed"


def test_custom_models_are_kept() -> None:
    d = SEED["us-dbx"]
    a = V2.analyse(d)
    assert a["lboIn"]["entryEVOverride"] == d["lbo"]["entryEVOverride"]
    assert a["customModel"] == {"lbo": True, "dcf": True}


# beta and cost of equity --------------------------------------------------------------------------------------
@pytest.mark.parametrize(("cap", "premium"), [(50_000, 0.0), (10_000, 0.0), (9_999, 0.01), (2_000, 0.01),
                                              (1_999, 0.02), (None, 0.02)])  # fmt: skip
def test_size_premium_bands(cap: float | None, premium: float) -> None:
    assert V2.size_premium(cap) == premium


def test_dcf_uses_the_companys_beta_and_live_risk_free_rate() -> None:
    a = V2.analyse(deal())
    assert a["dcfIn"]["beta"] == 1.2 and a["dcfIn"]["riskFree"] == 0.042
    assert a["wacc"]["costOfEquity"] == pytest.approx(0.042 + 1.2 * 0.055 + 0.02)  # $1.6bn cap
    assert a["dcf"]["ok"]


def test_no_beta_means_no_dcf() -> None:
    a = V2.analyse(deal(beta=None))
    assert a["dcf"]["ok"] is False and a["dcf"]["errors"] == ["Beta is required"]
    assert V1.analyse(deal(beta=None))["dcf"]["ok"] is True  # v1 silently used 1.1


# capex fade ---------------------------------------------------------------------------------------------------
def test_capex_fades_from_todays_ratio_to_da() -> None:
    assert V2.capex_path(0.12, 0.05, 5) == [0.12, 0.1025, 0.085, 0.0675, 0.05]
    assert V2.capex_path(0.03, 0.05, 5) == [0.03] * 5  # already below D&A: kept
    d = deal()  # capex 12% of revenue, D&A (200 - 150) 5%
    assert V2.default_dcf(d)["capexPct"] == [0.12, 0.1025, 0.085, 0.0675, 0.05]
    assert V2.default_lbo(d)["capexPct"] == [0.12, 0.1025, 0.085, 0.0675, 0.05]
    rows = V2.analyse(d)["dcf"]["rows"]
    assert [r["capex"] / r["revenue"] for r in rows] == pytest.approx([0.12, 0.1025, 0.085, 0.0675, 0.05])


def test_downside_adds_capex_to_every_year() -> None:
    sc = V2.default_scenarios(V2.default_lbo(deal()))
    assert sc["downside"]["capexPct"] == pytest.approx([0.125, 0.1075, 0.09, 0.0725, 0.055])
    assert sc["base"]["capexPct"] == [0.12, 0.1025, 0.085, 0.0675, 0.05]
    r = V2.analyse(deal())["scenarios"]
    assert all(r[k]["result"]["ok"] for k in ("downside", "base", "upside"))


def test_capex_fade_turns_the_build_out_companies_positive() -> None:
    # With v1's beta and size premium put back, only the capex fade differs.
    from scripts.kci_v2_changes import V1_WACC, dcf_ev

    for t in ("orcl", "meta", "googl"):
        d = SEED[f"us-{t}"]
        v1, v2 = dcf_ev(V1.default_dcf(d)), dcf_ev({**V2.default_dcf(d), **V1_WACC})
        assert v1 is not None and v2 is not None and v1 < 0 < v2


# versioning ---------------------------------------------------------------------------------------------------
def test_versions_are_recorded() -> None:
    a = V2.analyse(deal())
    assert a["calcVersion"] == "kci-v2" and a["engineVersion"] == "kci-v1" and a["changes"]
    assert V1.analyse(deal())["calcVersion"] == "kci-v1"


def test_v2_does_not_touch_inputs_or_v1() -> None:
    d = deal()
    before, v1_before = copy.deepcopy(d), V1.analyse(d)
    V2.analyse(d)
    assert d == before
    assert json.dumps(V1.analyse(d), sort_keys=True, default=str) == json.dumps(
        v1_before, sort_keys=True, default=str
    )


def test_golden_output_and_change_record_are_current() -> None:
    r = subprocess.run([sys.executable, "scripts/kci_v2_changes.py", "--check"], cwd=ROOT, capture_output=True,
                       text=True, check=False)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
