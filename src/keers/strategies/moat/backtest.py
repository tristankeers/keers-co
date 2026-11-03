"""Quarterly backtest of the Strategy B moat screen, with no look-ahead (``moat-backtest-v1``).

At the last session of each calendar quarter the screen runs on every company in the universe as the market knew
it that day: ``Fundamentals`` reads only facts filed by then, and the snapshot only prices up to then. The book
holds every company whose status is in ``Config.hold`` (``compounder`` and ``candidate``), equal weight, bought at
the NEXT session's close, so a signal never earns the return of the day it was seen. Between rebalances the
holdings drift with their prices. Turnover pays ``costs-v1`` slippage plus commission.

With ``members`` (the point-in-time S&P 500 from ``keers.strategies.quant.history``), each quarter screens only the
companies in the index on the signal day, including companies that later left it, so delisted names are not
missing (no survivorship bias from today's list).

Comparisons: SPY held throughout, any extra benchmarks (RSP, the equal-weight S&P 500), and an equal-weight book of
every company the screen could judge that quarter (status not ``blocked``), with the same timing and costs, which
isolates what the screen adds. Also the average next-quarter return of each status, and the held book minus the
equal-weight book per quarter.

The overfitting statistics are ``gate-v2``'s (Deflated Sharpe against Strategy B's trial register,
``config/research/trials.json``). They are reported, not used as a gate: B trades come from the research committee.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from keers.data.store import Store
from keers.portfolio.fills import BPS, CostModel
from keers.strategies.quant import gate
from keers.strategies.quant.backtest import _Book, _price_on, _t, month_ends, stats
from keers.strategies.quant.backtest import verdict as compare

from . import screener as S

BACKTEST_VERSION = "moat-backtest-v1"
BENCHMARK = "SPY"


@dataclass(frozen=True)
class Config:
    version: str = "moat-b-v1"
    screen: str = S.SCREEN_VERSION
    hold: tuple[str, ...] = S.PASSING
    weighting: str = "equal"
    rebalance: str = "quarterly"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hold"] = list(self.hold)
        return d


def quarter_ends(sessions: Sequence[date]) -> list[date]:
    return [d for d in month_ends(sessions) if d.month in (3, 6, 9, 12)]


@dataclass
class Quarter:
    signal_day: date
    trade_day: date
    status: dict[str, str] = field(default_factory=dict)  # ticker -> screen status
    blocked: dict[str, list[str]] = field(default_factory=dict)  # why companies were blocked -> tickers


def block_reason(why: Sequence[str]) -> str:
    """A short label for a block: the first reason up to its colon, e.g. ``stale fundamentals``."""
    if not why:
        return "unknown"
    first = why[0]
    if "company not in the store" in first:
        return "company not in the store"
    return first.split(":", 1)[0]


def screen_quarter(store: Store, tickers: Sequence[str], day: date) -> tuple[dict[str, str], dict[str, list[str]]]:
    status: dict[str, str] = {}
    blocked: dict[str, list[str]] = {}
    for t in tickers:
        r = S.screen(store, t, day, detail=False)
        status[t] = r["status"]
        if r["status"] == "blocked":
            blocked.setdefault(block_reason(r.get("blockedBy") or []), []).append(t)
    return status, blocked


def run(store: Store, tickers: Sequence[str], start: date, end: date, cfg: Config | None = None,
        costs: CostModel | None = None, benchmark: str = BENCHMARK,
        members: Callable[[date], frozenset[str]] | None = None, extra_benchmarks: Sequence[str] = (),
        progress: Callable[[str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    cfg, costs = cfg or Config(), costs or CostModel()
    if costs.model != "flat":
        raise ValueError("the moat backtest prices turnover with costs-v1 only (quarterly turnover is small)")
    cost_rate = (costs.slippage_bps + costs.commission_bps) * BPS
    tickers = sorted({t.upper() for t in tickers} - {benchmark})
    store.cache_facts()
    bench = [(d, a) for d, a in store.adjusted_closes(benchmark, start - timedelta(days=10), end) if a]
    sessions = [d for d, _ in bench if d >= start]
    warnings = (
        ["universe: index members on each signal date (point in time), including companies that later left"]
        if members
        else ["survivorship bias: the universe is today's ticker list, so delisted companies are missing"]
    )
    rebal = quarter_ends(sessions)
    if len(rebal) < 2:
        return {"ran": False, "reason": f"fewer than two quarter ends between {start} and {end} in {benchmark} prices",
                "warnings": warnings}  # fmt: skip
    panel: dict[str, dict[date, float]] = {
        t: {d: a for d, a in store.adjusted_closes(t, start, end) if a}
        for t in [*tickers, benchmark, *extra_benchmarks]
    }
    pdays = {t: sorted(s) for t, s in panel.items()}
    nxt = {d: sessions[i + 1] for i, d in enumerate(sessions[:-1])}
    quarters: list[Quarter] = []
    for sig in rebal:
        if sig not in nxt:
            continue
        names = tickers if members is None else sorted(members(sig) & set(tickers))
        q = Quarter(sig, nxt[sig], *screen_quarter(store, names, sig))
        quarters.append(q)
        if progress:
            n = {s: sum(1 for x in q.status.values() if x == s) for s in S.STATUSES}
            progress(f"{sig}: {len(names)} screened, " + ", ".join(f"{k} {v}" for k, v in n.items() if v))
            for why, ts in sorted(q.blocked.items(), key=lambda kv: -len(kv[1])):
                progress(f"    blocked, {why}: {len(ts)} (e.g. {' '.join(ts[:8])})")
    by_trade = {q.trade_day: q for q in quarters}

    book, ew = _Book(), _Book()
    curve: list[tuple[date, float]] = []
    ew_curve: list[tuple[date, float]] = []
    rf: list[float] = []
    rf_now, started = 0.0, False
    log: list[dict[str, Any]] = []
    for d in sessions:
        prices = {t: panel[t][d] for t in panel if d in panel[t]}
        if started:
            book.mark(prices)
            ew.mark(prices)
        today = by_trade.get(d)
        if today is not None:
            q = today
            held = sorted(t for t, s in q.status.items() if s in cfg.hold and t in prices)
            judged = sorted(t for t, s in q.status.items() if s != "blocked" and t in prices)
            book.rebalance({t: 1 / len(held) for t in held} if held else {}, prices, cost_rate)
            ew.rebalance({t: 1 / len(judged) for t in judged} if judged else {}, prices, cost_rate)
            r_ = store.last_rate("DGS3MO", q.signal_day)
            rf_now = (r_.value / 100 / 252) if r_ is not None else 0.0
            counts = {s: sum(1 for x in q.status.values() if x == s) for s in S.STATUSES}
            log.append({"signalDay": q.signal_day.isoformat(), "tradeDay": d.isoformat(), "screened": len(q.status),
                        "statuses": counts, "blocked": q.blocked, "held": held, "judged": len(judged), "turnover": book.turnover[-1]})  # fmt: skip
            started = True
        if started:
            curve.append((d, book.value()))
            ew_curve.append((d, ew.value()))
            rf.append(rf_now)

    # What each status went on to return over the next quarter, and the held book minus the judged book.
    fwd_by_status: dict[str, list[float]] = {s: [] for s in S.STATUSES}
    active: list[float] = []
    trade_days = [q.trade_day for q in quarters]
    for a, b in zip(trade_days, trade_days[1:], strict=False):
        q = by_trade[a]
        rets: dict[str, float] = {}
        for t in q.status:
            p0 = panel.get(t, {}).get(a)
            p1 = _price_on(panel.get(t, {}), pdays.get(t, []), b)
            if p0 and p1:
                rets[t] = p1 / p0 - 1
        groups: dict[str, list[float]] = {s: [] for s in S.STATUSES}
        for t, r in rets.items():
            groups[q.status[t]].append(r)
        for s, xs in groups.items():
            if xs:
                fwd_by_status[s].append(statistics.fmean(xs))
        held_r = [rets[t] for t, s in q.status.items() if s in cfg.hold and t in rets]
        judged_r = [rets[t] for t, s in q.status.items() if s != "blocked" and t in rets]
        if held_r and judged_r:
            active.append(statistics.fmean(held_r) - statistics.fmean(judged_r))

    research = gate.evaluate(curve, ew_curve, rf, cfg.as_dict(), strategy="B")
    spy = [(d, panel[benchmark][d]) for d, _ in curve if d in panel[benchmark]]
    warnings.append(
        "costs: costs-v1 slippage and commission on turnover; minimum commission, borrow and taxes ignored"
    )
    warnings.append("prices: adjusted closes; a held company with no price on a day keeps its last value")
    strat, ew_stats, spy_stats = stats(curve, rf), stats(ew_curve, rf), stats(spy, rf)
    extra = {b: stats([(d, panel[b][d]) for d, _ in curve if d in panel[b]], rf) for b in extra_benchmarks}
    return {
        "ran": True, "version": BACKTEST_VERSION, "model": cfg.version, "config": cfg.as_dict(),
        "costs": costs.as_dict(), "universe": len(tickers), "benchmark": benchmark,
        "strategy": strat, "equalWeight": ew_stats, "spy": spy_stats, "benchmarks": extra,
        "comparison": compare(strat, ew_stats, spy_stats),
        "averageHeld": statistics.fmean(len(x["held"]) for x in log) if log else None,
        "averageTurnover": statistics.fmean(book.turnover) if book.turnover else None,
        "nextQuarterByStatus": {s: {"quarters": len(v), "meanReturn": statistics.fmean(v) if v else None}
                                for s, v in fwd_by_status.items()},
        "activeVsJudged": {"quarters": len(active), "mean": statistics.fmean(active) if active else None,
                           "t": _t(active), "hitRate": sum(x > 0 for x in active) / len(active) if active else None},
        "research": research, "quarters": log,
        "curve": [(d.isoformat(), v) for d, v in curve],
        "equalWeightCurve": [(d.isoformat(), v) for d, v in ew_curve],
        "warnings": warnings,
    }  # fmt: skip
