"""Distress screen: each check against hand-worked numbers, the status rules, and missing data never passing."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from keers.data.models import Kind, PricePoint, RatePoint, Value, missing
from keers.strategies import distress as D
from tests.data import fixtures as fx
from tests.data.test_data_layer import load, with_market

AS_OF = date(2025, 11, 10)


def val(x: float) -> Value:
    return Value(x, Kind.REPORTED, "USDm", "test")


UNK = missing("USDm", "not filed")


# interest cover -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("ebit", "interest", "level"), [(300, 100, "clear"), (150, 100, "watch"),
                                                          (90, 100, "severe"), (-50, 100, "severe")])  # fmt: skip
def test_interest_cover_levels(ebit: float, interest: float, level: str) -> None:
    c = D.interest_cover(val(ebit), val(interest), val(1000))
    assert c.level == level and c.value == pytest.approx(ebit / interest)


def test_interest_cover_unknown_unless_there_is_no_debt() -> None:
    assert D.interest_cover(val(100), UNK, val(500)).level == "unknown"
    assert D.interest_cover(val(100), UNK, val(0)).level == "clear"
    assert D.interest_cover(UNK, val(10), val(500)).level == "unknown"


# leverage -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("nd", "ebitda", "level"), [(300, 100, "clear"), (450, 100, "watch"), (700, 100, "severe"),
                                                      (100, -5, "severe"), (-50, -5, "clear")])  # fmt: skip
def test_net_leverage_levels(nd: float, ebitda: float, level: str) -> None:
    assert D.net_leverage(val(nd), val(ebitda)).level == level


def test_net_leverage_unknown_when_net_debt_is_not_trusted() -> None:
    c = D.net_leverage(missing("USDm", "net debt: debt looks incomplete"), val(100))
    assert c.level == "unknown" and "debt looks incomplete" in c.reason


# debt due -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("due", "cash", "level"), [(40, 100, "clear"), (60, 100, "watch"), (150, 100, "severe"),
                                                     (0, 0, "clear"), (10, 0, "severe")])  # fmt: skip
def test_debt_due_levels(due: float, cash: float, level: str) -> None:
    assert D.debt_due(val(due), val(cash)).level == level


def test_undrawn_credit_lines_count_towards_liquidity() -> None:
    c = D.debt_due(val(150), val(100), val(200))
    assert c.level == "clear" and c.value == pytest.approx(0.5) and "200m of undrawn" in c.reason
    assert D.debt_due(val(150), val(100), UNK).level == "severe"  # not filed: not assumed


def test_debt_due_unknown() -> None:
    assert D.debt_due(UNK, val(100)).level == "unknown"
    assert D.debt_due(val(50), UNK).level == "unknown"


# Altman Z -----------------------------------------------------------------------------------------------------
def test_original_z_for_a_manufacturer_matches_the_formula() -> None:
    c = D.altman_z(
        "3714", val(1000), val(400), val(300), val(-200), val(20), val(900), val(100), val(150), val(800)
    )
    z = 1.2 * 0.1 + 1.4 * -0.2 + 3.3 * 0.02 + 0.6 * (150 / 900) + 1.0 * 0.8
    assert c.value == pytest.approx(z) and c.level == "severe"  # 0.806 < 1.81
    assert "original Z" in c.reason


def test_z_double_prime_for_others_uses_book_equity_not_sales() -> None:
    c = D.altman_z("7372", val(1000), val(400), val(300), val(100), val(80), val(600), val(400), UNK, UNK)
    z = 6.56 * 0.1 + 3.26 * 0.1 + 6.72 * 0.08 + 1.05 * (400 / 600)
    assert c.value == pytest.approx(z) and c.level == "watch"  # 2.22: grey zone
    assert "Z''" in c.reason


def test_z_names_what_is_missing() -> None:
    c = D.altman_z("7372", val(1000), UNK, val(300), val(100), val(80), val(600), val(400), UNK, UNK)
    assert c.level == "unknown" and "current assets" in c.reason


def test_sic_bands() -> None:
    assert D.is_manufacturer("2000") and D.is_manufacturer("3999")
    assert not D.is_manufacturer("1311") and not D.is_manufacturer("7372") and not D.is_manufacturer(None)


# status -------------------------------------------------------------------------------------------------------
def chk(level: str) -> D.Check:
    return D.Check("x", level, None, "", "")


@pytest.mark.parametrize(
    ("levels", "status"),
    [
        (["severe", "severe", "clear"], "distressed"),
        (["severe", "unknown", "unknown"], "stressed"),
        (["watch", "watch", "clear"], "stressed"),
        (["watch", "clear", "clear"], "watch"),
        (["watch", "unknown", "clear"], "incomplete"),
        (["clear", "unknown", "clear"], "incomplete"),
        (["clear", "clear", "clear"], "no flags"),
    ],
)
def test_overall_status_and_unknown_never_passes(levels: list[str], status: str) -> None:
    assert D.overall([chk(x) for x in levels]) == status


# end to end on the fictional company --------------------------------------------------------------------------
def full_store(drop: float = 0.0, assets: bool = True, liab_concept: str = "Liabilities"):  # type: ignore[no-untyped-def]
    rows = fx.base_rows()
    if assets:
        for concept, v in [("Assets", 1500), ("AssetsCurrent", 600), ("LiabilitiesCurrent", 250),
                           (liab_concept, 800 if liab_concept == "Liabilities" else 1500),
                           ("RetainedEarningsAccumulatedDeficit", 300),
                           ("StockholdersEquity", 700)]:  # fmt: skip
            rows.append(fx._inst(concept, "2025-09-30", v, fx.Q3_25))
    for start, end, v, f in [("2024-01-01", "2024-12-31", 40, fx.K24), ("2024-01-01", "2024-09-30", 30, fx.Q3_25),
                             ("2025-01-01", "2025-09-30", 33, fx.Q3_25)]:  # fmt: skip
        rows.append(fx._dur("InterestExpense", start, end, v, f))
    store = with_market(load(rows))
    # 260 weekdays of adjusted closes: flat at 50, then a fall of ``drop`` on the last day.
    d, pts = AS_OF - timedelta(days=364), []
    while d <= AS_OF - timedelta(days=4):  # ends the day before the stooq close that sets the price
        if d.weekday() < 5:
            pts.append(PricePoint(fx.TICKER, d, 50.0, "tiingo", raw_sha256="s", adj_close=50.0))
        d += timedelta(days=1)
    last = pts[-1]
    pts[-1] = PricePoint(
        fx.TICKER, last.day, 50 * (1 - drop), "tiingo", raw_sha256="s", adj_close=50 * (1 - drop)
    )
    store.put_prices(pts)
    store.put_rates([RatePoint("BAA10Y", date(2025, 11, 7), 3.4, "fred", "s"),
                     RatePoint("BAA10Y", date(2025, 8, 8), 2.5, "fred", "s")])  # fmt: skip
    return store


def test_screen_end_to_end_with_hand_worked_numbers() -> None:
    r = D.screen(full_store(), fx.TICKER, AS_OF)
    c = {x["name"]: x for x in r["checks"]}
    assert r["status"] == "no flags", r
    assert c["interestCover"]["value"] == pytest.approx(230 / 43)  # EBIT TTM 230, interest 40 + 33 - 30
    assert c["netLeverage"]["value"] == pytest.approx(200 / 283)
    assert c["debtDue"]["value"] == pytest.approx(20 / 200)  # current long-term debt against cash + STI
    z = 1.2 * 350 / 1500 + 1.4 * 300 / 1500 + 3.3 * 230 / 1500 + 0.6 * 2000 / 800 + 1.0 * 1100 / 1500
    assert c["altmanZ"]["value"] == pytest.approx(z)  # SIC 3990: original model, with market cap
    assert c["drawdown"]["value"] == pytest.approx(0.0)
    assert r["screenVersion"] == "distress-v1"


def test_price_collapse_is_flagged() -> None:
    r = D.screen(full_store(drop=0.65), fx.TICKER, AS_OF)
    c = {x["name"]: x for x in r["checks"]}
    assert c["drawdown"]["level"] == "severe" and c["drawdown"]["value"] == pytest.approx(-0.65)
    assert r["status"] == "stressed"


def test_missing_balance_sheet_makes_the_result_incomplete_not_clear() -> None:
    r = D.screen(full_store(assets=False), fx.TICKER, AS_OF)
    assert r["status"] == "incomplete"
    assert next(x for x in r["checks"] if x["name"] == "altmanZ")["level"] == "unknown"


def test_stale_data_blocks_the_screen() -> None:
    r = D.screen(full_store(), fx.TICKER, date(2026, 3, 1))
    assert r["status"] == "blocked" and r["checks"] == []


def test_run_sorts_worst_first_and_reports_the_credit_backdrop() -> None:
    res = D.run(full_store(), [fx.TICKER, "NOPE"], AS_OF)
    assert [r["status"] for r in res["companies"]] == ["no flags", "blocked"]
    assert res["credit"]["level"] == "watch"  # 3.4pp, and up 0.9pp in three months
    assert res["credit"]["change"] == pytest.approx(0.9)


def test_total_liabilities_from_liabilities_and_equity_when_not_filed() -> None:
    r = D.screen(full_store(liab_concept="LiabilitiesAndStockholdersEquity"), fx.TICKER, AS_OF)
    z = next(x for x in r["checks"] if x["name"] == "altmanZ")
    assert z["inputs"]["liabilities"]["value"] == pytest.approx(1500 - 700)
    assert z["level"] == "clear"
