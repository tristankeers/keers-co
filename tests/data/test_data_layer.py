"""Data layer tests on synthetic documents for a fictional company (see fixtures.py). No network."""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import date
from email.message import Message
from typing import Any

import pytest

from keers.data import fundamentals as F
from keers.data import snapshot
from keers.data.http import Fetcher, FetchError
from keers.data.market import parse_fred_csv, parse_stooq_csv, parse_tiingo_json
from keers.data.models import Fact, Kind, PricePoint, RatePoint
from keers.data.refresh import refresh
from keers.data.sec import ParseError, parse_company_facts, parse_submissions, parse_tickers
from keers.data.store import Store

from . import fixtures as fx


def load(rows: list[fx.Row] | None = None) -> Store:
    store = Store(":memory:")
    info, filings = parse_submissions(fx.submissions())
    store.put_company(info["cik"], fx.TICKER, info["name"], info["sic"], info["fiscalYearEnd"])
    store.put_filings(filings)
    store.put_facts(parse_company_facts(fx.company_facts(rows or fx.base_rows()), "sha-fixture"))
    return store


def with_market(store: Store, price_day: str = "2025-11-07", price: float = 40.0) -> Store:
    store.put_prices(
        [PricePoint(fx.TICKER, date.fromisoformat(price_day), price, "stooq", raw_sha256="sha-px")]
    )
    store.put_rates([RatePoint("DGS10", date(2025, 11, 7), 4.1, "fred", "sha-r")])
    return store


# parsers ------------------------------------------------------------------------------------------------------
def test_parse_tickers_and_submissions() -> None:
    assert parse_tickers(fx.tickers()) == {"EXW": (fx.CIK, fx.NAME)}
    info, filings = parse_submissions(fx.submissions())
    assert info["cik"] == fx.CIK and info["fiscalYearEnd"] == "1231"
    assert [f.form for f in filings] == ["10-K", "10-Q", "10-Q"]  # the 8-K is dropped


def test_parse_company_facts_keeps_only_requested_concepts() -> None:
    doc = fx.company_facts(fx.base_rows())
    facts = parse_company_facts(doc, "abc", ["us-gaap:Revenues"])
    assert {f.concept for f in facts} == {"Revenues"}
    assert all(f.raw_sha256 == "abc" for f in facts)
    assert any(f.start is None for f in parse_company_facts(doc, "abc", ["us-gaap:ShortTermInvestments"]))


@pytest.mark.parametrize(
    "bad",
    [
        {"facts": {}},  # no cik
        {"cik": 1, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"end": "2025-01-01"}]}}}}},  # no val
    ],
)
def test_bad_company_facts_raise(bad: dict[str, Any]) -> None:
    with pytest.raises(ParseError):
        parse_company_facts(bad, "x")


def test_market_parsers() -> None:
    pts = parse_stooq_csv(fx.stooq_csv([("2025-11-06", 39.5), ("2025-11-07", 40.0)]), "exw", "s")
    assert [(p.ticker, p.close) for p in pts] == [("EXW", 39.5), ("EXW", 40.0)]
    with pytest.raises(ParseError):
        parse_stooq_csv("No data", "exw", "s")
    rates = parse_fred_csv(fx.fred_csv("DGS10", [("2025-11-06", "."), ("2025-11-07", "4.10")]), "DGS10", "s")
    assert [(r.day, r.value) for r in rates] == [(date(2025, 11, 7), 4.1)]  # "." means no observation
    with pytest.raises(ParseError):
        parse_fred_csv(fx.fred_csv("DGS3MO", []), "DGS10", "s")
    t = parse_tiingo_json([{"date": "2025-11-07T00:00:00.000Z", "close": 40.0}], "exw", "s")
    assert t[0].day == date(2025, 11, 7) and t[0].source == "tiingo"


# store --------------------------------------------------------------------------------------------------------
def test_store_dedupes_instant_facts() -> None:
    store = load()
    again = store.put_facts(parse_company_facts(fx.company_facts(fx.base_rows()), "sha-fixture"))
    assert again == 0  # balance-sheet facts (no start date) included


# TTM and point in time -----------------------------------------------------------------------------------------
def rev(store: Store, as_of: date) -> list[Fact]:
    return store.facts(fx.CIK, "Revenues", "USD", known_by=as_of)


