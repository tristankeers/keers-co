# Strategy C: systematic signals

Paper trading only. Strategy C's trades come from rules in code, never from a model's opinion on one company.

## What is built

| Part | Module | Version |
|---|---|---|
| Point-in-time view of the store | `keers.strategies.quant.pit` | |
| Signals: momentum 12-1, low volatility | `keers.strategies.quant.signals` | `signals-v2` |
| Signals: value, quality (from filed accounts) | `keers.strategies.quant.fundamental` | `signals-v2` |
| Ranking and target book | `keers.strategies.quant.model` | `quant-c-v2` (v1 kept as a preset) |
| Monthly backtest | `keers.strategies.quant.backtest` | `backtest-v1` |
| Rebalance tickets | `keers.strategies.quant.rebalance` | |

**Signals.** Momentum 12-1 is the return from 252 to 21 sessions ago (the latest month is skipped because short-term
returns tend to reverse). Low volatility is the annualised standard deviation of the last 252 daily log returns; the
score is its negative. Both use Yahoo adjusted closes. A signal is unknown (and the company is not ranked) when the
history is short, has gaps (under 95% of business days), or the last price is more than 3 business days old.

**Value and quality.** Value is the earnings yield, operating income (EBIT, trailing twelve months) / enterprise
value. Quality is return on assets, the same EBIT / total assets at the end of that period. Both come from
`keers.data.snapshot` built as of the signal date, so only filings made by then count, and the valuation's debt and
period checks apply. They are unknown for banks, insurers and other financials (SIC 6000-6799), when enterprise
value is not positive, or when the accounts are older than the company's reporting cadence allows.

**Book (`quant-c-v2`).** Momentum, value and quality each become a z-score across the companies where they are known
(clipped at the 5th and 95th percentiles), and the composite is the mean of the known ones; a company needs at least
two of the three. Companies under $5 or with under $5m median daily traded value are left out. The top 15 are held,
30% of NAV in total and at most 5% a name, long only. Low volatility no longer votes on what to buy; it sizes the
book instead, with weights proportional to 1 / volatility.

**Why v2.** The first live backtest (v1: momentum plus low volatility, equal weights, Feb 2017 to Oct 2026) made
13.6% a year against 15.1% for SPY and 20.5% for an equal-weight book of the same 100 names. Low volatility was the
problem: the calmest fifth trailed the most volatile fifth by 2.6% a month. v2 was designed from the published
factor research before it was run, not tuned on this backtest; `--preset v1` still runs the old model for
comparison.

## Paper-trading gate

`model.APPROVED_FOR_PAPER` is `False`. While it is, the rebalance endpoint previews tickets but refuses to execute
them. It should only be set to `True` in a reviewed change, after a backtest of the current model beats both SPY and
the equal-weight book on CAGR and Sharpe (the backtest reports this as `verdict`).

## No look-ahead

Signals read the store only through `PointInTime(store, as_of)`, which cuts every query off at `as_of` and raises
`LookAheadError` if anything later comes back. A test fills the store with absurd prices after the as-of date and
checks the signals do not change. In the backtest the signal is taken at a month-end close and the trade happens at
the next session's close.

## Running it

```
python3 -m keers.strategies.quant fetch --universe config/universe/kci100.txt --years 10   # SEC + 10y of prices
python3 -m keers.strategies.quant signals --universe config/universe/kci100.txt
python3 -m keers.strategies.quant backtest --universe config/universe/kci100.txt --start 2017-01-01 --out bt.json
```

`fetch` needs `SEC_USER_AGENT`. The GitHub workflow **Quant backtest** runs the same three steps live and uploads the
result.

Paper trades go through the workbench: `POST /api/strategies/c/signals` shows the ranking, and
`POST /api/portfolios/{id}/strategies/c/rebalance` with `{"execute": true}` sends each ticket through
`Workbench.paper_trade`, so the paper desk prices it and `keers.risk` approves or blocks it. Sells go first. Without
`execute` every ticket is previewed on its own (previews are not chained).

## Known limits

- Survivorship bias: the universe is today's ticker list, so companies that were delisted are missing. This flatters
  backtest results; every run says so.
- Backtest costs are costs-v1 slippage plus commission on turnover; the minimum commission, borrow and tax are ignored.
- No shorts and no UI panel yet. Fundamentals are SEC filers only in the backtest universe.
- Yahoo data is personal use only; it must be replaced before any commercial use.
