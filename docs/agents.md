# Research agents

Three agents write research on one company under one strategy: a **research analyst**, a **bear reviewer**
and an **investment committee (IC) memo writer**. Everything else (the figures, the decision to propose a
trade, its size and whether it may fill) is deterministic code. Paper portfolios only.

```bash
python -m keers.agents run DBX --strategy B                       # memo only
python -m keers.agents run DBX --strategy B --portfolio 1           # memo + risk preview of the proposed trade
python -m keers.agents run DBX --strategy B --portfolio 1 --execute # memo + place the paper trade (if risk allows)
```

Or use the **Research memo** tab on a company in the workbench. Each run is saved under `agent-runs/` next to
the store: the memo (`.md`) and the full record (`.json`: fact sheet and its hash, prompts, model replies,
decision, trade).

## How a run works (`src/keers/agents/committee.py`)

1. **Fact sheet** (`facts.py`). The profile (filed figures with their SEC filings), the kci-v3 valuation and
   the distress screen and the moat screen (Strategy B, `docs/moat-compounders.md`) are read into a list of facts, each with a key such as `ev`, `dcf.vsPrice` or
   `distress.interestCover.reason`, a value, a label (reported, derived, market, assumption, estimate,
   engine) and its source.
2. **Analyst**: stance (long, short or no trade), conviction (low, medium, high), thesis, points with the facts
   they rest on, risks, and what would change its mind.
3. **Bear reviewer**: objections (minor, major, fatal), missing information, and a verdict: proceed, concerns
   or block.
4. **Decision, in code** (`decide`). No trade if the data is blocked or stale, the valuation did not run, the
   strategy is C, the analyst found no case, a short is proposed under B (long only), a B trade is proposed
   without a passing moat screen (`compounder` or `candidate`), or the bear blocked it.
   Otherwise the size is set by `sizing-v2`: 1%, 2% or 3% of NAV for low, medium or high conviction, except a
   strategy A short, which is always 1% (distressed shorts can gap up on a rescue, and borrow is not modelled).
5. **Paper desk.** With a portfolio, the proposal goes to `Workbench.paper_trade`, which is the same path as the
   trade ticket: `Desk.preview` (nothing written) or, with `--execute`, `Desk.execute`, which runs every
   `keers.risk` check again and fills only if all pass. A rejected attempt is recorded as rejected. The agents
   have no other way to trade.
6. **IC memo writer** writes the headline, summary and discussion around the decision it is given. Code adds the
   decision, the risk result, a key-figures table, data notes and the numbered sources.

## Models never type a number

A model may mention a figure only as its fact key in braces, `{ev}`. Code replaces each key with the formatted
value and a source number (`$2,781.5m [12]`). The number guard (`guard.py`) rejects any reply that contains
digits or quantities in words ("million", "percent", "twice"), or a key that is not on the fact sheet. A
rejected reply is sent back once with the problems; if it fails again, the stated rules stand in and the memo
says so. Form names such as 10-K are allowed.

So every figure in a memo was produced by tested code and can be traced to its filing, price or engine. A
model can still be wrong about what a figure means; that is what the bear reviewer and the person reading the
memo are for.

## With or without a model

- **Without a key** (the default, free): each agent follows `rules-v2` in `roles.py`. B is long when the DCF
  value is at least 15% above the price, the attractiveness score is at least 55 and the distress screen has no
  flags (medium conviction from 30%). A (changed in v2) reads the strategy A analysis instead of the DCF (see
  [strategy-a.md](strategy-a.md)): long when the screen flags stress, the equity is worth at least 30% more than
  the price at the mid recovery multiple and something at the low one, the runway is not severe and the next
  year is funded (medium conviction when the low case also covers the price); short on two or more severe flags
  with the equity worth at least 50% less than the price at the mid multiple (or no positive EBITDA) and money
  running short within a year. A without its analysis never trades. C never originates a trade. The rule-based
  bear lists stale or blocked data, distress flags, a terminal value over 75% of the DCF value, and the buyout
  engine passing; for A it also flags a short runway, a low case below the price, unmodelled borrow on shorts,
  and a short in a company that is not burning cash or whose high case is above the price.
- **With Claude**: `pip install -e ".[agents]"` and set `ANTHROPIC_API_KEY`. This is a paid API (roughly a few
  cents to a few tens of cents per memo, three calls). `KEERS_AGENT_MODEL` (default `claude-opus-5-5`) and
  `KEERS_AGENT_EFFORT` (default `medium`) tune it; `KEERS_AGENTS_OFFLINE=1` turns it off. Replies are held to a
  JSON schema. A failed call, a refusal or a reply that fails the checks falls back to the rules, and the memo
  says which agent used what.

Prompts are versioned `agents-v1`. Headlines are not given to the models: they could carry instructions
written by others, and the agents work only from the fact sheet.

## Track record (`scorecard.py`, `scorecard-v1`)

Every saved run is a call, and `python -m keers.agents scorecard` (or the Track record panel under the Research memo
tab, `GET /api/agents/scorecard`) grades it against what the share price did next:

- The return that counts is the **excess over SPY** over the same sessions, from the close the memo saw. A call is
  graded at its strategy's horizon (A: 126 sessions, about six months; B: 252, about a year) and is **pending**
  until then, shown with its return so far.
- **Hit rate** and **payoff** (average win over average loss) on long and short calls; the **average signed excess
  return** (positive when calls are right on balance).
- **IC**: rank correlation between signed conviction (short high −3 … long high +3) and excess return, with a
  t-stat, shown from 20 graded calls.
- Split by who wrote the analyst's call (the model or `rules-v1`/`rules-v2`), for the committee's proposed trades only, and
  for the bear: the average signed excess return of calls it let through and of calls it objected to. A useful
  bear objects to the worse ones.

A re-run of the same company and strategy on the same day replaces the earlier run, so it does not count twice. All
of it is arithmetic in code; no model is asked. The idea of grading each analyst at its horizon comes from the
MIT-licensed virattt/ai-hedge-fund project; the code is our own.

## Not done yet

- Strategy C trades come from its systematic signals (docs/quant-signals.md), never from the agents.
- Non-US listings have no filed figures yet, so their memos say the data is blocked.
- The agents do not read filings text, transcripts or news.
- Each run is one company; there is no screen-then-research batch yet.
