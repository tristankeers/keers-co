"""The risk checks on hand-built books. Every number is made up."""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from keers.risk import checks as R
from keers.risk.limits import Limits

TODAY = date(2025, 11, 10)  # Monday
FRESH = date(2025, 11, 7)  # the Friday before


def _days(n: int) -> list[date]:
    out, d = [], FRESH
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


DAYS = _days(260)
_rng = random.Random(5)
BENCH = {d: _rng.gauss(0.0004, 0.01) for d in DAYS}  # a FICTIONAL market, about 16% a year volatility
_noise = random.Random(6)
LIKE_MARKET = {d: BENCH[d] + _noise.gauss(0, 0.005) for d in DAYS}  # beta about 1


def mark(price: float | None = 100.0, day: date | None = FRESH, adv: float | None = 1_000_000.0,
         currency: str | None = "USD", warnings: tuple[str, ...] = (),
         returns: dict[date, float] | None = LIKE_MARKET) -> R.Mark:  # fmt: skip
    return R.Mark(price, day, currency, adv, warnings, returns)


def mkbook(cash: float, holdings: tuple[R.Holding, ...] = (), nav_history: tuple[float, ...] = (),
         halted: str = "") -> R.Book:  # fmt: skip
    return R.Book(cash, holdings, nav_history, BENCH, halted)


def buy(qty: float, symbol: str = "AAA", strategy: str = "B", sector: str | None = "Technology",
        m: R.Mark | None = None) -> R.Trade:  # fmt: skip
    m = m or mark()
    return R.Trade(symbol, strategy, sector, qty, (m.price or 0) * 1.001, 1.0, m)


EMPTY = mkbook(1_000_000.0)
LIM = Limits()
# v2's single 10% position cap for every strategy, for tests written before strategy A got its own 5% cap.
WIDE = Limits(max_position_strategy={"A": 0.10, "B": 0.10, "C": 0.10})


def statuses(rep: R.Report) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in rep.checks:
        out[c.name] = c.status if out.get(c.name, "pass") in ("pass", "warn") else out[c.name]
    return out


def test_small_trade_in_an_empty_book_passes_every_check() -> None:
    rep = R.check_trade(EMPTY, buy(500), LIM, TODAY)  # about 5% of NAV
    assert rep.approved, rep.breaches
    assert set(R.TRADE_CHECKS) <= {c.name for c in rep.checks}
    assert not rep.missing


def test_position_cap() -> None:
    rep = R.check_trade(EMPTY, buy(1_200), LIM, TODAY)  # about 12% of NAV
    assert not rep.approved
    assert statuses(rep)["position.size"] == "fail"


def test_missing_price_stale_price_and_stale_quote_block() -> None:
    for m in (mark(price=None), mark(day=date(2025, 10, 30)), mark(day=None),
              mark(warnings=("stale: market is open but the last trade is 40 minutes old",))):  # fmt: skip
        rep = R.check_trade(EMPTY, buy(10, m=m), LIM, TODAY)
        assert not rep.approved
        assert statuses(rep)["data.trade_price"] == "fail"


def test_unknown_volume_sector_or_currency_never_counts_as_a_pass() -> None:
    assert (
        statuses(R.check_trade(EMPTY, buy(10, m=mark(adv=None)), LIM, TODAY))["liquidity.trade"] == "unknown"
    )
    rep = R.check_trade(EMPTY, buy(10, sector=None), LIM, TODAY)
    assert not rep.approved and statuses(rep)["concentration.sector"] == "unknown"
    rep = R.check_trade(EMPTY, buy(10, m=mark(currency=None)), LIM, TODAY)
    assert not rep.approved and statuses(rep)["data.currency"] == "unknown"
    rep = R.check_trade(EMPTY, buy(10, m=mark(currency="GBP")), LIM, TODAY)
    assert not rep.approved and statuses(rep)["data.currency"] == "fail"


def test_bad_strategy_tag_blocks() -> None:
    rep = R.check_trade(EMPTY, buy(10, strategy="D"), LIM, TODAY)
    assert not rep.approved and statuses(rep)["data.strategy"] == "fail"


