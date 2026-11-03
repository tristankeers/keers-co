"""Strategy B moat screen: each check against hand-worked numbers, the status rules, unknown never passing, the
point-in-time reads, and the quarterly backtest on the FICTIONAL company (Example Widgets, EXW)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from keers.data import fundamentals as F
from keers.data.models import Kind, Value, missing
from keers.data.store import Store
from keers.strategies import moat
from keers.strategies.moat import backtest as B
from keers.strategies.moat import screener as S
from tests.data import fixtures as fx
from tests.data.test_data_layer import load, with_market
from tests.web import demo


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(demo.build(tmp_path / "k.sqlite"))


def years(attr: str, values: list[float | None]) -> list[S.Year]:
    out = []
    for i, v in enumerate(values):
        y = S.Year(date(2015 + i, 12, 31))
        setattr(y, attr, v)
        out.append(y)
    return out


def val(x: float) -> Value:
    return Value(x, Kind.DERIVED, "USDm", "test")


# checks -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("roics", "level"), [([0.2, 0.18, 0.16, 0.25, 0.3], "pass"),
                                              ([0.12, 0.11, 0.13, 0.09, 0.14], "watch"),
                                              ([0.05, 0.08, 0.12, 0.02, 0.09], "fail")])  # fmt: skip
def test_roic_level_is_the_median(roics: list[float], level: str) -> None:
    c = S.roic_level(years("roic", list(roics)))
    assert c.level == level and c.value == pytest.approx(sorted(roics)[2])


def test_roic_consistency_counts_years_above_the_floor() -> None:
    c = S.roic_consistency(years("roic", [0.2, 0.2, 0.05, 0.2, 0.2]))
    assert c.value == pytest.approx(0.8) and c.level == "pass" and "lowest 5.0% (fiscal 2017)" in c.reason
    assert S.roic_consistency(years("roic", [0.2, 0.05, 0.05, 0.2, 0.2])).level == "watch"
    assert S.roic_consistency(years("roic", [0.2, 0.05, 0.05, 0.05, 0.2])).level == "fail"


def test_too_few_years_is_unknown_not_a_pass() -> None:
    c = S.roic_level(years("roic", [0.3, 0.3, None, None, 0.3]))
    assert c.level == "unknown" and "3 fiscal years" in c.reason


def test_margin_stability_and_erosion() -> None:
    steady = S.margin_stability(years("operating_margin", [0.20, 0.21, 0.19, 0.20, 0.20]))
    assert steady.level == "pass" and steady.value == pytest.approx(0.006325, abs=1e-6)
    jumpy = S.margin_stability(years("operating_margin", [0.10, 0.20, 0.05, 0.18, 0.12]))
    assert jumpy.level == "watch"  # 5.4pp
    eroding = S.margin_stability(years("operating_margin", [0.30, 0.30, 0.30, 0.30, 0.24]))
    assert eroding.value is not None and eroding.value < S.MARGIN_SD_PASS
    assert eroding.level == "watch" and "well below the median" in eroding.reason


def test_growth_is_compound_annual() -> None:
    c = S.growth(years("revenue", [100, 110, 121, 133.1, 146.41]))
    assert c.value == pytest.approx(0.10, abs=1e-3) and c.level == "pass"
    assert S.growth(years("revenue", [100, 98, 97, 99, 101])).level == "watch"
    assert S.growth(years("revenue", [100, 90, 85, 80, 70])).level == "fail"


def test_cash_conversion_totals_the_window() -> None:
    ys = years("fcf", [50, 60, 70, 80, 90])
    for y, n in zip(ys, [100, 100, 100, 100, 100], strict=True):
        y.nopat = n
    c = S.cash_conversion(ys)
    assert c.value == pytest.approx(350 / 500) and c.level == "pass"
    for y in ys:
        y.nopat = -10
    assert S.cash_conversion(ys).level == "fail"


def test_balance_sheet_and_dilution() -> None:
    assert S.balance_sheet(val(-50), val(100)).level == "pass"
    assert S.balance_sheet(val(250), val(100)).level == "watch"
    assert S.balance_sheet(val(400), val(100)).level == "fail"
    assert S.balance_sheet(val(10), val(-1)).level == "fail"
    assert S.balance_sheet(missing("USDm", "debt incomplete"), val(100)).level == "unknown"
    assert S.dilution(years("diluted_shares", [100, 99, 98, 97, 96])).level == "pass"
    assert S.dilution(years("diluted_shares", [100, 102, 104, 106, 108])).level == "watch"
    assert S.dilution(years("diluted_shares", [100, 105, 110, 116, 122])).level == "fail"


@pytest.mark.parametrize(("levels", "status"), [(["pass"] * 7, "compounder"), (["pass"] * 6 + ["watch"], "candidate"),
                                                (["watch"] * 3 + ["pass"] * 4, "weak"),
                                                (["pass"] * 6 + ["unknown"], "incomplete"),
                                                (["fail", "unknown"] + ["pass"] * 5, "fails")])  # fmt: skip
def test_status_rules(levels: list[str], status: str) -> None:
    assert S.overall([S.Check("x", lvl, None, "", "") for lvl in levels]) == status


def test_tax_rate_falls_back_to_statutory_and_says_so() -> None:
    assert S.statutory_rate("us-gaap", date(2017, 12, 31)) == 0.35
    assert S.statutory_rate("us-gaap", date(2018, 12, 31)) == 0.21
    assert S.statutory_rate("ifrs-full", date(2018, 12, 31)) == 0.25


# end to end on the fictional company --------------------------------------------------------------------------
def test_screen_end_to_end_with_hand_worked_numbers(store: Store) -> None:
    r = moat.screen(store, fx.TICKER, demo.AS_OF)
    assert r["status"] == "compounder", r["checks"]
    assert r["window"] == "fiscal 2018-2024" and r["screenVersion"] == "moat-v1"
    y24 = r["years"][-1]
    # Invested capital: 2024 1,100 − (150 cash + 50 STI) − (240 − 20) = 680; 2023 1,000 − 150 − (190 − 20) = 680.
    assert y24["investedCapital"] == pytest.approx(680)
    assert y24["taxBasis"] == "effective" and y24["taxRate"] == pytest.approx(0.21)
    assert y24["roic"] == pytest.approx(200 * 0.79 / 680)
    assert y24["fcf"] == pytest.approx(150 - 30)
    assert y24["inputs"]["operatingCashFlow"]["sources"][0]["accn"] == fx.K24[0]
    c = {x["name"]: x for x in r["checks"]}
    assert c["growth"]["value"] == pytest.approx(
        (1000 / 600) ** (365.25 / (date(2024, 12, 31) - date(2018, 12, 31)).days) - 1
    )
    assert c["balanceSheet"]["value"] == pytest.approx(200 / 283)
    assert c["dilution"]["value"] < 0  # buybacks
    assert r["price"]["earningsYield"] == pytest.approx(230 / 2781.5, rel=1e-3)


def test_point_in_time_only_sees_filed_years(store: Store) -> None:
    # On 1 March 2025 the FY2024 10-K (filed 14 February 2025) is known: seven years.
    early = moat.screen(store, fx.TICKER, date(2025, 3, 1), detail=False)
    assert early["window"] == "fiscal 2018-2024"
    # In June 2024 the latest accounts are FY2022 (the FY2023 figures arrive only with the FY2024 10-K): stale.
    assert moat.screen(store, fx.TICKER, date(2024, 6, 28))["status"] == "blocked"


def test_short_history_is_insufficient_not_a_pass() -> None:
    st = with_market(load(demo.rows()[: len(fx.base_rows())] + [
        fx._inst("Assets", "2025-09-30", 1500, fx.Q3_25), fx._inst("LiabilitiesCurrent", "2025-09-30", 250, fx.Q3_25)]))  # fmt: skip
    r = moat.screen(st, fx.TICKER, demo.AS_OF)
    assert r["status"] == "insufficient history" and "2 fiscal years" in r["reason"]


def test_missing_cash_flow_makes_the_result_incomplete(tmp_path: Path) -> None:
    rows = [r for r in demo.rows() if r[1] != "NetCashProvidedByUsedInOperatingActivities"]
    r = moat.screen(with_market(load(rows)), fx.TICKER, demo.AS_OF)
    assert r["status"] == "incomplete"
    assert next(c for c in r["checks"] if c["name"] == "cashConversion")["level"] == "unknown"


def test_financials_are_not_graded(store: Store) -> None:
    store.db.execute("UPDATE companies SET sic='6022'")
    r = moat.screen(store, fx.TICKER, demo.AS_OF)
    assert r["status"] == "not applicable" and r["checks"] == []
    # Even with stale accounts, a bank is "not applicable", not "blocked".
    assert moat.screen(store, fx.TICKER, date(2026, 6, 1))["status"] == "not applicable"


def test_run_sorts_best_first(store: Store) -> None:
    res = moat.run(store, ["NOPE", fx.TICKER], demo.AS_OF)
    assert [c["status"] for c in res["companies"]] == ["compounder", "blocked"]


# data layer -----------------------------------------------------------------------------------------------------
def test_fiscal_years_span_revenue_concepts_and_annual_never_sums_quarters(store: Store) -> None:
    st = load(fx.base_rows() + [fx._dur("RevenueFromContractWithCustomerExcludingAssessedTax", "2022-01-01",
                                        "2022-12-31", 800, ("0000000001-23-000001", "10-K", "2023-02-14"))])  # fmt: skip
    fu = F.Fundamentals(st, fx.CIK, demo.AS_OF)
    assert [d.year for d in fu.fiscal_year_ends()] == [2022, 2023, 2024]
    assert not fu.annual(F.REVENUE, "revenue", date(2025, 9, 30)).known  # a TTM, not a fiscal year
    assert fu.annual(F.REVENUE, "revenue", date(2024, 12, 31)).value == 1000


def test_fact_cache_gives_the_same_point_in_time_answers(store: Store) -> None:
    before = moat.screen(store, fx.TICKER, date(2025, 3, 1), detail=False)
    store.cache_facts()
    assert moat.screen(store, fx.TICKER, date(2025, 3, 1), detail=False) == before
    assert moat.screen(store, fx.TICKER, date(2024, 6, 28))["status"] == "blocked"


# backtest -----------------------------------------------------------------------------------------------------
def test_backtest_holds_only_after_the_filing_is_known(store: Store) -> None:
    res = B.run(store, [fx.TICKER], date(2024, 1, 1), date(2025, 11, 7))
    assert res["ran"] and res["version"] == "moat-backtest-v1"
    held = {q["signalDay"]: q["held"] for q in res["quarters"]}
    assert held["2024-12-31"] == []  # FY2024 10-K not filed yet: accounts stale, so blocked
    assert held["2025-03-31"] == [fx.TICKER]
    assert res["research"]["gate"] == "gate-v2" and res["research"]["registered"] is False
    assert "survivorship bias" in res["warnings"][0]


def test_backtest_uses_point_in_time_members(store: Store) -> None:
    res = B.run(store, [fx.TICKER], date(2024, 1, 1), date(2025, 11, 7),
                members=lambda d: frozenset({fx.TICKER}) if d >= date(2025, 7, 1) else frozenset())  # fmt: skip
    held = {q["signalDay"]: q["held"] for q in res["quarters"]}
    # Not a member on 31 March (screened alone it was held then); a member and held on 30 September.
    assert held["2025-03-31"] == [] and held["2025-09-30"] == [fx.TICKER]
    assert next(q for q in res["quarters"] if q["signalDay"] == "2025-03-31")["screened"] == 0
    assert res["warnings"][0].startswith("universe: index members")


# share splits and filing errors -------------------------------------------------------------------------------
def _with_split(store: Store) -> None:
    """A 2-for-1 split in 2023: FY2022 and earlier counts as first filed; the FY2024 10-K restates FY2023 and the
    FY2022 comparative at twice the count, and FY2024 is filed post-split."""
    from keers.data.sec import parse_company_facts

    rows = [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "2022-01-01", "2022-12-31",
             52.4 * 2 * fx.M, *fx.K24),  # FY2022 restated in the FY2024 10-K
            ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "2023-01-01", "2023-12-31",
             52.0 * 2 * fx.M, *fx.K24),
            ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "2024-01-01", "2024-12-31",
             51.6 * 2 * fx.M, *fx.K24)]  # fmt: skip
    # each 10-K also carries the prior year as a comparative, as real ones do
    rows += [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", f"{y - 1}-01-01",
              f"{y - 1}-12-31", (54.0 - 0.4 * (y - 1 - 2018)) * fx.M, f"0000000001-{(y + 1) % 100:02d}-100{y % 10}",
              "10-K", f"{y + 1}-02-14") for y in range(2019, 2023)]  # fmt: skip
    store.db.execute("DELETE FROM facts WHERE concept='WeightedAverageNumberOfDilutedSharesOutstanding' AND "
                     "accn=? AND end>='2023-01-01'", (fx.K24[0],))  # fmt: skip
    store.put_facts(parse_company_facts(fx.company_facts(rows), "sha-split"))


def test_a_split_does_not_read_as_dilution(store: Store) -> None:
    _with_split(store)
    fu = F.Fundamentals(store, fx.CIK, demo.AS_OF)
    assert fu.split_factor("0000000001-23-1002") == pytest.approx(2.0)
    assert fu.split_factor(fx.K24[0]) == 1.0
    r = moat.screen(store, fx.TICKER, demo.AS_OF)
    d = next(c for c in r["checks"] if c["name"] == "dilution")
    assert d["level"] == "pass" and d["value"] < 0  # 108.0m to 103.2m on one basis: buybacks, not dilution
    assert any("split-adjusted x2" in n for n in r["dataWarnings"])


def test_a_share_count_scaling_error_is_left_out(store: Store) -> None:
    store.db.execute("UPDATE facts SET value=716 WHERE concept='WeightedAverageNumberOfDilutedSharesOutstanding' "
                     "AND start='2024-01-01' AND end='2024-12-31'")  # fmt: skip
    r = moat.screen(store, fx.TICKER, demo.AS_OF)
    d = next(c for c in r["checks"] if c["name"] == "dilution")
    assert d["level"] == "pass" and "53.6m" not in d["reason"] and "to 52.0m" in d["reason"]
    assert any("too few for a listed company" in n for n in r["dataWarnings"])


def test_two_splits_are_counted_once_each_and_a_units_slip_is_no_split(store: Store) -> None:
    from keers.data.sec import parse_company_facts

    c = "WeightedAverageNumberOfDilutedSharesOutstanding"
    a = ("0000000001-22-2001", "10-Q", "2022-05-01")  # pre-split
    b = ("0000000001-22-2002", "10-Q", "2022-08-01")  # after split 1 (2-for-1)
    d = ("0000000001-23-2003", "10-Q", "2023-05-01")  # after split 2 (2-for-1)
    e = ("0000000001-23-2004", "10-Q", "2023-08-01")  # restates in millions by mistake
    rows = [("us-gaap", c, "shares", "2022-01-01", "2022-03-31", 50 * fx.M, *a),
            ("us-gaap", c, "shares", "2021-01-01", "2021-03-31", 49 * fx.M, *a),
            ("us-gaap", c, "shares", "2022-01-01", "2022-03-31", 100 * fx.M, *b),
            ("us-gaap", c, "shares", "2022-04-01", "2022-06-30", 100 * fx.M, *b),
            ("us-gaap", c, "shares", "2022-04-01", "2022-06-30", 200 * fx.M, *d),
            ("us-gaap", c, "shares", "2021-01-01", "2021-03-31", 196 * fx.M, *d),  # both splits at once
            ("us-gaap", c, "shares", "2023-01-01", "2023-03-31", 200 * fx.M, *d),
            ("us-gaap", c, "shares", "2023-01-01", "2023-03-31", 200.0, *e)]  # fmt: skip
    store.put_facts(parse_company_facts(fx.company_facts(rows), "sha-splits"))
    fu = F.Fundamentals(store, fx.CIK, demo.AS_OF)
    assert fu.split_factor(a[0]) == pytest.approx(4.0)  # not 8 or 16
    assert fu.split_factor(b[0]) == pytest.approx(2.0)
    assert fu.split_factor(d[0]) == 1.0  # the units slip in the next filing is no split