def test_ttm_from_year_to_date() -> None:
    p = F.ttm_latest(rev(load(), date(2025, 11, 10)))
    assert p is not None
    assert p.value == pytest.approx((1000 + 800 - 700) * fx.M)
    assert p.end == date(2025, 9, 30) and p.kind is Kind.DERIVED and len(p.used) == 3


def test_point_in_time_ignores_later_filings() -> None:
    store = load()
    q2 = F.ttm_latest(rev(store, date(2025, 8, 10)))
    assert q2 is not None and q2.end == date(2025, 6, 30)
    assert q2.value == pytest.approx((1000 + 520 - 450) * fx.M)  # uses the six-month YTD, not the three-month
    fy = F.ttm_latest(rev(store, date(2025, 3, 1)))
    assert fy is not None and fy.end == date(2024, 12, 31) and fy.kind is Kind.REPORTED
    assert fy.value == pytest.approx(1000 * fx.M)


def test_restatement_known_only_after_it_is_filed() -> None:
    restated = ("us-gaap", "Revenues", "USD", "2024-01-01", "2024-12-31", 990 * fx.M,
                "0000000001-26-000001", "10-K", "2026-02-13")  # fmt: skip
    store = load(fx.base_rows() + [restated])
    before = F.ttm_at(rev(store, date(2025, 11, 10)), date(2024, 12, 31))
    after = F.ttm_at(rev(store, date(2026, 3, 1)), date(2024, 12, 31))
    assert before is not None and before.value == pytest.approx(1000 * fx.M)
    assert after is not None and after.value == pytest.approx(990 * fx.M)


def test_newer_concept_wins() -> None:
    newer = [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD", "2025-01-01", "2025-12-31",
         1234 * fx.M, "0000000001-26-000001", "10-K", "2026-02-13"),
    ]  # fmt: skip
    store = load(fx.base_rows() + newer)
    v = F.Fundamentals(store, fx.CIK, date(2026, 3, 1)).flow(F.REVENUE, "revenue")
    assert v.value == pytest.approx(1234) and "RevenueFromContract" in v.method


def test_missing_concept_is_missing_not_zero() -> None:
    v = F.Fundamentals(load(), fx.CIK, date(2025, 11, 10)).flow(F.INTEREST, "interest expense")
    assert v.value is None and not v.known and v.warnings


# snapshot -----------------------------------------------------------------------------------------------------
def test_snapshot_numbers_and_provenance() -> None:
    snap = snapshot.build(with_market(load()), "EXW", date(2025, 11, 10))
    v = {k: x.value for k, x in snap.values.items()}
    assert not snap.blocked, snap.blocked_by
    assert v["revenue"] == pytest.approx(1100)
    assert v["ebit"] == pytest.approx(200 + 170 - 140)
    assert v["ebitda"] == pytest.approx(230 + (50 + 40 - 37))
    assert v["netIncome"] == pytest.approx(140)
    assert v["capex"] == pytest.approx(35)
    assert v["netDebt"] == pytest.approx(380 + 20 - 170 - 30)
    assert v["shares"] == pytest.approx(50)
    assert v["marketCap"] == pytest.approx(2000)
    assert v["ev"] == pytest.approx(2200)
    assert v["revenueGrowth"] == pytest.approx(1100 / (900 + 700 - 630) - 1)
    assert snap.values["ebitda"].kind is Kind.DERIVED
    assert {s["accn"] for s in snap.values["revenue"].sources} == {fx.K24[0], fx.Q3_25[0]}


def test_live_snapshot_runs_kci_v1() -> None:
    out = snapshot.analyse(snapshot.build(with_market(load()), "EXW", date(2025, 11, 10)), "kci-v1")
    assert out["ran"] is True
    assert out["analysis"]["calcVersion"] == "kci-v1"
    assert out["deal"]["dataStatus"] == "filed"  # never "verified" without a cross-check
    assert out["analysis"]["lbo"]["ok"] in (True, False)


def test_live_snapshot_defaults_to_kci_v2_and_passes_live_rates() -> None:
    out = snapshot.analyse(snapshot.build(with_market(load()), "EXW", date(2025, 11, 10)))
    a = out["analysis"]
    assert out["ran"] is True and a["calcVersion"] == "kci-v2" and a["engineVersion"] == "kci-v1"
    assert out["deal"]["riskFree"] == pytest.approx(0.041)  # FRED's 4.1% as a decimal
    # No benchmark prices in the store, so no beta, so the DCF refuses rather than using a house beta.
    assert out["deal"]["beta"] is None
    assert a["dcf"]["ok"] is False and "Beta is required" in a["dcf"]["errors"]
    assert a["lboIn"]["entryEVOverride"] == pytest.approx(2200 + 0.3 * 2000)


