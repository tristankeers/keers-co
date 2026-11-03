"""Is a backtest's result skill or luck? Statistics for the paper-trading gates (``research-stats-v1``).

Plain Python, no third-party packages. Written from the published methods, not from any other code base:

- Probabilistic Sharpe Ratio (PSR): Bailey and López de Prado, "The Sharpe Ratio Efficient Frontier",
  Journal of Risk 15(2), 2012. The probability that the true Sharpe ratio is above a threshold, given how many
  returns were observed and how skewed and fat-tailed they are.
- Deflated Sharpe Ratio (DSR): Bailey and López de Prado, "The Deflated Sharpe Ratio", Journal of Portfolio
  Management 40(5), 2014. PSR against the Sharpe ratio that the best of N tried models would show by luck alone.
  The more models were tried, the higher the bar.
- Probability of Backtest Overfitting (PBO) by combinatorially symmetric cross-validation (CSCV): Bailey,
  Borwein, López de Prado and Zhu, "The Probability of Backtest Overfitting", Journal of Computational Finance
  20(4), 2017. How often the model that looks best on one half of the history is below the median on the other.
- Stationary bootstrap: Politis and Romano, JASA 89(428), 1994. Resamples blocks of returns of random length so
  that runs of good or bad months stay together; gives a confidence interval for the Sharpe ratio.

All Sharpe ratios here are per period (per trading day) unless the name says annual. Kurtosis is the plain
(Pearson) kurtosis, 3 for a normal distribution.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

STATS_VERSION = "research-stats-v1"
PERIODS_PER_YEAR = 252
EULER_GAMMA = 0.5772156649015329
_N = statistics.NormalDist()


def sharpe(returns: Sequence[float]) -> float | None:
    """Mean over sample standard deviation, per period. None with fewer than 2 returns or no variation."""
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    return statistics.fmean(returns) / sd if sd > 0 else None


def moments(returns: Sequence[float]) -> tuple[float, float] | None:
    """(skewness, Pearson kurtosis) with population moments, as in the PSR paper."""
    n = len(returns)
    if n < 3:
        return None
    m = statistics.fmean(returns)
    m2 = sum((x - m) ** 2 for x in returns) / n
    if m2 <= 0:
        return None
    m3 = sum((x - m) ** 3 for x in returns) / n
    m4 = sum((x - m) ** 4 for x in returns) / n
    return m3 / m2**1.5, m4 / m2**2


def _sr_spread(sr: float, skew: float, kurt: float) -> float | None:
    v = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    return math.sqrt(v) if v > 0 else None


def psr(sr: float, n: int, skew: float, kurt: float, threshold: float = 0.0) -> float | None:
    """Probability that the true per-period Sharpe exceeds ``threshold``, from ``n`` observed returns."""
    s = _sr_spread(sr, skew, kurt)
    if s is None or n < 2:
        return None
    return _N.cdf((sr - threshold) * math.sqrt(n - 1) / s)


def expected_max_sharpe(n_trials: int, trial_variance: float) -> float:
    """The Sharpe ratio the best of ``n_trials`` skill-free models would show, given how much the trials' Sharpe
    ratios vary. Zero for a single trial (nothing was selected)."""
    if n_trials < 2 or trial_variance <= 0:
        return 0.0
    a = _N.inv_cdf(1 - 1 / n_trials)
    b = _N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(trial_variance) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def min_track_record(sr: float, skew: float, kurt: float, threshold: float = 0.0,
                     confidence: float = 0.95) -> float | None:  # fmt: skip
    """Returns needed before PSR(threshold) reaches ``confidence``. None when the Sharpe is not above it."""
    s = _sr_spread(sr, skew, kurt)
    if s is None or sr <= threshold:
        return None
    return 1 + (s * _N.inv_cdf(confidence) / (sr - threshold)) ** 2


@dataclass(frozen=True)
class SharpeTest:
    observations: int
    sharpe_annual: float | None
    skew: float | None
    kurtosis: float | None
    psr: float | None  # probability the true Sharpe is above zero
    trials: int
    trial_sharpe_sd_annual: float | None  # spread of the tried models' Sharpe ratios
    expected_max_sharpe_annual: float | None  # what luck alone gives the best of ``trials``
    dsr: float | None  # probability the true Sharpe is above that
    min_track_record_years: float | None  # years of returns before PSR reaches 95%
    reason: str = ""  # why a figure is missing

    def as_dict(self) -> dict[str, Any]:
        return {_camel(k): v for k, v in asdict(self).items()}


def _camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(w.title() for w in rest)


def sharpe_test(returns: Sequence[float], trial_sharpes_annual: Sequence[float], n_trials: int,
                periods: int = PERIODS_PER_YEAR) -> SharpeTest:  # fmt: skip
    """PSR and DSR of ``returns`` (excess over the risk-free rate). ``trial_sharpes_annual`` are the annual
    Sharpe ratios of the models tried so far, used for their spread; ``n_trials`` counts every model tried,
    including any whose Sharpe was not kept. The DSR needs at least two known trial Sharpe ratios when
    ``n_trials`` is above one."""
    root = math.sqrt(periods)
    n = len(returns)
    sr, mo = sharpe(returns), moments(returns)
    if sr is None or mo is None:
        return SharpeTest(n, None, None, None, None, n_trials, None, None, None, None,
                          "too few returns or no variation")  # fmt: skip
    skew, kurt = mo
    p = psr(sr, n, skew, kurt)
    mtrl = min_track_record(sr, skew, kurt)
    known = [s / root for s in trial_sharpes_annual]
    reason = ""
    if n_trials <= 1:
        sd, emax = 0.0, 0.0
    elif len(known) >= 2:
        sd = statistics.stdev(known)
        emax = expected_max_sharpe(n_trials, sd**2)
    else:
        sd, emax, reason = None, None, "fewer than two recorded trial Sharpe ratios, so the bar cannot be set"
    d = psr(sr, n, skew, kurt, emax) if emax is not None else None
    return SharpeTest(
        observations=n,
        sharpe_annual=sr * root,
        skew=skew,
        kurtosis=kurt,
        psr=p,
        trials=n_trials,
        trial_sharpe_sd_annual=None if sd is None else sd * root,
        expected_max_sharpe_annual=None if emax is None else emax * root,
        dsr=d,
        min_track_record_years=None if mtrl is None else mtrl / periods,
        reason=reason,
    )


def bootstrap_sharpe_ci(returns: Sequence[float], mean_block: int = 21, resamples: int = 1000,
                        level: float = 0.95, seed: int = 0,
                        periods: int = PERIODS_PER_YEAR) -> tuple[float, float] | None:  # fmt: skip
    """Stationary-bootstrap confidence interval for the annual Sharpe ratio. Blocks average ``mean_block``
    returns (21 trading days, about a month). Seeded, so the same returns always give the same interval."""
    n = len(returns)
    if n < 2 * mean_block or resamples < 20:
        return None
    rng, p = random.Random(seed), 1 / mean_block  # noqa: S311 (statistics, not security)
    xs = list(returns)
    out: list[float] = []
    for _ in range(resamples):
        i = rng.randrange(n)
        s = s2 = 0.0
        for _k in range(n):
            x = xs[i]
            s += x
            s2 += x * x
            i = rng.randrange(n) if rng.random() < p else (i + 1) % n
        var = (s2 - s * s / n) / (n - 1)
        if var > 0:
            out.append(s / n / math.sqrt(var) * math.sqrt(periods))
    if len(out) < resamples // 2:
        return None
    out.sort()
    lo = out[int((1 - level) / 2 * (len(out) - 1))]
    hi = out[int((1 + level) / 2 * (len(out) - 1))]
    return lo, hi


@dataclass(frozen=True)
class PBO:
    pbo: float  # share of splits where the in-sample best model is at or below the median out of sample
    splits: int
    blocks: int
    models: int
    observations: int
    mean_logit: float  # average log-odds of the chosen model's out-of-sample rank; below zero is bad
    selected: dict[str, int]  # how often each model was the in-sample best

    def as_dict(self) -> dict[str, Any]:
        return {_camel(k): v for k, v in asdict(self).items()}


def pbo_cscv(returns: dict[str, Sequence[float]], blocks: int = 16) -> PBO:
    """PBO across models whose returns cover the same dates (one list per model, aligned). The history is cut
    into ``blocks`` equal parts (oldest leftover returns dropped); every way of choosing half of them is one
    split: in-sample is that half, out-of-sample the rest. Model ranking uses the Sharpe ratio."""
    names = sorted(returns)
    if len(names) < 2:
        raise ValueError("PBO needs at least two models")
    if blocks < 2 or blocks % 2:
        raise ValueError("blocks must be an even number of at least 2")
    t = len(returns[names[0]])
    if any(len(returns[k]) != t for k in names):
        raise ValueError("every model needs returns on the same dates")
    size = t // blocks
    if size < 20:
        raise ValueError(f"{t} returns is too few for {blocks} blocks of at least 20")
    start = t - size * blocks
    # Per block and model: count, sum and sum of squares, so each split's Sharpe is cheap.
    agg = [[(sum(xs), sum(x * x for x in xs)) for xs in
            (returns[k][start + b * size:start + (b + 1) * size] for k in names)] for b in range(blocks)]  # fmt: skip

    def sr(chosen: Sequence[int]) -> list[float]:
        n = size * len(chosen)
        out = []
        for m in range(len(names)):
            s = sum(agg[b][m][0] for b in chosen)
            s2 = sum(agg[b][m][1] for b in chosen)
            var = (s2 - s * s / n) / (n - 1)
            out.append(s / n / math.sqrt(var) if var > 0 else 0.0)
        return out

    logits: list[float] = []
    picked = dict.fromkeys(names, 0)
    everything = range(blocks)
    for ins in combinations(everything, blocks // 2):
        oos = [b for b in everything if b not in ins]
        sr_in, sr_out = sr(ins), sr(oos)
        best = max(range(len(names)), key=lambda m: (sr_in[m], -m))
        picked[names[best]] += 1
        below = sum(1 for x in sr_out if x < sr_out[best])
        ties = sum(1 for x in sr_out if x == sr_out[best])  # includes itself
        rank = below + (ties + 1) / 2  # 1 = worst, N = best; ties share the average rank
        w = rank / (len(names) + 1)
        logits.append(math.log(w / (1 - w)))
    return PBO(
        pbo=sum(1 for x in logits if x <= 0) / len(logits),
        splits=len(logits),
        blocks=blocks,
        models=len(names),
        observations=size * blocks,
        mean_logit=statistics.fmean(logits),
        selected=picked,
    )
