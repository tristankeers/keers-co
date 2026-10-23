"""Monthly backtest of the strategy C book, with no look-ahead (``backtest-v1``).

Timing: on the last session of each month the signals are computed from a ``PointInTime`` view at that day's
close. The book trades at the NEXT session's close, so a signal never earns the return of the day it was seen.
Between rebalances the holdings drift with their prices. Turnover pays the paper desk's ``costs-v1`` slippage
plus commission (the minimum commission is ignored). The book is shown as a stand-alone portfolio fully
invested in its names (the sleeve weights scaled to 100%).

Comparisons: SPY held throughout, and an equal-weight book of every company that could be ranked that month
(same timing and costs), which isolates what the ranking adds. Per signal: the monthly return of the top fifth
minus the bottom fifth, and the rank correlation between the signal and the next month's return (the IC).

Known biases, reported with every run: the universe is today's list of tickers, so companies that were delisted
are missing (survivorship bias, which flatters results); prices are Yahoo's adjusted closes.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from keers.data.store import Store
from keers.portfolio.fills import BPS, CostModel

from .model import SIGNALS, Config, Row, rank
from .pit import PointInTime
from .signals import SIGNALS_VERSION

BACKTEST_VERSION = "backtest-v1"
BENCHMARK = "SPY"
WARMUP_DAYS = 420  # history the first signal needs before ``start``


def month_ends(sessions: Sequence[date]) -> list[date]:
    out: list[date] = []
    for a, b in zip(sessions, sessions[1:], strict=False):
        if (a.year, a.month) != (b.year, b.month):
            out.append(a)
    return out


def _price_on(series: dict[date, float], days: list[date], d: date) -> float | None:
    """Last known price on or before ``d``."""
    import bisect

    i = bisect.bisect_right(days, d) - 1
    return series[days[i]] if i >= 0 else None


class _Book:
    """Holdings in value terms; starts at 1.0."""

    def __init__(self) -> None:
        self.h: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.cash = 1.0
        self.turnover: list[float] = []

    def value(self) -> float:
        return self.cash + sum(self.h.values())

    def mark(self, prices: dict[str, float]) -> None:
        for t in self.h:
            p = prices.get(t)
            if p is not None and t in self.last:
                self.h[t] *= p / self.last[t]
                self.last[t] = p

    def rebalance(self, w: dict[str, float], prices: dict[str, float], cost_rate: float) -> None:
        v = self.value()
        w = {t: x for t, x in w.items() if prices.get(t) is not None}
        new = {t: v * x for t, x in w.items()}
        traded = sum(abs(new.get(t, 0.0) - self.h.get(t, 0.0)) for t in set(new) | set(self.h))
        self.turnover.append(traded / v if v else 0.0)
        cost = traded * cost_rate
        self.h = {t: x * (1 - cost / v) for t, x in new.items()} if v else {}
        self.cash = (v - cost) - sum(self.h.values())
        self.last = {t: prices[t] for t in self.h}


def stats(values: Sequence[tuple[date, float]], rf_daily: Sequence[float]) -> dict[str, Any]:
    if len(values) < 2:
        return {"days": len(values)}
    rets = [b[1] / a[1] - 1 for a, b in zip(values, values[1:], strict=False)]
    years = (values[-1][0] - values[0][0]).days / 365.25
    total = values[-1][1] / values[0][1] - 1
    vol = statistics.stdev(rets) * math.sqrt(252) if len(rets) > 1 else None
    ex = [r - f for r, f in zip(rets, rf_daily, strict=False)]
    sharpe = (
        (statistics.fmean(ex) / statistics.stdev(ex) * math.sqrt(252))
        if len(ex) > 1 and statistics.stdev(ex) > 0
        else None
    )
    peak, mdd = values[0][1], 0.0
    for _, v in values:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return {"start": values[0][0].isoformat(), "end": values[-1][0].isoformat(), "totalReturn": total,
            "cagr": (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else None, "volatility": vol,
            "sharpe": sharpe, "maxDrawdown": mdd}  # fmt: skip


def verdict(strat: dict[str, Any], ew: dict[str, Any], spy: dict[str, Any]) -> dict[str, Any]:
    """Whether the book beat each benchmark on both growth (CAGR) and risk-adjusted return (Sharpe)."""

    def beats(b: dict[str, Any]) -> bool:
        keys = ("cagr", "sharpe")
        return all(strat.get(k) is not None and b.get(k) is not None and strat[k] > b[k] for k in keys)

    out = {"beatsSpy": beats(spy), "beatsEqualWeight": beats(ew)}
    out["passes"] = out["beatsSpy"] and out["beatsEqualWeight"]
    return out


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 5:
        return None

    def ranks(x: list[float]) -> list[float]:
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    sa, sb = statistics.pstdev(ra), statistics.pstdev(rb)
    if sa == 0 or sb == 0:
        return None
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True)) / (len(ra) * sa * sb)


def _t(xs: list[float]) -> float | None:
    if len(xs) < 3 or statistics.stdev(xs) == 0:
        return None
    return statistics.fmean(xs) / (statistics.stdev(xs) / math.sqrt(len(xs)))


def run(store: Store, tickers: Sequence[str], start: date, end: date, cfg: Config | None = None,
        costs: CostModel | None = None, benchmark: str = BENCHMARK) -> dict[str, Any]:  # fmt: skip
    cfg, costs = cfg or Config(), costs or CostModel()
    cost_rate = (costs.slippage_bps + costs.commission_bps) * BPS
    tickers = sorted({t.upper() for t in tickers} - {benchmark})
    bench = [(d, a) for d, a in store.adjusted_closes(benchmark, start - timedelta(days=10), end) if a]
    sessions = [d for d, _ in bench if d >= start]
    rebal = month_ends(sessions)
    warnings = ["survivorship bias: the universe is today's ticker list, so delisted companies are missing"]
    if len(rebal) < 2:
        return {"ran": False, "reason": f"fewer than two month ends between {start} and {end} in {benchmark} prices",
                "warnings": warnings}  # fmt: skip
    # The P&L panel. Signals never see it: they read only through PointInTime.
    panel: dict[str, dict[date, float]] = {
        t: {d: a for d, a in store.adjusted_closes(t, start, end) if a} for t in [*tickers, benchmark]
    }
    pdays = {t: sorted(s) for t, s in panel.items()}
    nxt = {d: sessions[i + 1] for i, d in enumerate(sessions[:-1])}
    execs = [(t, nxt[t]) for t in rebal if t in nxt]

    book, ew = _Book(), _Book()
    months: list[dict[str, Any]] = []
    spreads: dict[str, list[float]] = {n: [] for n in SIGNALS}
    ics: dict[str, list[float]] = {n: [] for n in SIGNALS}
    exec_set = {e: s for s, e in execs}
    plan: dict[date, list[Row]] = {}
    for sig_day, ex_day in execs:
        plan[ex_day] = rank(PointInTime(store, sig_day), tickers, cfg)

    curve: list[tuple[date, float]] = []
    ew_curve: list[tuple[date, float]] = []
    rf: list[float] = []
    rf_now = 0.0
    started = False
    missing_after: set[str] = set()
    for d in sessions:
        prices = {t: panel[t][d] for t in panel if d in panel[t]}
        if started:
            book.mark(prices)
            ew.mark(prices)
            missing_after |= {t for t in book.h if d not in panel[t]}
        if d in exec_set:
            rows = plan[d]
            w = {r.ticker: r.weight for r in rows if r.weight > 0}
            tot = sum(w.values())
            w = {t: x / tot for t, x in w.items()} if tot else {}
            ranked = [r.ticker for r in rows if r.composite is not None]
            book.rebalance(w, prices, cost_rate)
            ew.rebalance({t: 1 / len(ranked) for t in ranked} if ranked else {}, prices, cost_rate)
            r_ = store.last_rate("DGS3MO", exec_set[d])
            rf_now = (r_.value / 100 / 252) if r_ is not None else 0.0
            months.append({"signalDay": exec_set[d].isoformat(), "tradeDay": d.isoformat(), "ranked": len(ranked),
                           "held": sorted(w), "turnover": book.turnover[-1]})  # fmt: skip
            started = True
        if started:
            curve.append((d, book.value()))
            ew_curve.append((d, ew.value()))
            rf.append(rf_now)

    # Per-signal research: top fifth minus bottom fifth over each holding month, and the rank IC.
    ex_days = [e for _, e in execs]
    for a, b in zip(ex_days, ex_days[1:], strict=False):
        rows = plan[a]
        fwd: dict[str, float] = {}
        for r in rows:
            p0 = panel[r.ticker].get(a)
            p1 = _price_on(panel[r.ticker], pdays[r.ticker], b)
            if p0 and p1 and not r.liquidity:
                fwd[r.ticker] = p1 / p0 - 1
        for n in SIGNALS:
            pairs = sorted(((s.score, fwd[r.ticker]) for r in rows if (s := r.signals[n]).score is not None
                            and r.ticker in fwd), key=lambda x: x[0])  # fmt: skip
            if len(pairs) >= 10:
                k = len(pairs) // 5
                spreads[n].append(
                    statistics.fmean(x[1] for x in pairs[-k:]) - statistics.fmean(x[1] for x in pairs[:k])
                )
            ic = _spearman([x[0] for x in pairs], [x[1] for x in pairs])
            if ic is not None:
                ics[n].append(ic)

    spy = [(d, panel[benchmark][d]) for d, _ in curve if d in panel[benchmark]]
    if missing_after:
        warnings.append(
            f"held names with missing prices on some days (value carried): {sorted(missing_after)}"
        )
    warnings.append(
        "costs: costs-v1 slippage and commission on turnover; minimum commission, borrow and taxes ignored"
    )
    strat, ew_stats, spy_stats = stats(curve, rf), stats(ew_curve, rf), stats(spy, rf)
    return {
        "ran": True,
        "version": BACKTEST_VERSION,
        "model": cfg.version,
        "signalsVersion": SIGNALS_VERSION,
        "config": cfg.as_dict(),
        "costs": costs.as_dict(),
        "universe": len(tickers),
        "benchmark": benchmark,
        "strategy": stats(curve, rf),
        "equalWeight": stats(ew_curve, rf),
        "spy": stats(spy, rf),
        "averageTurnover": statistics.fmean(book.turnover) if book.turnover else None,
        "signals": {
            n: {
                "months": len(spreads[n]),
                "meanSpread": statistics.fmean(spreads[n]) if spreads[n] else None,
                "spreadT": _t(spreads[n]),
                "hitRate": (sum(x > 0 for x in spreads[n]) / len(spreads[n])) if spreads[n] else None,
                "meanIC": statistics.fmean(ics[n]) if ics[n] else None,
                "icT": _t(ics[n]),
            }
            for n in SIGNALS
        },  # fmt: skip
        "verdict": verdict(strat, ew_stats, spy_stats),
        "months": months,
        "curve": [(d.isoformat(), v) for d, v in curve],
        "warnings": warnings,
    }
