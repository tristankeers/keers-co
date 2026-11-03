# Research protocol: is a backtest result skill or luck?

Written 3 November 2026, before any run of the statistics below. It applies to every strategy whose trades come
from rules in code (today, strategy C).

## Why

Each new model version we try and keep the best of makes a good backtest more likely by luck alone. Strategy C has
been tried twice (quant-c-v1 and v2) on the same 100 names and the same ten years. Beating SPY and the equal-weight
book is necessary but does not say whether the lead would survive. These statistics put a number on that.

## What is measured (`keers.research.stats`, `research-stats-v1`)

| Figure | Question it answers | Source |
|---|---|---|
| Probabilistic Sharpe Ratio (PSR) | How likely is the true Sharpe above zero, given the number of days and how skewed and fat-tailed the returns are? | Bailey and López de Prado, 2012 |
| Deflated Sharpe Ratio (DSR) | How likely is the true Sharpe above what the best of all the models we tried would show by luck? | Bailey and López de Prado, 2014 |
| Minimum track record | How many years of returns would PSR need to reach 95%? | Bailey and López de Prado, 2012 |
| Sharpe 95% interval | A stationary-bootstrap range for the annual Sharpe (blocks average 21 days, 1,000 resamples, fixed seed) | Politis and Romano, 1994 |
| Information ratio against equal weight, and its PSR | Does the ranking add anything beyond holding the whole list? | |
| Probability of Backtest Overfitting (PBO) | How often does the model that looks best on half the history fall to the median or below on the other half? | Bailey, Borwein, López de Prado and Zhu, 2017 |

All of it is plain Python written from the papers. The DSR test reproduces the 2014 paper's worked example
(0.9004). No code was taken from the reference projects we reviewed. The idea of putting these checks in the gate
came from the MIT-licensed Aurum project (zero-was-here/tradingbot).

## The trial register (`config/research/trials.json`)

Every model configuration that is backtested is recorded with its configuration hash, universe, period, annual
Sharpe and the run it came from. Trials are never deleted, including models that were dropped, because the DSR
needs the full count. A backtest of a configuration that is not registered counts as one more trial, and its
output says so. Add it to the register in the same reviewed change.

## The holdout

Every date before 3 November 2026 has already been seen while designing quant-c-v1 and v2, so there is no clean
stretch of the past left. The holdout is therefore the period from 3 November 2026 on. Backtest statistics leave
it out. Once at least a year of it exists, `python -m keers.research holdout backtest.json --record` reports a
model's holdout result and records the look in the register. A second look is refused unless `--again` is given,
and is recorded as a second look.

## The gate (`keers.strategies.quant.gate`, `gate-v2`)

A strategy C backtest passes only if all of these hold:

1. The book beats SPY on both CAGR and Sharpe.
2. The book beats the equal-weight book of the same ranked names on both CAGR and Sharpe.
3. The DSR is at least 0.95, counting every trial in the register. A missing DSR fails.

The PBO across registered versions is reported by the Quant backtest workflow (`python -m keers.research pbo`). It
is not part of the pass/fail rule yet, because two versions give only a coarse figure. A PBO above 0.5 should block
approval in review.

Passing the gate does not turn on paper trading. That takes the person's approval of that exact configuration,
recorded in the trial register (`approvedForPaper`, by configuration hash) in a reviewed change.
