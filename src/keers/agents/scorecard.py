"""Were the research agents right? Grades every saved research run against what the share price did next
(``scorecard-v1``).

Each run in ``agent-runs/`` is a call: the analyst's stance (long, short or no trade) and conviction, the bear
reviewer's verdict and the code's decision. A call is graded at its strategy's horizon, counted in trading
sessions after the call: ``HORIZON_SESSIONS``. Until then it is pending: counted and shown with its return so
far, never graded.

The return that counts is the excess over SPY across the same sessions (adjusted closes), so a long call in a rising
market does not score for free. Entry is the last close on or before the day of the run, the close the memo saw.

Measures, for each grader (the analyst by source, model or rules; the bear; the committee's decision):

- **Hit rate**: share of directional calls (long or short) whose excess return had the same sign.
- **Payoff**: the average excess return won on hits over the average lost on misses.
- **Average signed excess return**: the excess return times +1 for long, -1 for short.
- **IC**: rank correlation between the signed conviction (long low 1 ... high 3, short negative, no trade 0) and
  the excess return, with a t-stat. It is reported only from ``MIN_FOR_IC`` graded calls: below that it is noise.
- **Bear**: the average signed excess return of the analyst's directional calls the bear let through (proceed) and
  of those it objected to (concerns or block). A useful bear objects to the worse ones.

The idea of grading every analyst at its own horizon with hit rate and IC comes from the MIT-licensed
virattt/ai-hedge-fund project; this is our own code over our own run records. Arithmetic only, no model is asked.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCORECARD_VERSION = "scorecard-v1"
BENCHMARK = "SPY"
HORIZON_SESSIONS = {"A": 126, "B": 252, "C": 21}  # about six months, a year, a month
MIN_FOR_IC = 20
CONVICTION = {"low": 1, "medium": 2, "high": 3}
DIRECTION = {"long": 1, "short": -1, "no_trade": 0}

Prices = Callable[[str, date, date], Sequence[tuple[date, float | None]]]


def load_runs(directory: str | Path) -> list[dict[str, Any]]:
    """Every saved run record. A later run of the same company and strategy on the same day replaces earlier
    ones, so re-running a memo does not count twice."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    d = Path(directory)
    if not d.is_dir():
        return []
    for p in sorted(d.glob("*.json")):
        try:
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict) or not r.get("symbol") or not r.get("createdAt") or not r.get("analyst"):
            continue
        key = (r["symbol"], r.get("strategy", ""), r["createdAt"][:10])
        if key not in latest or r["createdAt"] >= latest[key]["createdAt"]:
            latest[key] = r
    return sorted(latest.values(), key=lambda r: (r["createdAt"], r["symbol"]))


def _series(prices: Prices, ticker: str, start: date, end: date) -> tuple[list[date], list[float]]:
    rows = [(d, p) for d, p in prices(ticker, start, end) if p]
    return [d for d, _ in rows], [p for _, p in rows]


def _grade_one(r: Mapping[str, Any], prices: Prices, today: date) -> dict[str, Any]:
    case = (r.get("analyst") or {}).get("data") or {}
    stance = case.get("stance", "no_trade")
    direction = DIRECTION.get(stance, 0)
    conviction = CONVICTION.get(case.get("conviction", ""), 0)
    strategy = r.get("strategy", "")
    horizon = HORIZON_SESSIONS.get(strategy, 252)
    called = datetime.fromisoformat(r["createdAt"]).date()
    bear = ((r.get("bear") or {}).get("data") or {}).get("verdict")
    decision = r.get("decision") or {}
    out: dict[str, Any] = {
        "id": r.get("id"), "symbol": r["symbol"], "strategy": strategy, "called": called.isoformat(),
        "stance": stance, "conviction": case.get("conviction"), "signedConviction": direction * conviction,
        "analystSource": (r.get("analyst") or {}).get("source", "unknown"), "bear": bear,
        "proposed": bool(decision.get("propose")), "horizonSessions": horizon,
    }  # fmt: skip
    lo = date.fromordinal(called.toordinal() - 10)
    days, px = _series(prices, r["symbol"], lo, today)
    bdays, bpx = _series(prices, BENCHMARK, lo, today)
    i = bisect.bisect_right(days, called) - 1
    j = bisect.bisect_right(bdays, called) - 1
    if i < 0 or j < 0 or days[i] != bdays[j]:
        return {
            **out,
            "status": "no price",
            "note": f"no close for {r['symbol']} and {BENCHMARK} on the call day",
        }
    entry = days[i]
    bset = set(bdays)
    later = [k for k in range(i + 1, len(days)) if days[k] in bset]
    graded = len(later) >= horizon
    k = later[horizon - 1] if graded else (later[-1] if later else i)
    exit_day = days[k]
    b0, b1 = bpx[j], bpx[bisect.bisect_right(bdays, exit_day) - 1]
    stock = px[k] / px[i] - 1
    bench = b1 / b0 - 1
    excess = stock - bench
    return {**out, "status": "graded" if graded else "pending", "entry": entry.isoformat(),
            "through": exit_day.isoformat(), "sessions": len([d for d in later if d <= k]),
            "stockReturn": stock, "benchmarkReturn": bench, "excessReturn": excess,
            "signedExcess": excess * direction if direction else None}  # fmt: skip


