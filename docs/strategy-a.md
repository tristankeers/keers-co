# Strategy A: distressed and special situations

Paper trading only. Strategy A looks for companies under financial stress where the price may be wrong because
other holders are forced or afraid. This page covers the research pipeline from the screen to a reviewed memo and a
paper-desk check. Being on the candidate list is not a finding.

## What is built

| Part | Module | Version |
|---|---|---|
| Distress screen (5 checks) | `keers.strategies.distress` | `distress-v1` |
| Runway, funding gap, recovery value | `keers.strategies.distressed.analysis` | `a-analysis-v1` |
| Candidate list | `keers.strategies.distressed.candidates` | `a-candidates-v1` |
| Rule-based analyst and bear for A | `keers.agents.roles` | `rules-v2` |
| Sizing | `keers.agents.committee` | `sizing-v2` |
| Risk limits | `keers.risk` | `risk-limits-v3` |
| One run, end to end | `keers.strategies.distressed.pipeline` | `a-pipeline-v1` |

## The run

```
python -m keers.strategies.distressed run --universe config/universe/distress-test.txt --top 10
```

1. **Refresh** filings (SEC EDGAR) and prices (Yahoo, Tiingo fallback). `--no-refresh` uses the store as it is.
2. **Screen** every ticker with the distress screen. See the module docstring for the five checks.
3. **Candidates.** Only `distressed` and `stressed` companies qualify, and only with a market value of at least
   $100m, a price of at least $1 and at least $1m traded a day (20-session average), in US dollars. A figure that
   cannot be measured excludes the company. Candidates are ranked by severity (2 points per severe check, 1 per
   watch), then by the fall from the 52-week high. Every company is accounted for in the report: candidate,
   excluded (with the reason), needs data (`incomplete`), or blocked (stale data). With `--previous`, the report
   lists new candidates, ones that dropped out, and status changes.
4. **Research.** The analyst, bear reviewer and memo writer (`keers.agents`) write an IC memo on each of the top
   `--top` candidates under strategy A. Without `ANTHROPIC_API_KEY` (the default, free) they follow `rules-v2`.
5. **Paper desk.** A proposed trade is previewed against the risk limits on a fresh paper portfolio in the output
   folder. `--portfolio ID --execute` places it on a named paper portfolio instead; the risk limits apply again.
6. **Report**: `strategy-a.md`, `strategy-a.json`, `candidates.json` and the memos in `agent-runs/`.

The **Strategy A research run** workflow (`.github/workflows/strategy-a.yml`) does this from the Actions tab and
caches the candidate list, so each run lists what changed since the last one. The model is off unless `use_model`
is ticked (paid API calls).

## The analysis (`a-analysis-v1`)

From the same filed period as the screen. Each part has a level and a sentence saying why; unknown never counts
as clear.

- **Liquidity runway**: (cash + short-term investments + undrawn committed credit lines) ÷ monthly burn, where burn
  is negative free cash flow (cash from operations − capital expenditure, TTM). Severe under 12 months, watch under
  24. Positive free cash flow: no burn, clear.
- **Funding gap** over the next year: cash + short-term investments + undrawn credit lines + last year's free cash
  flow − debt due within a year. Negative is severe (new money or a refinancing is needed). It assumes the next
  year's free cash flow repeats the last.
- **Recovery value**: EBITDA × 4x, 6x and 8x (low, mid, high) plus cash, set against total debt. Debt is paid
  first; equity gets what is left, per share, against the last close. Severe when equity gets nothing at 6x,
  watch when it gets less than the price. Unknown when EBITDA is not positive, when the debt figure failed its
  own checks, or when the accounts and the share price are in different currencies.

The multiples are a generic assumption for a distressed sale, the same for every company. Not modelled: secured
before unsecured debt, leases outside debt, pensions, restructuring costs and time, and new shares issued in a
restructuring. All of these lower what equity would get, so the per-share values are best cases.

## Rules without a model (`rules-v2`, strategy A)

- **Long**: the screen flags stress, equity is worth at least 30% more than the price at 6x and something at 4x,
  the runway is not severe and the funding gap is clear. Medium conviction when the 4x case also covers the price,
  otherwise low.
- **Short**: two or more severe checks, equity worth at least 50% less than the price at 6x (or no positive
  EBITDA), and the runway or the funding gap is severe. Medium conviction when even the 8x case is below the
  price, otherwise low.
- Otherwise no trade. Without the analysis, A never trades (in code, `committee.decide`).
- The bear flags a runway under two years and a 4x case below the price on longs; and on shorts, borrow not
  modelled, a company that is not burning cash, and an 8x case above the price.

## Risk (`risk-limits-v3`, `sizing-v2`)

- Strategy A positions are capped at 5% of NAV (B and C keep 10%), within the 40% A sleeve.
- No new or bigger short below $5 a share; every other short passes with a warning that borrow availability and
  fees are not modelled.
- A shorts are always the low size (1% of NAV); longs are 1%, 2% or 3% by conviction.

## Known gaps and next steps

- The default universe is the 15-name test list. A wider universe (NYSE and Nasdaq SEC filers) is the next step;
  Yahoo and SEC rate limits mean it should run in batches.
- Recovery multiples by sector, and debt ranking from the filings (secured, unsecured, convertible).
- Company bond prices and credit default swap spreads are not in the data layer.
- Borrow availability and fees, and corporate actions (rights issues, debt-for-equity swaps), are not modelled.
- Strategy A has no backtest yet: the screen would need point-in-time filings for companies that later went
  bankrupt and delisted. Until then, the agent scorecard (`python -m keers.agents scorecard`) is how its calls are
  graded.
