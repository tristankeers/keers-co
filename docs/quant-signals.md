# Strategy C: systematic signals (first version)

Paper trading only. Strategy C's trades come from rules in code, never from a model's opinion on one company.

## What is built

| Part | Module | Version |
|---|---|---|
| Point-in-time view of the store | `keers.strategies.quant.pit` | |
| Signals: momentum 12-1, low volatility | `keers.strategies.quant.signals` | `signals-v1` |
| Ranking and target book | `keers.strategies.quant.model` | `quant-c-v1` |
| Monthly backtest | `keers.strategies.quant.backtest` | `backtest-v1` |
| Rebalance tickets | `keers.strategies.quant.rebalance` | |

**Signals.** Momentum 12-1 is the return from 252 to 21 sessions ago (the latest month is skipped because short-term
returns tend to reverse). Low volatility is the annualised standard deviation of the last 252 daily log returns; the
score is its negative. Both use Yahoo adjusted closes. A signal is unknown (and the company is not ranked) when the
history is short, has gaps (under 95% of business days), or the last price is more than 3 business days old.

**Book.** Each signal becomes a z-score across the companies where it is known (clipped at the 5th and 95th
percentiles), and the composite is their mean. Companies under $5 or with under $5m median daily traded value are
left out. The top 15 are held equally, 30% of NAV in total and at most 5% a name. Long only.

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
- No shorts, no value or quality signals yet (next steps: EBIT/EV and EBIT/assets from point-in-time filings,
  excluding financial companies), no UI panel yet.
- Yahoo data is personal use only; it must be replaced before any commercial use.