def _spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    def ranks(x: Sequence[float]) -> list[float]:
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

    if len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    sa, sb = statistics.pstdev(ra), statistics.pstdev(rb)
    if sa == 0 or sb == 0:
        return None
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True)) / (len(ra) * sa * sb)


def summarise(calls: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    calls = list(calls)
    graded = [c for c in calls if c.get("status") == "graded"]
    directional = [c for c in graded if c["signedExcess"] is not None]
    hits = [c["signedExcess"] for c in directional if c["signedExcess"] > 0]
    misses = [-c["signedExcess"] for c in directional if c["signedExcess"] <= 0]
    out: dict[str, Any] = {
        "calls": len(calls),
        "graded": len(graded),
        "pending": sum(1 for c in calls if c.get("status") == "pending"),
        "noPrice": sum(1 for c in calls if c.get("status") == "no price"),
        "directional": len(directional),
        "hitRate": len(hits) / len(directional) if directional else None,
        "payoff": (statistics.fmean(hits) / statistics.fmean(misses)) if hits and misses and statistics.fmean(
            misses) > 0 else None,
        "meanSignedExcess": statistics.fmean(c["signedExcess"] for c in directional) if directional else None,
        "ic": None,
        "icT": None,
    }  # fmt: skip
    if len(graded) >= MIN_FOR_IC:
        ic = _spearman([c["signedConviction"] for c in graded], [c["excessReturn"] for c in graded])
        out["ic"] = ic
        if ic is not None and abs(ic) < 1:
            out["icT"] = ic * math.sqrt((len(graded) - 2) / (1 - ic * ic))
    else:
        out["icNote"] = f"needs at least {MIN_FOR_IC} graded calls"
    return out


def scorecard(runs: Sequence[Mapping[str, Any]], prices: Prices, today: date) -> dict[str, Any]:
    calls = [_grade_one(r, prices, today) for r in runs]
    by_source: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        by_source.setdefault(c["analystSource"], []).append(c)
    directional = [c for c in calls if c.get("status") == "graded" and c["signedExcess"] is not None]
    let_through = [c["signedExcess"] for c in directional if c["bear"] == "proceed"]
    objected = [c["signedExcess"] for c in directional if c["bear"] in ("concerns", "block")]
    proposed = [c for c in calls if c["proposed"]]
    return {
        "version": SCORECARD_VERSION,
        "asOf": today.isoformat(),
        "benchmark": BENCHMARK,
        "horizonSessions": HORIZON_SESSIONS,
        "analyst": summarise(calls),
        "analystBySource": {k: summarise(v) for k, v in sorted(by_source.items())},
        "committeeProposals": summarise(proposed),
        "bear": {
            "letThrough": len(let_through),
            "objected": len(objected),
            "meanSignedExcessLetThrough": statistics.fmean(let_through) if let_through else None,
            "meanSignedExcessObjected": statistics.fmean(objected) if objected else None,
        },
        "calls": calls,
        "notes": [
            "Excess return is over SPY across the same sessions; entry is the close the memo saw.",
            "A call is graded only once its strategy's horizon has passed; until then it is pending.",
            f"IC is shown from {MIN_FOR_IC} graded calls. A few calls say almost nothing about skill.",
        ],
    }
