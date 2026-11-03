"""Command line: ``python -m keers.research pbo|holdout``.

``pbo`` takes two or more saved backtests (``backtest --out``) of different configurations over the same dates
and gives the probability of backtest overfitting across them. ``holdout`` gives one backtest's result on the
holdout period, once: it refuses a configuration whose holdout was already recorded unless ``--again`` is given,
and ``--record`` writes the look into the trial register for the reviewed change that follows.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import stats
from .trials import Register

MIN_HOLDOUT_DAYS = 252


def _load(path: str) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(Path(path).read_text())
    if not d.get("ran") or "research" not in d:
        raise SystemExit(
            f"{path}: not a finished backtest with research statistics (rerun with this version)"
        )
    return d


def excess_returns(bt: dict[str, Any], since: date | None = None,
                   until: date | None = None) -> dict[date, float]:  # fmt: skip
    """Daily return over the risk-free rate, keyed by day, for days in [since, until)."""
    curve = [(date.fromisoformat(d), v) for d, v in bt["curve"]]
    rf = bt.get("riskFreeDaily") or []
    out = {}
    for i in range(1, len(curve)):
        d = curve[i][0]
        if (since and d < since) or (until and d >= until):
            continue
        out[d] = curve[i][1] / curve[i - 1][1] - 1 - (rf[i] if i < len(rf) else 0.0)
    return out


def cmd_pbo(paths: list[str], blocks: int, strategy: str, register: str | None) -> dict[str, Any]:
    reg = Register.load(register)
    holdout = reg.holdout_start(strategy)
    runs = {}
    for p in paths:
        bt = _load(p)
        name = f"{bt['model']}:{bt['research']['configHash']}"
        if name in runs:
            raise SystemExit(f"{p}: the same configuration ({name}) is given twice")
        runs[name] = excess_returns(bt, until=holdout)
    days = sorted(set.intersection(*(set(r) for r in runs.values())))
    res = stats.pbo_cscv({k: [r[d] for d in days] for k, r in runs.items()}, blocks=blocks)
    return {"version": stats.STATS_VERSION, "from": days[0].isoformat(), "to": days[-1].isoformat(),
            **res.as_dict(), "holdoutStart": holdout.isoformat(),
            "read": "PBO is the share of splits where the model that was best on half of the history ranked at or "
                    "below the median on the other half. Under 0.5 is the minimum; with only two or three "
                    "models it is a coarse figure."}  # fmt: skip


def cmd_holdout(path: str, strategy: str, register: str | None, record: bool, again: bool) -> dict[str, Any]:
    reg = Register.load(register)
    bt = _load(path)
    h = bt["research"]["configHash"]
    start = reg.holdout_start(strategy)
    earlier = reg.holdout_runs(strategy, h)
    if earlier and not again:
        raise SystemExit(f"the holdout of {h} was already looked at on {earlier[-1].get('recorded')}; "
                         "--again looks once more and is recorded as a second look")  # fmt: skip
    xs = list(excess_returns(bt, since=start).values())
    curve = [(date.fromisoformat(d), v) for d, v in bt["curve"]]
    inside = [v for d, v in curve if d >= start]
    before = [v for d, v in curve if d < start]
    total = inside[-1] / before[-1] - 1 if inside and before else None
    sr, mo = stats.sharpe(xs), stats.moments(xs)
    p = stats.psr(sr, len(xs), *mo) if sr is not None and mo is not None else None
    out = {
        "configHash": h,
        "model": bt["model"],
        "holdoutStart": start.isoformat(),
        "days": len(xs),
        "totalReturn": total,
        "sharpeAnnual": None if sr is None else sr * stats.PERIODS_PER_YEAR**0.5,
        "psr": p,
        "look": len(earlier) + 1,
        "warnings": (
            [f"only {len(xs)} holdout days; at least {MIN_HOLDOUT_DAYS} are needed for a fair read"]
            if len(xs) < MIN_HOLDOUT_DAYS
            else []
        ),  # fmt: skip
    }
    if record:
        reg.record_holdout(
            strategy, {**out, "recorded": datetime.now(UTC).date().isoformat(), "source": path}
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m keers.research")
    ap.add_argument("--register", help="trial register (default config/research/trials.json)")
    ap.add_argument("--strategy", default="C")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pbo")
    p.add_argument("backtests", nargs="+")
    p.add_argument("--blocks", type=int, default=16)
    h = sub.add_parser("holdout")
    h.add_argument("backtest")
    h.add_argument("--record", action="store_true", help="write this look into the trial register")
    h.add_argument("--again", action="store_true", help="look again at a holdout already looked at")
    a = ap.parse_args(argv)
    if a.cmd == "pbo":
        if len(a.backtests) < 2:
            ap.error("pbo needs at least two backtests")
        out = cmd_pbo(a.backtests, a.blocks, a.strategy, a.register)
    else:
        out = cmd_holdout(a.backtest, a.strategy, a.register, a.record, a.again)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