def test_liquidity_share_of_adv() -> None:
    rep = R.check_trade(EMPTY, buy(500, m=mark(adv=4_000)), LIM, TODAY)  # 12.5% of ADV
    assert statuses(rep)["liquidity.trade"] == "fail"
    held = mkbook(950_000.0, (R.Holding("AAA", "B", "Technology", 500, mark(adv=4_000)),))
    rep = R.check_trade(held, buy(-500, m=mark(adv=4_000)), LIM, TODAY)  # selling it all is allowed
    assert statuses(rep)["liquidity.trade"] == "warn"


def test_sector_and_strategy_concentration_add_up_across_positions() -> None:
    hs = tuple(R.Holding(s, "A", "Energy", 900, mark()) for s in ("E1", "E2", "E3"))  # 3 × 9% of NAV
    book = mkbook(1_000_000.0 - 270_000.0, hs)
    rep = R.check_trade(book, buy(900, "E4", "A", "Energy"), WIDE, TODAY)
    st = statuses(rep)
    assert st["concentration.sector"] == "fail"  # 36% > 25%
    assert st["position.size"] == "pass"
    rep = R.check_trade(book, buy(900, "T1", "A", "Technology"), WIDE, TODAY)
    st = statuses(rep)
    assert (
        st["concentration.sector"] == "pass" and st["concentration.strategy"] == "pass"
    )  # A sleeve 36% ≤ 40%
    rep = R.check_trade(
        book,
        buy(900, "T1", "A", "Technology"),
        Limits(
            max_strategy={"A": 0.30, "B": 0.6, "C": 0.4}, max_position_strategy=WIDE.max_position_strategy
        ),
        TODAY,
    )
    assert statuses(rep)["concentration.strategy"] == "fail"


def test_gross_and_net_exposure() -> None:
    longs = tuple(R.Holding(f"L{i}", "B", f"S{i}", 900, mark()) for i in range(10))  # 90% long
    book = mkbook(1_000_000.0 - 900_000.0, longs)
    assert statuses(R.check_trade(book, buy(900, "L10", "C", "S10"), LIM, TODAY))["exposure.net"] == "pass"
    tight = Limits(max_net=0.8)
    assert statuses(R.check_trade(book, buy(900, "L10", "C", "S10"), tight, TODAY))["exposure.net"] == "fail"
    assert statuses(R.check_trade(book, buy(-900, "L9", "B", "S9"), tight, TODAY))["exposure.net"] == "warn"
    net_short = mkbook(
        1_000_000.0 + 270_000.0, tuple(R.Holding(f"S{i}", "C", f"X{i}", -900, mark()) for i in range(3))
    )
    assert (
        statuses(R.check_trade(net_short, buy(-900, "S3", "C", "X3"), LIM, TODAY))["exposure.net"] == "fail"
    )
    shorts = tuple(R.Holding(f"S{i}", "C", f"X{i}", -900, mark()) for i in range(6))  # 54% short
    book2 = mkbook(book.cash + 540_000.0, longs + shorts)
    rep = R.check_trade(book2, buy(-900, "S6", "C", "X6"), LIM, TODAY)
    assert statuses(rep)["exposure.gross"] == "fail"  # 153% > 150%


def test_no_borrowing() -> None:
    rep = R.check_trade(mkbook(5_000.0), buy(60), LIM, TODAY)
    assert statuses(rep)["cash.no_borrowing"] == "fail"


def test_drawdown_halt_blocks_new_risk_but_allows_reductions() -> None:
    h = R.Holding("AAA", "B", "Technology", 800, mark())
    book = mkbook(700_000.0, (h,), nav_history=(1_000_000.0, 1_100_000.0))  # NAV 780k vs peak 1.1m: 29% down
    rep = R.check_trade(book, buy(10), LIM, TODAY)
    assert statuses(rep)["drawdown"] == "fail" and not rep.approved
    rep = R.check_trade(book, buy(-800), LIM, TODAY)
    assert statuses(rep)["drawdown"] == "warn"
    assert statuses(R.assess(book, LIM, TODAY))["drawdown"] == "fail"


