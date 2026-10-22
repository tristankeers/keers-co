"""Equity beta measured from stored prices. Deterministic; no fallback value.

Method: weekly returns from split- and dividend-adjusted closes (the last close of each ISO week), over the two
years to the as-of date, regressed on the benchmark's weekly returns over the same weeks. The raw slope is
then Blume-adjusted (0.67 × raw + 0.33), the usual correction for raw betas drifting towards 1 over time.

The beta is missing, never guessed, when either series lacks adjusted closes, the benchmark is stale, or there
are fewer than ``MIN_WEEKS`` paired weekly returns (a recent listing, say).
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import Kind, Value, missing
from .store import Store

BENCHMARK = "SPY"  # S&P 500 ETF, a total-return proxy for the US market
LOOKBACK_DAYS = 730
MIN_WEEKS = 78  # 18 months of the 24
MAX_BENCHMARK_GAP_DAYS = 7
BLUME_WEIGHT = 0.67


def weekly_closes(daily: list[tuple[date, float | None]]) -> dict[tuple[int, int], float] | None:
    """Last adjusted close of each ISO week. Days without one are skipped; None if no day has one."""
    out: dict[tuple[int, int], float] = {}
    for day, adj in daily:
        if adj is not None:
            iso = day.isocalendar()
            out[(iso.year, iso.week)] = adj
    return out or None


def regress(y: list[float], x: list[float]) -> tuple[float, float]:
    """Ordinary least squares slope of y on x, and R²."""
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    slope = sxy / sxx
    r2 = sxy * sxy / (sxx * syy) if syy > 0 else 0.0
    return slope, r2


def beta(store: Store, ticker: str, as_of: date, benchmark: str = BENCHMARK) -> Value:
    start = as_of - timedelta(days=LOOKBACK_DAYS)
    stock_daily = store.adjusted_closes(ticker, start, as_of)
    bench_daily = store.adjusted_closes(benchmark, start, as_of)
    if not bench_daily:
        return missing("x", f"beta: no {benchmark} prices in the store")
    if (as_of - bench_daily[-1][0]).days > MAX_BENCHMARK_GAP_DAYS:
        return missing("x", f"beta: last {benchmark} price {bench_daily[-1][0]} is stale")
    s, b = weekly_closes(stock_daily), weekly_closes(bench_daily)
    if s is None or b is None:
        return missing("x", "beta: prices lack adjusted closes (needs Tiingo)")
    # Only weeks where both traded, and only returns between consecutive weeks.
    weeks = sorted(set(s) & set(b))
    pairs = [(a, c) for a, c in zip(weeks, weeks[1:], strict=False) if _next_week(a) == c]
    if len(pairs) < MIN_WEEKS:
        return missing("x", f"beta: {len(pairs)} weekly returns, needs {MIN_WEEKS}")
    ys = [s[c] / s[a] - 1 for a, c in pairs]
    xs = [b[c] / b[a] - 1 for a, c in pairs]
    raw, r2 = regress(ys, xs)
    adjusted = BLUME_WEIGHT * raw + (1 - BLUME_WEIGHT)
    method = (
        f"weekly returns vs {benchmark}, {len(pairs)} weeks to {as_of}: raw {raw:.3f}, R² {r2:.2f}, "
        f"Blume-adjusted {BLUME_WEIGHT} × raw + {1 - BLUME_WEIGHT:.2f}"
    )
    last = min(stock_daily[-1][0], bench_daily[-1][0])
    src = {"source": "prices", "ticker": ticker.upper(), "benchmark": benchmark, "weeks": len(pairs),
           "raw": raw, "r2": r2}  # fmt: skip
    return Value(adjusted, Kind.DERIVED, "x", method, last, (src,))


def _next_week(w: tuple[int, int]) -> tuple[int, int]:
    monday = date.fromisocalendar(w[0], w[1], 1) + timedelta(days=7)
    iso = monday.isocalendar()
    return (iso.year, iso.week)