@pytest.mark.parametrize(
    ("price_day", "as_of", "reason"),
    [
        ("2025-10-24", date(2025, 11, 10), "stale price"),
        ("2025-11-07", date(2026, 3, 1), "stale fundamentals"),
    ],
)
def test_stale_data_blocks(price_day: str, as_of: date, reason: str) -> None:
    snap = snapshot.build(with_market(load(), price_day), "EXW", as_of)
    assert any(reason in b for b in snap.blocked_by)
    assert snapshot.analyse(snap)["ran"] is False


def test_missing_price_and_unknown_company_block() -> None:
    snap = snapshot.build(load(), "EXW", date(2025, 11, 10))
    assert any("missing marketCap" in b for b in snap.blocked_by)
    assert snapshot.build(load(), "NOPE", date(2025, 11, 10)).blocked


def test_business_days() -> None:
    assert snapshot.business_days_between(date(2025, 11, 7), date(2025, 11, 10)) == 1  # Fri -> Mon
    assert snapshot.business_days_between(date(2025, 11, 10), date(2025, 11, 10)) == 0


# http ---------------------------------------------------------------------------------------------------------
def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", Message(), None)


def test_fetcher_retries_then_succeeds() -> None:
    calls, slept = [], []

    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        calls.append(req.get_header("User-agent"))
        if len(calls) < 3:
            raise http_error(503)
        return b"ok"

    r = Fetcher("Test agent test@example.com", opener=opener, sleep=slept.append).get(
        "https://data.sec.gov/a"
    )
    assert r.body == b"ok" and len(calls) == 3 and calls[0] == "Test agent test@example.com"
    assert [s for s in slept if s >= 1] == [1.0, 2.0]


def test_fetcher_gives_up_on_404_without_retrying() -> None:
    calls = []

    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        calls.append(1)
        raise http_error(404)

    with pytest.raises(FetchError, match="HTTP 404"):
        Fetcher("t", opener=opener, sleep=lambda s: None).get("https://x/y")
    assert len(calls) == 1


def test_fetcher_needs_user_agent() -> None:
    with pytest.raises(ValueError):
        Fetcher("  ")


# refresh end to end with a fake network -----------------------------------------------------------------------
def fake_network(fail: set[str]) -> Fetcher:
    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        url = req.full_url
        if any(f in url for f in fail):
            raise http_error(404)
        if url.endswith("company_tickers.json"):
            return fx.as_bytes(fx.tickers() | {"1": {"cik_str": 8888888, "ticker": "BAD", "title": "Bad"}})
        if "submissions" in url:
            return fx.as_bytes(fx.submissions())
        if "companyfacts" in url:
            return fx.as_bytes(fx.company_facts(fx.base_rows()))
        if "finance.yahoo.com" in url:
            ticker = url.split("/chart/", 1)[1].split("?", 1)[0]
            # Thu 6 and Fri 7 Nov 2025, 09:30 New York; the session that followed has closed.
            rows = [(1762439400, 39.0), (1762525800, 40.0)]
            return fx.as_bytes(fx.yahoo_chart(ticker, rows, 1762549200, (1762525800, 1762549200)))
        if "stooq" in url:
            return fx.as_bytes(fx.stooq_csv([("2025-11-06", 39.0), ("2025-11-07", 40.0)]))
        if "fredgraph" in url:
            series = url.rsplit("=", 1)[1]
            return fx.as_bytes(fx.fred_csv(series, [("2025-11-07", "4.10")]))
        raise AssertionError(url)

    return Fetcher("Test agent test@example.com", opener=opener, sleep=lambda s: None, min_interval={})


def test_refresh_records_failures_and_keeps_good_companies() -> None:
    store = Store(":memory:")
    res = refresh(store, ["EXW", "BAD", "MISSING"], fake_network({"CIK0008888888"}), today=date(2025, 11, 10))
    assert res["status"] == "partial"
    assert set(res["failed"]) == {"BAD", "MISSING"}
    assert store.company("EXW") is not None and store.company("BAD") is None
    run = store.runs(1)[0]
    assert run.status == "partial" and run.detail["ok"] == ["EXW"]
    snap = snapshot.build(store, "EXW", date(2025, 11, 10))
    assert not snap.blocked, snap.blocked_by
    assert store.raw(snap.values["price"].sources[0]["raw_sha256"]) is not None  # raw bytes kept


