# Strategy B: durable-moat compounders

Strategy B buys high-quality businesses with lasting advantages at a reasonable price and holds them for years.
Long only. It has its own mandate: it shares the data layer, the risk limits and the paper desk with strategies A
and C, and nothing else.

The first step is a deterministic screen, `moat-v1` (`src/keers/strategies/moat/screener.py`). It judges the
business from up to ten fiscal years of filed accounts. It does not judge the price; that is the valuation's and
the investment committee's job.

```bash
python scripts/moat_screen.py                 # live: refresh, then screen config/universe/moat-test.txt
python -m keers.strategies.moat screen MSFT KO   # companies already in the store
```

Or open the **Moat screen** tab on a company in the workbench (`GET /api/companies/{symbol}/moat`).

## How each fiscal year is worked out

Only filings made by the as-of date count, so a back-dated screen sees what the market saw.

| Figure | Formula |
|---|---|
| NOPAT | operating income × (1 − tax rate) |
| Tax rate | income tax ÷ pre-tax income, when pre-tax income is positive and the rate is 0-50%; otherwise the statutory rate, labelled an assumption (US 35% before 2018, 21% after; IFRS filers 25%) |
| Invested capital | total assets − cash and short-term investments − (current liabilities − current borrowings) |
| ROIC | NOPAT ÷ average of invested capital at the start and end of the year |
| Free cash flow | cash from operations − capital expenditure |

Invested capital is taken from the operating side of the balance sheet because buybacks can push book equity
below zero (McDonald's, for example), which would make a debt-plus-equity measure meaningless. Goodwill stays in,
so a company that grew by buying others is charged for what it paid. Current borrowings not filed at a date are
taken as zero, with a note. ROIC is not computed when invested capital is not positive.

## The checks

| Check | Pass | Watch | Otherwise |
|---|---|---|---|
| Median ROIC | 15% or more | 10% or more | fail |
| ROIC consistency: share of years with ROIC of 10% or more | 80% or more | 60% or more | fail |
| Operating margin standard deviation | up to 3pp | up to 6pp | fail |
| Revenue growth per year (first to last year of the window) | 5% or more | 0% or more | fail |
| Cash conversion: total free cash flow ÷ total NOPAT | 70% or more | 40% or more | fail |
| Net debt ÷ EBITDA (latest TTM, as the valuation builds it) | up to 2x, or net cash | up to 3x | fail |
| Diluted share count growth per year | up to 1% | up to 3% | fail |

A latest operating margin more than 5pp below the window's median makes the margin check at least a watch. Each
multi-year check needs at least five fiscal years with the figure known; with fewer it is `unknown`, which never
counts as a pass.

Status: `compounder` (every check passes), `candidate` (one or two watches), `weak` (three or more watches),
`fails` (any fail), `incomplete` (an unknown check and no fail), `insufficient history` (fewer than five fiscal
years), `not applicable` (banks, insurers and other financials, SIC 6000-6799), `blocked` (stale or missing core
data, the same rule as the valuation).

Beside the checks the screen shows the price, ungraded: earnings yield (operating income TTM ÷ enterprise value)
and free cash flow yield (latest fiscal year ÷ market capitalisation).

## How it reaches a paper trade

1. The research agents' fact sheet carries the screen as `moat.*` facts (`moat.status`, `moat.roic.value`,
   `moat.growth.reason`, `moat.earningsYield` ...). Models still write figures only as fact keys.
2. `committee.decide` proposes no Strategy B trade unless the screen says `compounder` or `candidate`, whatever
   the analyst or a model argues. The rule-based analyst also needs the DCF margin of safety, the score and a
   clean distress screen, as before; the rule-based bear reviewer turns moat watch flags into objections.
3. The trade goes through `Desk.execute` and `keers.risk`. `risk-limits-v4` adds the long-only mandate to the
   risk code itself: a trade that would leave a Strategy B position short fails `mandate.long_only`. The B sleeve
   stays capped at 60% of NAV.

## Backtest (`moat-backtest-v1`)

`python -m keers.strategies.moat backtest --start 2017-01-01 --membership sp500-membership.json --benchmarks RSP`

At the last session of each quarter the screen runs on every company in the S&P 500 on that day, including
companies that later left the index (the membership from `python -m keers.strategies.quant fetch-sp500`). The book
holds every `compounder` and `candidate`, equal weight, bought at the next session's close, with costs-v1 on
turnover. It is compared with SPY, RSP and an equal-weight book of every company the screen could judge that
quarter, and the next-quarter return of each status is reported. The Deflated Sharpe uses Strategy B's own entry
in `config/research/trials.json`; record each backtested configuration there with its run.

The workflow `moat-sp500.yml` runs it in GitHub Actions, starting from the store the quant S&P 500 workflow
caches. Company facts stored before this screen existed lack cash from operations and income tax, so it fetches
those companies' facts again once (`refetch-accounts`).

The backtest is evidence, not a gate: Strategy B trades come from the research committee, one company at a time.

## Share counts

Diluted share counts are put on today's basis before the dilution check. A stock split shows in the filings as a
later filing restating a count already filed for the same period by a large ratio (at least 1.25x); counts filed
before the split and never restated are multiplied by that ratio, with a note. A period restated across two splits
counts each split once. A restatement by more than 50x is a units slip, not a split. A count under 10,000 shares
(millions filed as units) or more than 20 times away from the other years' median is left out, with a note.

## Blocked companies

A company is `blocked` when its latest accounts are stale or its core figures are missing; the screen never
grades it on older data. `python -m keers.strategies.moat explain TICKER --as-of DATE` prints why, with every
revenue fact known that day per concept and the periodic filings made. The backtest logs the reasons each quarter
(`blocked` in each quarter of its JSON output). Two causes found on 3 November 2026, both now fixed in the data
layer:

- **SEC company facts lagging a filing.** Visa's 10-Q of 29 July 2026 was not in company facts on 3 November, so
  its accounts ended in March and read as 217 days old. A 10-Q or 10-K filed after every stored fact, with no fact
  from it, is now read from the filing's own XBRL instance (standard concepts without dimensions, dated the day it
  was filed). If that read fails, the accounts stay as they were and still read as stale.
- **Revenue moving to a new concept mid-year (ASC 606, 2018).** Most US filers tagged their 2018 10-Qs under
  `RevenueFromContractWithCustomer...` while the 2017 fiscal year stayed under the old concept, so no one concept
  gave a twelve-month figure until the next 10-K. The TTM may now take the fiscal year from one concept and the
  year to date from the other, but only when both report the same value (within 0.1%, for rounding) for every period both carry
  and share at least one. Otherwise revenue stays unknown and the company stays blocked.

## Known limits

- US GAAP and IFRS standard concepts only. A company that files cash from operations or tax under its own
  extension tag shows that check as unknown.
- Operating income includes one-off items as filed; nothing is adjusted.
- Leases: right-of-use assets are in total assets (US GAAP from 2019, IFRS 16), so leased capital counts. Before
  2019, US operating leases were off the balance sheet, which flatters the earlier years of heavy lessees such as
  retailers.
- The thresholds are a first, reviewed choice (`moat-v1`), not fitted to any backtest.
