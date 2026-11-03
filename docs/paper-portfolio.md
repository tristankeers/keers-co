# Paper portfolio and risk limits

Paper trading only. No broker is connected and no real order is ever sent.

## How a trade happens

1. You fill in the trade ticket in the workbench (Portfolio tab): symbol, strategy (A, B or C), side, and a size
   in shares, % of NAV or an amount. Sector is optional; if blank it comes from the listing record or the SEC
   SIC code (coarse, and shown as such).
2. The desk (`keers.portfolio.desk`) fetches the latest Yahoo price (a quote, or the last daily close if that is
   newer), works out the size in whole shares, and prices a simulated fill with `costs-v2`: the price moves
   against you by half the bid-ask spread (5 basis points) plus market impact, 1.0 × the name's daily volatility
   (last 60 sessions) × √(shares ÷ 20-day average daily volume), the square-root law (Almgren et al. 2005). The
   commission is 0.005 a share, at least 1.00 and at most 1% of the trade's value (Interactive Brokers' fixed US
   rate). With no volume or volatility the impact is taken as 25 basis points, and the fill says so. A big order in
   a thin, jumpy name costs much more than a small one in a liquid name. Orders keep the cost version and the
   actual basis points applied. `costs-v1` (a flat 10 basis points plus 5 of commission) is kept for comparison.
3. The risk module (`keers.risk.checks`) gets the plain facts (cash, holdings, prices, volumes, sectors, NAV
   history and the proposed trade), recomputes the exposures itself, and returns one result per check.
4. The trade fills only when every required check is present and passed. Otherwise it is recorded as rejected
   with the reasons. Preview runs steps 2 and 3 without writing anything; placing the trade runs them again.

## The checks (`risk-limits-v2`)

| Check | Limit |
|---|---|
| Position size | 10% of NAV |
| Sector concentration | 25% of NAV (gross) |
| Strategy sleeve | A 40%, B 60%, C 40% of NAV (gross) |
| Trade vs volume | 10% of 20-day average daily volume |
| Position vs volume | 25% of average daily volume |
| Gross exposure | 150% of NAV |
| Net exposure | −30% to +100% of NAV |
| No borrowing | cash may not go below zero |
| Drawdown | warning at 10%; at 15% only trades that reduce exposure are allowed |
| Net beta to SPY | −0.30 to +1.20: each position's share of NAV times its beta, summed |
| Book volatility | at most 25% a year: today's positions replayed over the last 252 sessions of daily returns |
| Kill switch | while it is on, only trades that reduce exposure are allowed |
| Data quality | every price present, under 3 business days old, not flagged stale; USD only (no FX yet); sector, strategy and volume known; at least 120 sessions of returns for beta and volatility |

Rules that keep it safe:

- `unknown` (a missing input) never passes. A check that crashes counts as a fail. A report with a check
  missing is not approved. The ledger itself refuses to store a fill without an approved report.
- A trade that reduces an existing breach passes that check with a warning; one that adds to it fails. Large
  exits are never blocked for size against volume (flagged instead).
- `keers.risk` imports nothing from the portfolio, strategies, valuation or web code.
- To change limits, set `KEERS_RISK_LIMITS` to a JSON file with every key (see `Limits` in
  `src/keers/risk/limits.py`). A missing, unknown or out-of-range key is refused, so a v1 file needs the five new
  keys (`max_beta`, `min_beta`, `max_volatility`, `risk_days`, `min_risk_days`) before it loads.
- Beta and volatility use daily closes from the store over about the last year and a half. A name with less than
  120 sessions of history (a recent listing) has an unknown beta, so it cannot be bought until it has more.

## Kill switch

The Portfolio tab (or `POST /api/portfolios/{id}/halt` and `/resume`, each with a `reason`) turns a portfolio's
kill switch on or off. While it is on, every trade that would add gross exposure is rejected (and recorded as
rejected); closing or cutting positions still goes through. Each change is kept, with its time and reason, in the
ledger's `controls` table. The drawdown halt at 15% works the same way, automatically.

## Tamper-evident ledger (schema 2)

Every order row stores the SHA-256 of its own contents and of the row before it, and kill-switch records are
chained the same way. Editing, deleting or reordering a row breaks the chain. The desk checks the whole chain
before it records any trade and refuses to trade on a broken one; the Portfolio tab shows the result. A schema 1
ledger is upgraded in place the first time it is opened: its existing rows are chained in the order they were
written, so the chain vouches for them from that moment on.

The kill switch and the hash chain follow ideas from the MIT-licensed virattt/ai-hedge-fund project (a kill switch
checked inside the engine, a hash-chained book of record); the code is our own.

## Record and analytics

- The ledger is its own SQLite file, `paper.sqlite` next to the market-data store (`KEERS_PAPER_DB` to move it).
  It is append-only: every attempt, filled or rejected, with the price, its source, time and raw-response hash,
  the fill, costs and the full risk report.
- Positions are keyed by symbol and strategy and rebuilt from the fills each time (average cost). P&L is split
  into realised, unrealised and commissions. NAV history uses stored daily closes; drawdown is the fall from the
  highest NAV so far.
- Not modelled yet: short borrow fees, interest on cash, dividends, corporate actions, FX, intraday fills.
