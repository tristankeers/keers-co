"""Strategy A: runway, funding gap and recovery value by hand, the candidate list, the A rules, and a full run on
the FICTIONAL company (Example Widgets, EXW). Offline; every number is made up."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from keers.agents import committee, roles
from keers.agents import facts as F
from keers.data.models import Kind, PricePoint, Value, missing
from keers.data.store import Store
from keers.strategies import distress
from keers.strategies.distressed import analysis as A
from keers.strategies.distressed import candidates as C
from keers.strategies.distressed import pipeline
from keers.web.service import Workbench
from tests.data import fixtures as fx
from tests.strategies.test_distress import AS_OF, full_store
from tests.web import demo


def val(x: float, unit: str = "USDm") -> Value:
    return Value(x, Kind.REPORTED, unit, "test")


UNK = missing("USDm", "not filed")


# runway and funding gap -----------------------------------------------------------------------------------------
def test_runway_by_hand() -> None:
    r = A.runway(val(-110), val(35), val(200), val(40), "USDm")
    burn = (110 + 35) / 12
    assert r["value"] == pytest.approx(240 / burn) and r["level"] == "watch"  # about 20 months
    assert r["freeCashFlow"] == pytest.approx(-145)
    assert (
        A.runway(val(-110), val(35), val(100), UNK, "USDm")["level"] == "severe"
    )  # 8 months, no credit lines
    pos = A.runway(val(170), val(35), val(200), None, "USDm")
    assert pos["level"] == "clear" and pos["value"] is None and "no cash is burned" in pos["reason"]


def test_runway_unknown_never_clear() -> None:
    assert A.runway(UNK, val(35), val(200), None, "USDm")["level"] == "unknown"
    assert A.runway(val(-10), UNK, val(200), None, "USDm")["level"] == "unknown"
    assert A.runway(val(-10), val(5), UNK, None, "USDm")["level"] == "unknown"


def test_funding_gap_by_hand() -> None:
    short = A.funding_gap(val(-110), val(35), val(200), val(40), val(300))
    assert short["level"] == "severe" and short["value"] == pytest.approx(200 + 40 - 145 - 300)
    ok = A.funding_gap(val(170), val(35), val(200), None, val(20))
    assert ok["level"] == "clear" and ok["value"] == pytest.approx(200 + 135 - 20)
    assert A.funding_gap(val(170), val(35), val(200), None, UNK)["level"] == "unknown"


# recovery value -------------------------------------------------------------------------------------------------
def rec(ebitda: float = 100, debt: float = 700, cash: float = 100, shares: float = 50, price: float = 4.0,
        **kw: Any) -> dict[str, Any]:  # fmt: skip
    return A.recovery(val(ebitda), val(debt), val(debt - cash), val(cash), val(shares, "m shares"),
                      val(price, "USD"), kw.get("same", True))  # fmt: skip


def test_recovery_cases_by_hand() -> None:
    r = rec()
    lo, mid, hi = (r["cases"][k] for k in ("low", "mid", "high"))
    assert lo["equity"] == 0.0 and lo["debtCovered"] == pytest.approx(500 / 700) and lo["vsPrice"] == -1.0
    assert mid["equity"] == pytest.approx(0.0) and mid["debtCovered"] == pytest.approx(1.0)  # 700 vs 700
    assert hi["perShare"] == pytest.approx(200 / 50) and hi["vsPrice"] == pytest.approx(0.0)
    assert r["level"] == "severe"  # nothing left for equity at the mid multiple
    up = rec(ebitda=150)  # mid: 900 + 100 - 700 = 300, 6.00 a share against 4.00
    assert up["level"] == "clear" and up["value"] == pytest.approx(0.5)
    assert rec(ebitda=120)["level"] == "watch"  # mid: 720 + 100 - 700 = 120, 2.40 a share against 4.00


def test_recovery_unknown_cases() -> None:
    neg = rec(ebitda=-20)
    assert neg["level"] == "unknown" and neg.get("ebitdaNegative") and "not positive" in neg["reason"]
    assert rec(same=False)["level"] == "unknown"
    bad = A.recovery(val(100), val(700), missing("USDm", "net debt: roll-forward failed"), val(100),
                     val(50, "m shares"), val(4.0, "USD"), True)  # fmt: skip
    assert bad["level"] == "unknown" and "roll-forward" in bad["reason"]


# end to end on the fixture store --------------------------------------------------------------------------------
def with_cfo(store: Store, fy: float, ytd24: float, ytd25: float) -> Store:
    from keers.data.sec import parse_company_facts

    rows = [fx._dur("NetCashProvidedByUsedInOperatingActivities", "2024-01-01", "2024-12-31", fy, fx.K24),
            fx._dur("NetCashProvidedByUsedInOperatingActivities", "2024-01-01", "2024-09-30", ytd24, fx.Q3_25),
            fx._dur("NetCashProvidedByUsedInOperatingActivities", "2025-01-01", "2025-09-30", ytd25, fx.Q3_25)]  # fmt: skip
    store.put_facts(parse_company_facts(fx.company_facts(rows), "sha-cfo"))
    return store


def test_analyse_end_to_end() -> None:
    res = A.analyse(with_cfo(full_store(), -100, -70, -80), fx.TICKER, AS_OF)
    assert res["ran"] and res["analysisVersion"] == "a-analysis-v1"
    p = {x["name"]: x for x in res["parts"]}
    # CFO TTM -100 + 70 - 80 = -110; capex TTM 30 - 20 + 25 = 35; cash + STI 170 + 30 = 200
    assert p["runway"]["freeCashFlow"] == pytest.approx(-145)
    assert p["runway"]["value"] == pytest.approx(200 / (145 / 12)) and p["runway"]["level"] == "watch"
    assert p["fundingGap"]["value"] == pytest.approx(200 - 145 - 20) and p["fundingGap"]["level"] == "clear"
    mid = p["recovery"]["cases"]["mid"]
    assert mid["value"] == pytest.approx(283 * 6 + 200) and mid["equity"] == pytest.approx(
        283 * 6 + 200 - 400
    )
    assert mid["perShare"] == pytest.approx((283 * 6 - 200) / 50)


def test_analyse_without_cash_flow_is_unknown_not_clear() -> None:
    p = {x["name"]: x for x in A.analyse(full_store(), fx.TICKER, AS_OF)["parts"]}
    assert p["runway"]["level"] == "unknown" and "cash from operations" in p["runway"]["reason"]
    assert A.analyse(full_store(), fx.TICKER, date(2026, 3, 1))["ran"] is False  # stale


# candidates -----------------------------------------------------------------------------------------------------
def row(t: str, status: str, levels: dict[str, str], mcap: float | None = 500.0, cur: str = "USD",
        drop: float = -0.5) -> dict[str, Any]:  # fmt: skip
    levels = {
        "altmanZ": "clear"
    } | levels  # the screen always reports the Z-score's inputs, market value among them
    checks = [{"name": n, "level": lvl, "reason": f"{n} {lvl}", "value": drop if n == "drawdown" else None,
               "inputs": {"marketCap": {"value": mcap}} if n == "altmanZ" else {}} for n, lvl in levels.items()]  # fmt: skip
    return {"ticker": t, "name": t, "status": status, "currency": cur, "checks": checks}


def priced(tickers: dict[str, tuple[float, float]]) -> Store:
    st = Store(":memory:")
    for t, (px, vol) in tickers.items():
        d, pts = AS_OF - timedelta(days=40), []
        while d < AS_OF:
            if d.weekday() < 5:
                pts.append(PricePoint(t, d, px, "yahoo", volume=vol, raw_sha256="s"))
            d += timedelta(days=1)
        st.put_prices(pts)
    return st


def test_candidates_filter_rank_and_explain() -> None:
    st = priced({"AAA": (10.0, 500_000), "BBB": (10.0, 500_000), "CCC": (0.5, 9_000_000), "DDD": (10.0, 50_000),
                 "EEE": (10.0, 500_000)})  # fmt: skip
    screen = {"screenVersion": "distress-v1", "companies": [
        row("AAA", "stressed", {"interestCover": "severe", "drawdown": "clear"}, drop=-0.2),
        row("BBB", "distressed", {"interestCover": "severe", "netLeverage": "severe", "drawdown": "watch"}),
        row("CCC", "distressed", {"interestCover": "severe", "netLeverage": "severe"}),  # penny stock
        row("DDD", "stressed", {"interestCover": "severe"}),  # 0.5m traded a day
        row("EEE", "stressed", {"interestCover": "severe"}, mcap=None),  # market value unknown
        row("FFF", "incomplete", {"altmanZ": "unknown"}),
        row("GGG", "no flags", {"altmanZ": "clear"}),
        {"ticker": "HHH", "status": "blocked", "blockedBy": ["stale price"], "checks": []},
    ]}  # fmt: skip
    out = C.build(st, screen, AS_OF)
    assert [c["ticker"] for c in out["candidates"]] == ["BBB", "AAA"]  # severity 5, then 2
    assert out["candidates"][0]["rank"] == 1 and out["candidates"][0]["dollarAdvM"] == pytest.approx(5.0)
    why = {x["ticker"]: " ".join(x["excludedBecause"]) for x in out["excluded"]}
    assert "below 1.00" in why["CCC"] and "0.50m a day" in why["DDD"] and "market value unknown" in why["EEE"]
    assert [x["ticker"] for x in out["needsData"]] == ["FFF"] and out["blocked"][0]["ticker"] == "HHH"
    assert "GGG" not in str(out["candidates"] + out["excluded"])


def test_candidates_exclude_other_currencies_and_list_changes() -> None:
    st = priced({"AAA": (10.0, 500_000), "BBB": (10.0, 500_000), "VVV": (10.0, 500_000)})
    prev = {"asOf": "2025-10-01", "candidates": [{"ticker": "AAA", "status": "stressed"},
                                                  {"ticker": "ZZZ", "status": "stressed"}]}  # fmt: skip
    screen = {"companies": [row("AAA", "distressed", {"interestCover": "severe", "netLeverage": "severe"}),
                            row("BBB", "stressed", {"interestCover": "severe"}),
                            row("VVV", "stressed", {"interestCover": "severe"}, cur="EUR")]}  # fmt: skip
    out = C.build(st, screen, AS_OF, prev)
    assert "US dollars only" in out["excluded"][0]["excludedBecause"][0]
    ch = out["changes"]
    assert ch["new"] == ["BBB"] and ch["dropped"] == [{"ticker": "ZZZ", "now": "no flags or not screened"}]
    assert ch["statusChanged"] == [{"ticker": "AAA", "from": "stressed", "to": "distressed"}]


# the A rules ----------------------------------------------------------------------------------------------------
def sheet(**facts: Any) -> F.FactSheet:
    s = F.FactSheet("EXW", "Example", "2025-11-10")
    s.a_analysis_ran = True
    units = {"vsPrice": "ratio"}
    for k, v in facts.items():
        key = k.replace("__", ".")
        s.add(F.Fact(key, key, v, units.get(key.rsplit(".", 1)[-1], "text"), "engine", "test", "test"))
    return s


def long_sheet(**over: Any) -> F.FactSheet:
    base = {"distress__status": "stressed", "distress__interestCover": "severe",
            "distress__interestCover__reason": "cover thin", "a__runway": "clear", "a__runway__reason": "fine",
            "a__fundingGap": "clear", "a__fundingGap__reason": "funded", "a__recovery": "clear",
            "a__recovery__reason": "value", "a__recovery__low__vsPrice": 0.05, "a__recovery__mid__vsPrice": 0.6,
            "a__recovery__high__vsPrice": 1.2}  # fmt: skip
    return sheet(**(base | over))


def test_rules_a_long_and_its_conviction() -> None:
    case = roles.rules_analyst(long_sheet(), "A")
    assert case["stance"] == "long" and case["conviction"] == "medium"
    assert roles.rules_analyst(long_sheet(a__recovery__low__vsPrice=-0.4), "A")["conviction"] == "low"
    for over in ({"a__recovery__mid__vsPrice": 0.2}, {"a__fundingGap": "severe"}, {"a__runway": "severe"},
                 {"a__runway": "unknown"}, {"distress__interestCover": "clear"}):  # fmt: skip
        assert roles.rules_analyst(long_sheet(**over), "A")["stance"] == "no_trade", over


def short_sheet(**over: Any) -> F.FactSheet:
    base = {"distress__status": "distressed", "distress__interestCover": "severe", "distress__netLeverage": "severe",
            "distress__interestCover__reason": "cover thin", "distress__netLeverage__reason": "too much debt",
            "a__runway": "severe", "a__runway__reason": "eight months", "a__fundingGap": "severe",
            "a__fundingGap__reason": "short", "a__recovery": "severe", "a__recovery__reason": "nothing left",
            "a__recovery__low__vsPrice": -1.0, "a__recovery__mid__vsPrice": -0.8,
            "a__recovery__high__vsPrice": -0.3}  # fmt: skip
    return sheet(**(base | over))


def test_rules_a_short_and_bear_objections() -> None:
    s = short_sheet()
    case = roles.rules_analyst(s, "A")
    assert case["stance"] == "short" and case["conviction"] == "medium"
    bear = roles.rules_bear(s, "A", case)
    assert any("Borrowing the shares" in o["claim"] for o in bear["objections"])
    s2 = short_sheet(a__recovery__high__vsPrice=0.4, a__runway="clear")
    case2 = roles.rules_analyst(s2, "A")  # still short on the funding gap, low conviction
    assert case2["stance"] == "short" and case2["conviction"] == "low"
    sev = {o["severity"] for o in roles.rules_bear(s2, "A", case2)["objections"]}
    assert "major" in sev
    no_value = short_sheet(a__recovery="unknown", a__recovery__reason="EBITDA is not positive (-20m)",
                           a__recovery__mid__vsPrice=None, a__recovery__high__vsPrice=None)  # fmt: skip
    assert roles.rules_analyst(no_value, "A")["stance"] == "short"


def test_a_decision_needs_its_analysis_and_shorts_are_small() -> None:
    s = short_sheet()
    case = roles.rules_analyst(s, "A")
    d = committee.decide(s, "A", case, {"verdict": "concerns", "objections": [], "missing": []})
    assert d["propose"] and d["side"] == "sell" and d["weight"] == committee.SIZING["low"]
    s.a_analysis_ran = False
    d = committee.decide(s, "A", case, {"verdict": "proceed", "objections": [], "missing": []})
    assert not d["propose"] and "strategy A analysis" in d["reasons"][0]
    assert roles.rules_analyst(s, "A")["stance"] == "no_trade"


# the whole run on the fictional company ------------------------------------------------------------------------
@pytest.fixture
def stressed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Workbench:
    # Make the healthy demo company look distressed (cover 5.3x and leverage 0.7x both "severe") and value it at
    # high multiples, so the run has a long case to carry through the desk. Test thresholds only.
    monkeypatch.setattr(distress, "COVER_SEVERE", 10.0)
    monkeypatch.setattr(distress, "COVER_WATCH", 12.0)
    monkeypatch.setattr(distress, "LEVERAGE_SEVERE", 0.5)
    monkeypatch.setattr(A, "RECOVERY_MULTIPLES", {"low": 10.0, "mid": 14.0, "high": 18.0})
    return Workbench(str(demo.build(tmp_path / "keers.sqlite")), today=lambda: demo.AS_OF)


def test_workbench_and_fact_sheet_carry_the_a_analysis(stressed: Workbench) -> None:
    d = stressed.distress(fx.TICKER)
    assert d["strategyA"]["ran"] and {p["name"] for p in d["strategyA"]["parts"]} == {"runway", "fundingGap",
                                                                                         "recovery"}  # fmt: skip
    s = F.build(stressed, fx.TICKER)
    assert s.a_analysis_ran and s.facts["a.runway"].value == "clear"
    assert s.facts["a.recovery.mid.multiple"].label == "assumption"
    assert s.facts["distress.interestCover.value"].unit == "x"
    assert s.facts["a.freeCashFlow"].value == pytest.approx(170 - 35)


def test_pipeline_end_to_end(stressed: Workbench, tmp_path: Path) -> None:
    pid = int(stressed.create_portfolio("A", 1_000_000)["id"])
    out = tmp_path / "out"
    res = pipeline.run(stressed, [fx.TICKER, "NOPE"], demo.AS_OF, 5, None, "test", pid, False, None, out)
    cands = res["candidates"]
    assert [c["ticker"] for c in cands["candidates"]] == [fx.TICKER]
    assert cands["blocked"][0]["ticker"] == "NOPE"
    r = res["research"][0]
    assert r["stance"] == "long" and r["analystSource"] == "rules-v2" and r["propose"]
    assert r["trade"] and r["trade"]["approved"] and r["trade"]["executed"] is False
    assert stressed.portfolio(pid)["orders"] == []  # previews write nothing
    md = (out / "strategy-a.md").read_text()
    assert "## Candidates" in md and "would pass risk" in md and (out / r["memo"]).exists()
    memo = (out / r["memo"]).read_text()
    assert "Equity value per share against the last close, mid case" in memo
    # the next run lists what changed
    import json

    prev = json.loads((out / "candidates.json").read_text())
    again = pipeline.run(stressed, [fx.TICKER], demo.AS_OF, 0, None, "test", None, False, prev, None)
    assert again["candidates"]["changes"]["new"] == [] and again["research"] == []


def test_pipeline_refuses_execute_without_a_portfolio(stressed: Workbench) -> None:
    with pytest.raises(committee.RunError):
        pipeline.run(stressed, [fx.TICKER], demo.AS_OF, execute=True)
