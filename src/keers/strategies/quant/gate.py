"""Strategy C's paper-trading gate, with the overfitting statistics (``gate-v2``).

A backtest passes only if the book beats SPY and the equal-weight book on both CAGR and Sharpe (``gate-v1``) AND
its Deflated Sharpe Ratio is at least ``MIN_DSR``: the probability that its true Sharpe is above what the best of
every model tried so far would show by luck. The trial count comes from the register in
``config/research/trials.json``. Days from the register's holdout start on are left out of these statistics.

Also reported, not required: the probabilistic Sharpe ratio of the book against zero, a bootstrap confidence
interval for its Sharpe, and the probability that the book's true information ratio against the equal-weight
book is above zero (the ranking adds something beyond holding the whole list).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from keers.research import stats
from keers.research.trials import Register, config_hash

GATE_VERSION = "gate-v2"
MIN_DSR = 0.95
STRATEGY = "C"


def _returns(curve: Sequence[tuple[date, float]], until: date) -> list[tuple[date, float, int]]:
    """(day, simple return, index in the curve) for each day before ``until``."""
    out = []
    for i in range(1, len(curve)):
        d = curve[i][0]
        if d >= until:
            break
        out.append((d, curve[i][1] / curve[i - 1][1] - 1, i))
    return out


def evaluate(curve: Sequence[tuple[date, float]], ew_curve: Sequence[tuple[date, float]],
             rf_daily: Sequence[float], config: dict[str, Any],
             register: Register | None = None) -> dict[str, Any]:  # fmt: skip
    reg = register or Register.load()
    summary = reg.summary(STRATEGY, config)
    holdout = date.fromisoformat(summary["holdoutStart"])
    book = _returns(curve, holdout)
    excess = [r - (rf_daily[i] if i < len(rf_daily) else 0.0) for _, r, i in book]
    trial_sharpes = list(summary["recordedSharpes"])
    if not summary["registered"]:
        own = stats.sharpe(excess)
        if own is not None:
            trial_sharpes.append(own * stats.PERIODS_PER_YEAR**0.5)
    test = stats.sharpe_test(excess, trial_sharpes, summary["trials"])
    ci = stats.bootstrap_sharpe_ci(excess)
    ew = dict((d, r) for d, r, _ in _returns(ew_curve, holdout))
    active = [r - ew[d] for d, r, _ in book if d in ew]
    sr_a, mo_a = stats.sharpe(active), stats.moments(active)
    psr_active = stats.psr(sr_a, len(active), *mo_a) if sr_a is not None and mo_a is not None else None
    warnings = []
    if not summary["registered"]:
        warnings.append(
            f"this configuration ({summary['configHash']}) is not in the trial register, so it was counted as "
            "one more trial; add it to config/research/trials.json with its run"
        )
    if test.reason:
        warnings.append(test.reason)
    return {
        "version": stats.STATS_VERSION,
        "gate": GATE_VERSION,
        "configHash": summary["configHash"],
        "registered": summary["registered"],
        "holdoutStart": summary["holdoutStart"],
        "holdoutDaysExcluded": sum(1 for d, _ in curve[1:] if d >= holdout),
        "sharpeTest": test.as_dict(),
        "sharpeCI95Annual": list(ci) if ci else None,
        "bootstrap": {"method": "stationary", "meanBlockDays": 21, "resamples": 1000, "seed": 0},
        "activeVsEqualWeight": {
            "observations": len(active),
            "informationRatioAnnual": None if sr_a is None else sr_a * stats.PERIODS_PER_YEAR**0.5,
            "psr": psr_active,
        },
        "minDsr": MIN_DSR,
        "warnings": warnings,
    }


def verdict(base: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    """``gate-v1``'s comparisons plus the Deflated Sharpe requirement. A missing DSR fails."""
    dsr = research["sharpeTest"]["dsr"]
    out = {**base, "gate": GATE_VERSION, "deflatedSharpe": dsr,
           "dsrPasses": dsr is not None and dsr >= MIN_DSR}  # fmt: skip
    out["passes"] = bool(base.get("passes")) and out["dsrPasses"]
    return out


def paper_approval(config: dict[str, Any], register: Register | None = None) -> dict[str, Any] | None:
    """The person's recorded approval for paper trading THIS configuration, or None.

    The approval lives in ``config/research/trials.json`` (``approvedForPaper``) and names the configuration by
    hash, with who approved it, when, why and the run that passed the gate. Changing any setting changes the hash,
    so an approval never carries over to a model nobody reviewed. It is added only in a reviewed change.
    """
    reg = register or Register.load()
    entry = reg.strategy(STRATEGY).get("approvedForPaper")
    if not entry or entry.get("configHash") != config_hash(config):
        return None
    out: dict[str, Any] = entry
    return out
