"""KCI's 19 hand-worked checks, run against the Python port.

Each expected value is worked out by hand (shown in the comment or the name), not taken from either engine.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from keers.valuation import kci_v1 as K

BASE_LBO: dict[str, Any] = {
    "entryRevenue": 500,
    "entryEbitda": 100,
    "entryMultiple": 10,
    "holdYears": 5,
    "tranches": [{"name": "Senior", "leverage": 5, "rate": 0.1, "amortPct": 0, "sweep": True}],
    "revenueGrowth": 0,
    "ebitdaMargin": 0.2,
    "taxRate": 0,
    "exitMultiple": 10,
    "cashSweepPct": 1,
}
FLAT_DCF: dict[str, Any] = {
    "baseRevenue": 1e3,
    "years": 5,
    "revenueGrowth": 0,
    "ebitdaMargin": 0.1,
    "daPct": 0,
    "capexPct": 0,
    "nwcPct": 0,
    "taxRate": 0,
    "wacc": 0.1,
    "terminalMethod": "gordon",
    "terminalGrowth": 0,
    "netDebt": 0,
}


def near(a: Any, b: float, tol: float = 1e-9) -> bool:
    return K.is_num(a) and abs(a - b) <= tol * max(1, abs(b))


def test_irr_doubling_over_five_years() -> None:
    assert near(K.irr([-100, 0, 0, 0, 0, 200])["value"], 2**0.2 - 1)


def test_irr_par_bond() -> None:
    assert near(K.irr([-100, 10, 10, 110])["value"], 0.1)


def test_lbo_unlevered_yield_equals_cost_of_debt() -> None:
    r = K.run_lbo(BASE_LBO)
    assert near(r["returns"]["irr"], 0.1)
    assert near(r["returns"]["moic"], 1.1**5)
    # 500 -> 450 -> 395 -> 334.5 -> 267.95 -> 194.745
    assert near(r["exit"]["exitDebt"], 194.745)


def test_sources_equal_uses_with_fees_cash_and_rollover() -> None:
    r = K.run_lbo(
        {**BASE_LBO, "transactionFeePct": 0.02, "financingFeePct": 0.02, "minCash": 10, "rollover": 40}
    )
    assert near(r["totalSources"], 1040) and near(r["totalUses"], 1040)  # EV 1,000 + fees 20 + 10 + cash 10


def test_zero_debt_moic() -> None:
    r = K.run_lbo(
        {**BASE_LBO, "tranches": [{"name": "None", "leverage": 0, "rate": 0.1, "amortPct": 0, "sweep": True}]}
    )
    assert near(r["returns"]["moic"], 1.5)  # cash builds 500; exit equity 1,500 on 1,000


def test_negative_cash_flow_is_funded_never_negative() -> None:
    r = K.run_lbo(
        {**BASE_LBO, "ebitdaMargin": -0.05, "minCash": 5, "revolver": {"commitment": 20, "rate": 0.08}}
    )
    assert all(y["closeCash"] >= 0 for y in r["years"])
    assert r["returns"]["cures"] > 0 and r["allChecksPass"]


def test_wiped_out_equity_floors_at_zero() -> None:
    r = K.run_lbo({**BASE_LBO, "exitMultiple": 1.5})  # exit EV 150 below net debt 194.7
    assert r["exit"]["sponsorProceeds"] == 0 and r["returns"]["irr"] == -1


def test_zero_ebitda_is_rejected() -> None:
    assert not K.run_lbo({**BASE_LBO, "entryEbitda": 0})["ok"]


def test_break_even_multiple_reproduces_target() -> None:
    b = K.break_even(K.run_lbo(BASE_LBO), 0.2)
    assert b is not None
    r2 = K.run_lbo({**BASE_LBO, "exitMultiple": b["requiredExitMultiple"]})
    assert near(r2["returns"]["irr"], 0.2, 1e-7)


def test_dcf_perpetuity_end_year_and_mid_year() -> None:
    assert near(K.run_dcf(FLAT_DCF)["primary"]["ev"], 1000)  # 100 / 10%
    assert near(K.run_dcf({**FLAT_DCF, "midYear": True})["primary"]["ev"], 1000 * math.sqrt(1.1))


def test_dcf_rejects_growth_at_wacc() -> None:
    assert not K.run_dcf({**FLAT_DCF, "terminalGrowth": 0.1})["ok"]


def test_wacc_capm_build_up() -> None:
    w = K.wacc(
        {
            "riskFree": 0.04,
            "beta": 1.2,
            "erp": 0.05,
            "sizePremium": 0.01,
            "preTaxCostOfDebt": 0.07,
            "taxRate": 0.25,
            "debtWeight": 0.3,
        }
    )
    assert near(w["value"], 0.7 * 0.11 + 0.3 * 0.0525)


def test_ev_to_equity_bridge() -> None:
    br = K.ev_bridge(ebitda=12.5, multiple=8, netDebt=30, otherClaims=5, nonOperatingAssets=2)
    assert near(br["equity"], 67)  # 12.5 x 8 - 30 - 5 + 2


def test_quartiles_match_excel() -> None:
    st = K.stats([30, 7, 5, 11, 9])
    assert (st["median"], st["q1"], st["q3"]) == (9, 7, 11)


@pytest.fixture
def rich_lbo() -> dict[str, Any]:
    return K.run_lbo(
        {
            **BASE_LBO,
            "transactionFeePct": 0.02,
            "exitFeePct": 0.01,
            "minCash": 5,
            "rollover": 50,
            "taxRate": 0.25,
            "daPct": 0.03,
        }
    )


def test_chart_exit_year_equity_equals_sponsor_proceeds(rich_lbo: dict[str, Any]) -> None:
    cs = K.chart_series(rich_lbo)
    assert cs is not None
    assert near(cs["equityByExitYear"][-1], rich_lbo["exit"]["sponsorProceeds"])


def test_value_bridge_sums_to_exit_equity(rich_lbo: dict[str, Any]) -> None:
    cs = K.chart_series(rich_lbo)
    assert cs is not None
    s = sum(x["value"] for x in cs["bridge"] if not x.get("total")) + cs["bridge"][0]["value"]
    assert near(s, rich_lbo["exit"]["exitEquityRaw"])


def test_missing_data_never_proceeds() -> None:
    rec = K.recommend(
        {
            "baseIrr": None,
            "baseMoic": 3,
            "downsideMoic": 1.5,
            "entryLeverage": 4,
            "score": 80,
            "checksPass": True,
            "dataVerified": True,
        }
    )
    assert rec["decision"] == "Further Research"
