"""Strategy C signals, backtest and rebalance on FICTIONAL prices (seeded random walks; nothing here is real)."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keers.data.models import PricePoint
from keers.data.store import Store
from keers.strategies.quant import backtest, model, rebalance, signals
from keers.strategies.quant.pit import PointInTime
from keers.web.app import create_app
from keers.web.service import Workbench
from tests.web import demo

START, END = date(2020, 1, 1), date(2023, 12, 29)
TICKERS = [f"T{i:02d}" for i in range(20)]


def sessions(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def walk(t: str, drift: float, vol: float, seed: int, days: list[date]) -> list[PricePoint]:
    rng, px, out = random.Random(seed), 50.0, []
    for d in days:
        px *= math.exp(drift + rng.gauss(0, vol))
        out.append(PricePoint(t, d, px, "yahoo", px, px, px, 1_000_000.0, "sha-test", px))
    return out


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "q.sqlite")
    days = sessions(START, END)
    s.put_prices(walk("SPY", 0.0003, 0.01, 1, days))
    for i, t in enumerate(TICKERS):
        s.put_prices(walk(t, 0.0001 * (i - 10), 0.005 + 0.001 * i, 100 + i, days))
    return s


def test_momentum_and_volatility_by_hand(store: Store) -> None:
    as_of = date(2022, 6, 30)
    px = [a for d, a in store.adjusted_closes("T05", as_of - timedelta(days=400), as_of) if a]
    m = signals.momentum(PointInTime(store, as_of), "T05")
    assert m.value == pytest.approx(px[-22] / px[-253] - 1)
    v = signals.volatility(PointInTime(store, as_of), "T05")
    rets = [math.log(b / a) for a, b in zip(px[-253:], px[-252:], strict=False)]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    assert v.value == pytest.approx(sd) and v.score == pytest.approx(-sd)


def test_unknown_when_history_short_or_stale(store: Store) -> None:
    assert signals.momentum(PointInTime(store, date(2020, 6, 30)), "T01").score is None  # too short
    stale = signals.momentum(PointInTime(store, date(2024, 3, 1)), "T01")
    assert stale.score is None and "business days old" in stale.reason
    rows = model.rank(PointInTime(store, date(2020, 6, 30)), TICKERS, model.Config())
    assert all(r.composite is None and r.weight == 0 for r in rows)  # unknown is never ranked


def test_future_prices_cannot_change_a_signal(store: Store) -> None:
    as_of = date(2022, 6, 30)
    before = [r.as_dict() for r in model.rank(PointInTime(store, as_of), TICKERS, model.Config())]
    # Poison everything after the as-of date with absurd prices and volumes.
    store.put_prices([PricePoint(t, d, 1e6, "yahoo", 1e6, 1e6, 1e6, 1.0, "sha-poison", 1e6)
                      for t in [*TICKERS, "SPY"] for d in sessions(as_of + timedelta(days=1), END)])  # fmt: skip
    after = [r.as_dict() for r in model.rank(PointInTime(store, as_of), TICKERS, model.Config())]
    assert before == after
    assert all(d <= as_of for d, _ in PointInTime(store, as_of).adjusted_closes("T03", 5000))


def test_book_is_top_n_equal_weight_within_caps(store: Store) -> None:
    cfg = model.Config(n_long=5)
    rows = model.rank(PointInTime(store, date(2022, 6, 30)), TICKERS, cfg)
    book = [r for r in rows if r.weight]
    assert [r.rank for r in book] == [1, 2, 3, 4, 5]
    assert all(r.weight == pytest.approx(min(cfg.sleeve / 5, cfg.max_weight)) for r in book)
    with pytest.raises(ValueError):
        model.Config(sleeve=0.5)


def test_backtest_trades_the_session_after_the_signal(store: Store) -> None:
    res = backtest.run(store, TICKERS, date(2021, 2, 1), END, model.Config(n_long=5))
    assert res["ran"] and len(res["months"]) >= 30
    for m in res["months"]:
        assert m["tradeDay"] > m["signalDay"]
    assert res["strategy"]["maxDrawdown"] >= 0 and "survivorship" in res["warnings"][0]
    assert set(res["signals"]) == {"momentum_12_1", "low_volatility"}
    free = backtest.run(store, TICKERS, date(2021, 2, 1), END, model.Config(n_long=5),
                        backtest.CostModel(slippage_bps=0, commission_bps=0))  # fmt: skip
    assert free["strategy"]["totalReturn"] > res["strategy"]["totalReturn"]  # costs are charged


def test_backtest_is_deterministic(store: Store) -> None:
    a = backtest.run(store, TICKERS, date(2021, 2, 1), END)
    b = backtest.run(store, TICKERS, date(2021, 2, 1), END)
    assert a == b


def test_rebalance_plan_sells_first_and_skips_small_moves() -> None:
    pos = [{"symbol": "OLD", "strategy": "C", "quantity": 10, "weight": 0.02},
           {"symbol": "KEEP", "strategy": "C", "quantity": 10, "weight": 0.0199},
           {"symbol": "CUT", "strategy": "C", "quantity": 10, "weight": 0.05},
           {"symbol": "B1", "strategy": "B", "quantity": 10, "weight": 0.05},
           {"symbol": "NOPX", "strategy": "C", "quantity": 10, "weight": None}]  # fmt: skip
    tickets, problems = rebalance.plan({"KEEP": 0.02, "CUT": 0.02, "NEW": 0.02, "NOPX": 0.02}, pos)
    assert [(t["symbol"], t["side"]) for t in tickets] == [("CUT", "sell"), ("OLD", "sell"), ("NEW", "buy")]
    assert tickets[1]["close"] and tickets[0]["weight"] == pytest.approx(0.03)
    assert all(t["strategy"] == "C" for t in tickets) and "NOPX" in problems[0]


def test_rebalance_goes_through_desk_and_risk(tmp_path: Path) -> None:
    db = demo.build(tmp_path / "keers.sqlite")
    c = TestClient(create_app(Workbench(str(db), today=lambda: demo.AS_OF)))
    sig = c.post("/api/strategies/c/signals", json={"tickers": ["EXW"]}).json()
    assert sig["targets"] == {"EXW": 0.05}
    pid = c.post("/api/portfolios", json={"name": "C", "startingCash": 1_000_000}).json()["id"]
    r = c.post(
        f"/api/portfolios/{pid}/strategies/c/rebalance", json={"tickers": ["EXW"], "execute": True}
    ).json()
    assert r["results"][0]["filled"] and r["results"][0]["approved"]
    v = c.get(f"/api/portfolios/{pid}").json()
    assert v["positions"][0]["strategy"] == "C" and v["orders"][0]["risk"]["approved"]
    again = c.post(
        f"/api/portfolios/{pid}/strategies/c/rebalance", json={"tickers": ["EXW"], "execute": True}
    ).json()
    assert again["results"] == []  # already at target
