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
| Paper-trading gate and overfitting statistics | `keers.strategies.quant.gate`, `keers.research` | `gate-v2`, `research-stats-v1` |

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

## Results so far (live run 39923030866, 23 Oct 2026, KCI 100 list, Feb 2017 to Oct 2026, after costs)

| | CAGR | Sharpe | Max drawdown |
|---|---|---|---|
| v2 book | 21.5% | 0.84 | 34% |
| v1 book | 13.6% | 0.57 | 33% |
| SPY | 15.1% | 0.72 | 34% |
| Equal-weight book (v2's ranked names) | 23.4% | 0.93 | 37% |

v2 beats SPY but not the equal-weight book, so the gate stays closed. Per signal (top fifth minus bottom fifth, a
month): momentum +1.3% (t 1.4), value −0.4% (t −0.5), quality −0.7% (t −1.2), low volatility −2.6% (t −2.8). None
shows a reliable edge on these 100 names, and most of v2's lead over SPY comes from the list itself (the equal-weight
book does better still), which survivorship bias flatters. Tuning the signals further on the same 100 names would
mostly fit noise; the next useful test is a wider universe that includes delisted companies.

## Wider universe: the S&P 500 as it stood each month

`python3 -m keers.strategies.quant fetch-sp500 --start 2017-01-01` rebuilds S&P 500 membership from Wikipedia's
constituents table and its dated change log (`history.py`), then fetches filings and prices for every company in
the index during the window, including ones that were later taken over, went bust or were demoted (`wide.py`).
`backtest --membership data/sp500-membership.json --benchmarks RSP` then ranks, each month, only the companies in the
index on the signal date. RSP (the equal-weight S&P 500 ETF) is a real-world benchmark next to SPY.

How companies that left are handled, all free sources:

- Accounts: the SEC ticker map lists only today's tickers, so a departed company's CIK comes from the map when the
  ticker still belongs to the same company, else from SEC's CIK name list (exact match after removing punctuation
  and legal suffixes). No match, or several, means no accounts; the company can still be ranked on price signals.
- Prices: Yahoo, then Tiingo (which keeps many delisted tickers). If an old ticker now belongs to a different
  company, its prices would be wrong, so the company is left out and reported. Tickers that two index companies
  used at different times are left out too.
- A held company that stops trading keeps its last value until the next rebalance, when it is sold at that value.
  For a bankruptcy that is a little optimistic.

What is still not perfect, and reported with every run: Wikipedia's change log is incomplete in places (the member
count on each date is in `universeCoverage`), some departed companies have no free price history (the priced share is
in `universeCoverage`), and every company's outcome is listed in `sp500-fetch.json`. The GitHub workflow **Quant
backtest (S&P 500, point in time)** runs it; Tiingo's free limit makes the first fetch slow, so the store is cached
and a later run continues where one stopped.

### Result (live run 41550299164, 3 Nov 2026, Feb 2017 to Oct 2026, after costs)

| Book | CAGR | Sharpe | Max drawdown |
|---|---|---|---|
| quant-c-v2 (momentum + value + quality, 1/vol sizing) | 13.3% | 0.57 | 36% |
| quant-c-v1 (momentum + low-vol, equal weight) | 6.2% | 0.28 | 35% |
| SPY | 14.8% | 0.71 | 34% |
| RSP (equal-weight S&P 500 ETF) | 11.8% | 0.56 | 39% |
| Equal-weight of ranked members (v2 run) | 12.7% | 0.62 | 37% |

699 tickers were in the index at some point in the window. 591 got SEC accounts and 633 got prices. 58 were left
out, mostly reused tickers. The priced share of members averaged 91% and never fell below 88%. Every signal's monthly
top-minus-bottom spread is within noise (t-stats from -0.9 to +0.3). The KCI 100 result (v2 at 21.5%) did not hold
once delisted names and the full index were included, so the paper gate stays closed.

The live Wikipedia page no longer carries the change log. It is read from the newest past revision that has it
(revision 1368675864, 10 Aug 2026). Since that revision, 8 names that joined are dated by "Date added". 8 names that
left have unknown dates, so they are kept as members until today.

## Paper-trading gate

Paper trading of strategy C is off until an approval for the exact model configuration is recorded in
`config/research/trials.json` (`approvedForPaper`: configuration hash, who approved it, when, the run and why). Until
then the rebalance endpoint previews tickets but refuses to execute them. An approval should only be added in a
reviewed change, after a backtest of that configuration passes `gate-v2` (reported as `verdict`): it beats both SPY
and the equal-weight book on CAGR and Sharpe, and its Deflated Sharpe Ratio, counting every model in the trial
register, is at least 0.95. Changing any setting changes the hash, so an approval never carries over to an
unreviewed model. The backtest's `research` section has the statistics. See `docs/research-protocol.md`.

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

- Survivorship bias: the KCI 100 universe is today's ticker list, so companies that were delisted are missing, which
  flatters its results (every run says so). The S&P 500 run above mostly removes this.
- Backtest costs default to costs-v1 (flat slippage plus commission on turnover) so results stay comparable with
  the registered trials. `--costs v2` prices each name's half spread, square-root impact and per-share commission for
  a fund of `--fund-nav` (default 10 million), from its volume and volatility as of the signal day; the Quant
  backtest workflow runs v2 both ways. Borrow and tax are ignored.
- No shorts and no UI panel yet. Fundamentals are SEC filers only in the backtest universe.
- Yahoo data is personal use only; it must be replaced before any commercial use.
