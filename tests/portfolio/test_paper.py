"""Paper trading on made-up prices: fills, the ledger, positions, P&L, drawdown and the risk gate."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from keers.portfolio import analytics as A
from keers.portfolio.desk import Desk, TradeError, TradeRequest
from keers.portfolio.fills import CostModel, simulate
from keers.portfolio.ledger import Ledger, Order
from keers.portfolio.market import MarketMark, average_volume, sector_from_sic

TODAY = date(2025, 11, 10)
FRIDAY = date(2025, 11, 7)


def mm(symbol: str, price: float | None, day: date | None = FRIDAY, adv: float | None = 2_000_000.0,
       currency: str = "USD", warnings: tuple[str, ...] = (), sector: str | None = "Technology") -> MarketMark:  # fmt: skip
    return MarketMark(symbol, price, day, day.isoformat() if day else None, "yahoo quote", "sha-test", currency,
                      adv, 20, warnings, sector, "listing" if sector else "")  # fmt: skip


class Prices:
    def __init__(self) -> None:
        self.marks: dict[str, MarketMark] = {"AAA": mm("AAA", 100.0), "BBB": mm("BBB", 50.0, sector="Energy")}
        self.history: dict[str, list[tuple[date, float]]] = {}

    def mark(self, s: str) -> MarketMark:
        return self.marks.get(s, mm(s, None, None))

    def closes(self, s: str, start: date, end: date) -> list[tuple[date, float]]:
        return [(d, p) for d, p in self.history.get(s, []) if start <= d <= end]


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Desk, Prices, int]:
    led = Ledger(tmp_path / "paper.sqlite")
    pid = led.create_portfolio("Test", 1_000_000).id
    px = Prices()
    clock = {"today": TODAY}
    desk = Desk(led, px.mark, px.closes, lambda: clock["today"])
    desk.clock = clock  # type: ignore[attr-defined]
    return desk, px, pid


def req(**k: object) -> TradeRequest:
    base: dict[str, object] = {"symbol": "AAA", "strategy": "B", "side": "buy"}
    base.update(k)
    return TradeRequest.from_dict(base)


def test_fill_moves_against_us_and_charges_commission() -> None:
    c = CostModel()
    b = simulate(100, 50.0, c)
    s = simulate(-100, 50.0, c)
    assert b.price == pytest.approx(50.05) and s.price == pytest.approx(49.95)
    assert b.commission == pytest.approx(max(1.0, 100 * 50.05 * 0.0005))
    assert b.cash_change == pytest.approx(-(100 * 50.05) - b.commission)
    assert simulate(1, 10.0, c).commission == 1.0  # minimum


def test_approved_trade_fills_and_is_recorded_with_its_price_source(setup: tuple[Desk, Prices, int]) -> None:
    desk, _, pid = setup
    out = desk.execute(pid, req(weight=0.05))
    assert out["filled"] and out["risk"]["approved"]
    assert out["quantity"] == 499  # floor(50,000 / 100.10)
    o = desk.ledger.orders(pid)[0]
    assert o.status == "filled" and o.price_source == "yahoo quote" and o.price_sha256 == "sha-test"
    assert o.reference_price == 100.0 and o.fill_price == pytest.approx(100.1)
    assert o.strategy == "B" and o.sector == "Technology" and o.sector_basis == "listing"
    v = desk.view(pid)
    pos = v["positions"][0]
    assert pos["quantity"] == 499 and pos["avgCost"] == pytest.approx(100.1)
    assert pos["unrealised"] == pytest.approx((100.0 - 100.1) * 499)
    s = v["summary"]
    assert s["cash"] == pytest.approx(1_000_000 - 499 * 100.1 - o.commission)
    assert s["nav"] == pytest.approx(s["cash"] + 499 * 100.0)
    assert s["byStrategy"]["B"]["pct"] == pytest.approx(49_900 / s["nav"])
    assert v["risk"]["approved"]


def test_breach_is_rejected_logged_and_changes_nothing(setup: tuple[Desk, Prices, int]) -> None:
    desk, _, pid = setup
    out = desk.execute(pid, req(weight=0.2))
    assert not out["filled"]
    o = desk.ledger.orders(pid)[0]
    assert o.status == "rejected" and o.fill_price is None and "position.size" in o.reason
    assert desk.view(pid)["summary"]["cash"] == 1_000_000


def test_stale_or_missing_price_is_rejected(setup: tuple[Desk, Prices, int]) -> None:
    desk, px, pid = setup
    px.marks["AAA"] = mm("AAA", 100.0, day=date(2025, 10, 1))
    assert not desk.execute(pid, req(shares=10))["filled"]
    with pytest.raises(TradeError, match="no price"):
        desk.execute(pid, req(symbol="ZZZ", weight=0.01))
    out = desk.execute(pid, req(symbol="ZZZ", shares=10))  # sized in shares, but still no price to fill at
    assert not out["filled"] and desk.ledger.orders(pid)[-1].status == "rejected"


def test_preview_writes_nothing(setup: tuple[Desk, Prices, int]) -> None:
    desk, _, pid = setup
    p = desk.preview(pid, req(shares=100))
    assert p["risk"]["approved"] and p["fill"]["price"] == pytest.approx(100.1)
    assert desk.ledger.orders(pid) == []


def test_close_realises_pnl_and_flip_is_refused(setup: tuple[Desk, Prices, int]) -> None:
    desk, px, pid = setup
    assert desk.execute(pid, req(shares=400))["filled"]
    with pytest.raises(TradeError, match="flip"):
        desk.execute(pid, req(side="sell", shares=500))
    px.marks["AAA"] = mm("AAA", 110.0)
    out = desk.execute(pid, req(side="sell", close=True))
    assert out["filled"] and out["quantity"] == -400
    pos = desk.view(pid)["positions"][0]
    assert pos["quantity"] == 0 and not pos["open"]
    assert pos["realised"] == pytest.approx(400 * (110 * 0.999 - 100.1))
    with pytest.raises(TradeError, match="no open"):
        desk.execute(pid, req(side="sell", close=True))


def test_short_position_and_cover() -> None:
    p = A.Position("S", "C")

    def o(q: float, px: float) -> Order:
        return Order(1, "", "2025-11-07", "filled", "S", "C", None, "none", q, px, "t", None, "", px, 1.0, 0, 0, "",
                     "USD", "", {"approved": True})  # fmt: skip

    A.apply(p, o(-100, 50.0))
    A.apply(p, o(-100, 40.0))
    assert p.quantity == -200 and p.avg_cost == pytest.approx(45.0)
    A.apply(p, o(150, 30.0))
    assert p.quantity == -50 and p.realised == pytest.approx(150 * 15.0)
    assert p.commissions == 3.0


def test_nav_history_and_drawdown(setup: tuple[Desk, Prices, int]) -> None:
    desk, px, pid = setup
    desk.clock["today"] = date(2025, 11, 3)  # type: ignore[attr-defined]
    px.marks["AAA"] = mm("AAA", 100.0, day=date(2025, 10, 31))
    assert desk.execute(pid, req(shares=900))["filled"]
    px.history["AAA"] = [(date(2025, 11, 3), 100.0), (date(2025, 11, 4), 120.0), (date(2025, 11, 5), 90.0),
                         (date(2025, 11, 6), 95.0), (date(2025, 11, 7), 95.0)]  # fmt: skip
    desk.clock["today"] = TODAY  # type: ignore[attr-defined]
    px.marks["AAA"] = mm("AAA", 95.0)
    v = desk.view(pid)
    h = v["history"]
    assert [x["day"] for x in h] == ["2025-11-03", "2025-11-04", "2025-11-05", "2025-11-06", "2025-11-07"]
    peak = h[1]["nav"]
    assert h[2]["drawdown"] == pytest.approx(1 - h[2]["nav"] / peak)
    assert v["summary"]["maxDrawdown"] == pytest.approx(h[2]["drawdown"])
    assert v["summary"]["drawdown"] == pytest.approx(1 - v["summary"]["nav"] / peak)


def test_ledger_refuses_an_unapproved_fill(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "p.sqlite")
    pid = led.create_portfolio("X", 10_000).id
    bad = Order(pid, "", "2025-11-07", "filled", "AAA", "B", None, "none", 1, 1.0, "t", None, "", 1.0, 1.0, 0, 0,
                "", "USD", "", {"approved": False})  # fmt: skip
    with pytest.raises(ValueError, match="approved"):
        led.append(bad)
    with pytest.raises(ValueError, match="already exists"):
        led.create_portfolio("X", 10_000)


def test_request_validation() -> None:
    with pytest.raises(TradeError):
        TradeRequest.from_dict({"symbol": "A", "strategy": "B", "shares": "-3"})
    r = TradeRequest.from_dict(
        {"symbol": " aaa ", "strategy": "b", "shares": "10", "sector": "  Tech   hardware "}
    )
    assert r.symbol == "AAA" and r.strategy == "B" and r.sector == "Tech hardware"


def test_market_helpers() -> None:
    d = date(2025, 10, 1)
    rows = [(d + timedelta(days=i), 1.0, float(v), "yahoo", "") for i, v in enumerate([0, 100, 200, 300])]
    assert average_volume(rows, 20, 3) == (200.0, 3)
    assert average_volume(rows, 20, 4) == (None, 3)
    assert sector_from_sic("7372") == "Technology" and sector_from_sic("1311") == "Energy"
    assert sector_from_sic(None) is None and sector_from_sic("") is None