def test_reducing_a_breached_position_passes_but_adding_fails() -> None:
    h = R.Holding("AAA", "B", "Technology", 2_000, mark())  # 20% of NAV
    book = mkbook(800_000.0, (h,))
    assert statuses(R.check_trade(book, buy(-500), LIM, TODAY))["position.size"] == "warn"
    assert statuses(R.check_trade(book, buy(10), LIM, TODAY))["position.size"] == "fail"
    assert statuses(R.assess(book, LIM, TODAY))["position.size"] == "fail"


def test_stale_holding_blocks_every_trade() -> None:
    old = R.Holding("OLD", "A", "Energy", 100, mark(day=date(2025, 10, 1)))
    rep = R.check_trade(mkbook(990_000.0, (old,)), buy(10), LIM, TODAY)
    assert statuses(rep)["data.book_prices"] == "fail" and not rep.approved


def test_missing_check_or_crash_means_no_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = R.check_trade(EMPTY, buy(10), LIM, TODAY)
    assert rep.approved
    partial = R.Report(tuple(c for c in rep.checks if c.name != "drawdown"), rep.required, rep.limits_version)
    assert not partial.approved and partial.missing == ["drawdown"]
    assert not R.Report((), R.TRADE_CHECKS, "x").approved

    def boom(*a: object, **k: object) -> R.Check:
        raise RuntimeError("bug")

    monkeypatch.setattr(R, "_net_check", boom)
    rep = R.check_trade(EMPTY, buy(10), LIM, TODAY)
    assert not rep.approved
    assert any(
        c.name == "exposure.net" and c.status == "fail" and "could not run" in c.detail for c in rep.checks
    )


def test_limits_are_validated_and_files_must_be_complete(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Limits(max_position=0)
    with pytest.raises(ValueError):
        Limits(max_strategy={"A": 0.4})
    with pytest.raises(ValueError):
        Limits(drawdown_warn=0.3, drawdown_halt=0.2)
    with pytest.raises(ValueError):
        Limits(min_beta=1.5, max_beta=1.2)
    with pytest.raises(ValueError):
        Limits(min_risk_days=300)
    f = tmp_path / "limits.json"
    d = Limits().as_dict()
    f.write_text(json.dumps(d))
    assert Limits.from_json(f) == Limits()
    del d["max_sector"]
    f.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="max_sector"):
        Limits.from_json(f)


def test_risk_module_does_not_import_the_trade_proposers() -> None:
    src = Path(R.__file__).read_text() + (Path(R.__file__).parent / "limits.py").read_text()
    for name in ("keers.portfolio", "keers.strategies", "keers.web", "keers.valuation"):
        assert name not in src


def test_beta_and_volatility_limits() -> None:
    # 9% in each of 10 names that move with the market: net beta about 0.9, volatility about 15%.
    hs = tuple(R.Holding(f"M{i}", "ABC"[i % 3], f"S{i}", 900, mark()) for i in range(10))
    b = mkbook(1_000_000.0 - 900_000.0, hs)
    m = R.market_risk(b, LIM)
    assert 0.8 < m.beta < 1.0 and 0.1 < m.volatility < 0.2 and m.sessions == LIM.risk_days
    assert R.assess(b, WIDE, TODAY).approved
    tight = Limits(max_beta=0.5, max_volatility=0.10)
    rep = R.check_trade(b, buy(100, symbol="M0", strategy="A", sector="S0"), tight, TODAY)
    assert statuses(rep)["exposure.beta"] == "fail" and statuses(rep)["volatility"] == "fail"
    cut = R.check_trade(b, buy(-500, symbol="M0", strategy="A", sector="S0"), tight, TODAY)
    assert (
        statuses(cut)["exposure.beta"] == "warn" and statuses(cut)["volatility"] == "warn"
    )  # cutting is allowed


def test_beta_by_hand() -> None:
    double = {d: 2 * r for d, r in BENCH.items()}
    assert R.beta(double, BENCH, DAYS, LIM) == pytest.approx(2.0)
    assert R.beta({d: double[d] for d in DAYS[-50:]}, BENCH, DAYS, LIM) is None  # too short a history