# balance dates (found in the first live run: an old total-debt figure was used in place of the current one) ----
def test_old_balance_is_never_used_for_the_current_period() -> None:
    old_total = ("us-gaap", "LongTermDebt", "USD", None, "2022-12-31", 9999 * fx.M,
                 "0000000001-23-000001", "10-K", "2023-02-14")  # fmt: skip
    snap = snapshot.build(with_market(load(fx.base_rows() + [old_total])), "EXW", date(2025, 11, 10))
    assert snap.values["debt"].value == pytest.approx(400)  # current components, not the 2022 total
    assert snap.values["netDebt"].value == pytest.approx(200)


def test_debt_missing_at_period_end_blocks_rather_than_reads_zero() -> None:
    rows = [r for r in fx.base_rows() if not (r[1].startswith("LongTermDebt") and r[4] == "2025-09-30")]
    snap = snapshot.build(with_market(load(rows)), "EXW", date(2025, 11, 10))
    assert snap.values["debt"].value is None
    assert any("missing netDebt" in b for b in snap.blocked_by)


def test_company_that_never_filed_debt_has_zero_debt_with_a_warning() -> None:
    rows = [r for r in fx.base_rows() if not r[1].startswith("LongTermDebt")]
    snap = snapshot.build(with_market(load(rows)), "EXW", date(2025, 11, 10))
    assert snap.values["debt"].value == 0
    assert snap.values["debt"].warnings


# patterns seen in the first live run (rebuilt here on the fictional company) --------------------------------
def without(rows: list[fx.Row], *concepts: str) -> list[fx.Row]:
    return [r for r in rows if r[1] not in concepts]


def flow_rows(concept: str, fy: float, ytd9_24: float, ytd9_25: float) -> list[fx.Row]:
    return [
        fx._dur(concept, "2024-01-01", "2024-12-31", fy, fx.K24),
        fx._dur(concept, "2024-01-01", "2024-09-30", ytd9_24, fx.Q3_25),
        fx._dur(concept, "2025-01-01", "2025-09-30", ytd9_25, fx.Q3_25),
    ]


def build(rows: list[fx.Row]) -> snapshot.Snapshot:
    return snapshot.build(with_market(load(rows)), "EXW", date(2025, 11, 10))


def test_figure_from_an_old_period_is_not_used() -> None:
    # Workday's last DepreciationAndAmortization was for 2019; it must not be paired with 2025 operating income.
    old = [fx._dur("DepreciationAndAmortization", "2018-11-01", "2019-10-31", 260, fx.K24)]
    snap = build(without(fx.base_rows(), "DepreciationDepletionAndAmortization") + old)
    assert snap.values["da"].value is None
    assert any("missing ebitda" in b for b in snap.blocked_by)


def test_da_from_separate_depreciation_and_amortisation_lines() -> None:
    rows = without(fx.base_rows(), "DepreciationDepletionAndAmortization")
    rows += flow_rows("Depreciation", 40, 30, 32) + flow_rows("AmortizationOfIntangibleAssets", 10, 7, 8)
    snap = build(rows)
    assert snap.values["da"].value == pytest.approx((40 + 32 - 30) + (10 + 8 - 7))
    assert snap.values["ebitda"].value == pytest.approx(230 + 53)


def test_long_term_debt_beside_debt_current_is_the_noncurrent_line() -> None:
    # Adobe files LongTermDebt for the non-current line and DebtCurrent for the current one, with no
    # LongTermDebtNoncurrent.
    rows = without(fx.base_rows(), "LongTermDebtNoncurrent", "LongTermDebtCurrent")
    rows += [
        fx._inst("LongTermDebt", "2025-09-30", 380, fx.Q3_25),
        fx._inst("DebtCurrent", "2025-09-30", 20, fx.Q3_25),
    ]
    snap = build(rows)
    assert snap.values["debt"].value == pytest.approx(400)
    assert any("non-current line" in w for w in snap.values["debt"].warnings)


