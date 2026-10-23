# Paper portfolio and risk limits

Paper trading only. No broker is connected and no real order is ever sent.

## How a trade happens

1. You fill in the trade ticket in the workbench (Portfolio tab): symbol, strategy (A, B or C), side, and a size
   in shares, % of NAV or an amount. Sector is optional; if blank it comes from the listing record or the SEC
   SIC code (coarse, and shown as such).
2. The desk (`keers.portfolio.desk`) fetches the latest Yahoo price (a quote, or the last daily close if that is
   newer), works out the size in whole shares, and prices a simulated fill: the price moved against you by
   10 basis points, plus a 5 basis point commission (minimum 1.00). These are `costs-v1`.
3. The risk module (`keers.risk.checks`) gets the plain facts (cash, holdings, prices, volumes, sectors, NAV
   history and the proposed trade), recomputes the exposures itself, and returns one result per check.
4. The trade fills only when every required check is present and passed. Otherwise it is recorded as rejected
   with the reasons. Preview runs steps 2 and 3 without writing anything; placing the trade runs them again.

## The checks (`risk-limits-v1`)

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
| Data quality | every price present, under 3 business days old, not flagged stale; USD only (no FX yet); sector, strategy and volume known |

Rules that keep it safe:

- `unknown` (a missing input) never passes. A check that crashes counts as a fail. A report with a check
  missing is not approved. The ledger itself refuses to store a fill without an approved report.
- A trade that reduces an existing breach passes that check with a warning; one that adds to it fails. Large
  exits are never blocked for size against volume (flagged instead).
- `keers.risk` imports nothing from the portfolio, strategies, valuation or web code.
- To change limits, set `KEERS_RISK_LIMITS` to a JSON file with every key (see `Limits` in
  `src/keers/risk/limits.py`). A missing, unknown or out-of-range key is refused.

## Record and analytics

- The ledger is its own SQLite file, `paper.sqlite` next to the market-data store (`KEERS_PAPER_DB` to move it).
  It is append-only: every attempt, filled or rejected, with the price, its source, time and raw-response hash,
  the fill, costs and the full risk report.
- Positions are keyed by symbol and strategy and rebuilt from the fills each time (average cost). P&L is split
  into realised, unrealised and commissions. NAV history uses stored daily closes; drawdown is the fall from the
  highest NAV so far.
- Not modelled yet: short borrow fees, interest on cash, dividends, corporate actions, FX, intraday fills.