def test_short_history_or_no_benchmark_makes_market_risk_unknown() -> None:
    short = {d: r for d, r in LIKE_MARKET.items() if d in DAYS[-60:]}
    rep = R.check_trade(EMPTY, buy(10, m=mark(returns=short)), LIM, TODAY)
    assert not rep.approved and statuses(rep)["exposure.beta"] == "unknown"
    assert statuses(rep)["volatility"] == "unknown"
    rep = R.check_trade(R.Book(1_000_000.0), buy(10), LIM, TODAY)
    assert not rep.approved and statuses(rep)["exposure.beta"] == "unknown"
    assert (
        statuses(R.check_trade(EMPTY, buy(10, m=mark(returns=None)), LIM, TODAY))["volatility"] == "unknown"
    )


def test_kill_switch_blocks_new_risk_but_allows_cutting() -> None:
    h = R.Holding("AAA", "B", "Technology", 500, mark())
    halted = mkbook(950_000.0, (h,), halted="halted by the person")
    assert statuses(R.check_trade(halted, buy(10), LIM, TODAY))["kill_switch"] == "fail"
    rep = R.check_trade(halted, buy(-100), LIM, TODAY)
    assert rep.approved and statuses(rep)["kill_switch"] == "warn"
    assert statuses(R.assess(halted, LIM, TODAY))["kill_switch"] == "warn"
    assert statuses(R.check_trade(mkbook(950_000.0, (h,)), buy(10), LIM, TODAY))["kill_switch"] == "pass"


# risk-limits-v3 ----------------------------------------------------------------------------------------------
def test_strategy_a_positions_are_capped_at_five_percent() -> None:
    a = statuses(R.check_trade(EMPTY, buy(700, strategy="A"), LIM, TODAY))  # about 7% of NAV
    b = statuses(R.check_trade(EMPTY, buy(700, strategy="B"), LIM, TODAY))
    assert a["position.size"] == "fail" and b["position.size"] == "pass"
    assert statuses(R.check_trade(EMPTY, buy(450, strategy="A"), LIM, TODAY))["position.size"] == "pass"
    # the lower of the two caps applies
    tight = Limits(max_position=0.03)
    assert statuses(R.check_trade(EMPTY, buy(450, strategy="A"), tight, TODAY))["position.size"] == "fail"
    held = mkbook(1_000_000.0 - 70_000.0, (R.Holding("AAA", "A", "Technology", 700, mark()),))
    assert any(c.name == "position.size" and c.status == "fail" for c in R.assess(held, LIM, TODAY).checks)


def test_short_sales_below_the_minimum_price_are_refused_and_others_flagged() -> None:
    cheap = R.check_trade(EMPTY, buy(-100, m=mark(price=3.0)), LIM, TODAY)
    assert statuses(cheap)["short_sale"] == "fail" and not cheap.approved
    short = R.check_trade(EMPTY, buy(-100), LIM, TODAY)
    assert statuses(short)["short_sale"] == "warn" and short.approved
    assert "borrow" in next(c.detail for c in short.checks if c.name == "short_sale")
    assert statuses(R.check_trade(EMPTY, buy(100, m=mark(price=3.0)), LIM, TODAY))["short_sale"] == "pass"
    # covering a short in a cheap share is never blocked by this check
    held = mkbook(1_000_000.0 + 300.0, (R.Holding("AAA", "B", "Technology", -100, mark(price=3.0)),))
    cover = R.check_trade(held, buy(100, m=mark(price=3.0)), LIM, TODAY)
    assert statuses(cover)["short_sale"] == "pass"


def test_v3_limits_file_must_give_the_new_keys() -> None:
    d = LIM.as_dict()
    assert d["version"] == "risk-limits-v3" and d["min_short_price"] == 5.0
    d.pop("min_short_price")
    with pytest.raises(ValueError, match="missing"):
        Limits.from_dict(d)
    with pytest.raises(ValueError, match="max_position_strategy"):
        Limits(max_position_strategy={"A": 0.05})
    with pytest.raises(ValueError, match="min_short_price"):
        Limits(min_short_price=-1)