def test_interest_too_high_for_the_debt_found_blocks() -> None:
    # Dropbox's term loan is filed under its own tag, so only part of its debt is visible in company facts.
    rows = fx.base_rows() + flow_rows("InterestExpense", 90, 70, 80)  # TTM 100 on 400 of debt = 25%
    snap = build(rows)
    assert snap.values["netDebt"].value is None
    assert any("debt looks incomplete" in b for b in snap.blocked_by)
    ok = build(fx.base_rows() + flow_rows("InterestExpense", 20, 15, 16))  # 21 on 400 = 5%
    assert ok.values["netDebt"].known


def test_old_share_count_is_not_used() -> None:
    # Workday's last non-dimensional cover-page count is from 2018 (it now has two share classes).
    rows = without(fx.base_rows(), "EntityCommonStockSharesOutstanding")
    rows += [("dei", "EntityCommonStockSharesOutstanding", "shares", None, "2018-11-30", 220 * fx.M, *fx.K24)]
    rows += [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "2025-07-01", "2025-09-30",
              52 * fx.M, *fx.Q3_25)]  # fmt: skip
    v = build(rows).values["shares"]
    assert v.value == pytest.approx(52) and v.warnings


def test_combined_cash_and_investments_line_and_split_leases() -> None:
    rows = fx.base_rows() + [
        fx._inst("CashCashEquivalentsAndShortTermInvestments", "2025-09-30", 205, fx.Q3_25),
        fx._inst("OperatingLeaseLiabilityCurrent", "2025-09-30", 5, fx.Q3_25),
        fx._inst("OperatingLeaseLiabilityNoncurrent", "2025-09-30", 45, fx.Q3_25),
    ]
    snap = build(rows)
    assert snap.values["liquidAssets"].value == pytest.approx(205)
    assert snap.values["netDebt"].value == pytest.approx(400 - 205)
    assert snap.values["operatingLeases"].value == pytest.approx(50)


def test_debt_roll_forward_catches_a_borrowing_we_cannot_see() -> None:
    # Dropbox drew 1.2bn on a credit line filed under its own tag; the cash-flow line still shows the draw.
    repaid = [fx._dur("RepaymentsOfLongTermDebt", "2025-01-01", "2025-09-30", 20, fx.Q3_25)]
    assert build(fx.base_rows() + repaid).values["netDebt"].known  # 420 - 20 = 400 found: reconciles
    hidden = repaid + [fx._dur("ProceedsFromLinesOfCredit", "2025-01-01", "2025-09-30", 300, fx.Q3_25)]
    snap = build(fx.base_rows() + hidden)
    assert snap.values["netDebt"].value is None
    assert any("700.0m expected, but only 400.0m found" in b for b in snap.blocked_by)


def test_api_error_reason_is_shown() -> None:
    import io

    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        raise urllib.error.HTTPError(
            "https://x", 403, "err", Message(), io.BytesIO(b'{"detail":"Invalid token."}')
        )

    with pytest.raises(FetchError, match="Invalid token"):
        Fetcher("t", opener=opener, sleep=lambda s: None).get(
            "https://x/y", headers={"Authorization": "Token s3cret"}
        )


def test_live_code_never_reads_kci_dataset() -> None:
    # KCI's tools are reused; its dataset is only a test and comparison baseline, never an input to a valuation.
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    offenders = [p for p in src.rglob("*.py") if "seed.json" in p.read_text() or "KCI_SEED" in p.read_text()]
    assert offenders == []


def test_live_snapshot_with_price_history_gets_a_beta_and_a_dcf() -> None:
    from .test_beta import fridays, market, put_series

    store = with_market(load())
    spy_days = fridays(105, date(2025, 11, 7))
    m = market(104)
    put_series(store, "SPY", spy_days, m)
    put_series(store, "EXW", spy_days[:-1], [1.2 * r for r in m[:-1]])  # ends a week earlier than the price
    snap = snapshot.build(store, "EXW", date(2025, 11, 10))
    assert snap.values["beta"].known and snap.values["beta"].kind is Kind.DERIVED
    assert snap.values["marketCap"].value == pytest.approx(2000)  # the stooq close still sets the price
    a = snapshot.analyse(snap)["analysis"]
    b = 0.67 * 1.2 + 0.33
    assert a["dcfIn"]["beta"] == pytest.approx(b)
    assert a["wacc"]["costOfEquity"] == pytest.approx(0.041 + b * 0.055 + 0.01)  # $2.0bn cap: 1% size premium
    assert a["dcf"]["ok"] is True
