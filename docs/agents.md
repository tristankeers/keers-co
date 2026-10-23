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

1. **Fact sheet** (`facts.py`). The profile (filed figures with their SEC filings), the kci-v2 valuation and
   the distress screen are read into a list of facts, each with a key such as `ev`, `dcf.vsPrice` or
   `distress.interestCover.reason`, a value, a label (reported, derived, market, assumption, estimate,
   engine) and its source.
2. **Analyst**: stance (long, short or no trade), conviction (low, medium, high), thesis, points with the facts
   they rest on, risks, and what would change its mind.
3. **Bear reviewer**: objections (minor, major, fatal), missing information, and a verdict: proceed, concerns
   or block.
4. **Decision, in code** (`decide`). No trade if the data is blocked or stale, the valuation did not run, the
   strategy is C, the analyst found no case, a short is proposed under B (long only), or the bear blocked it.
   Otherwise the size is set by `sizing-v1`: 1%, 2% or 3% of NAV for low, medium or high conviction.
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

- **Without a key** (the default, free): each agent follows `rules-v1` in `roles.py`. B is long when the DCF
  value is at least 15% above the price, the attractiveness score is at least 55 and the distress screen has no
  flags (medium conviction from 30%). A is long when the screen flags stress but the DCF value is at least 30%
  above the price and the downside buyout MOIC is at least 1.0x; short on two or more severe flags with no DCF
  support. C never originates a trade. The rule-based bear lists stale or blocked data, distress flags, a
  terminal value over 75% of the DCF value, and the buyout engine passing.
- **With Claude**: `pip install -e ".[agents]"` and set `ANTHROPIC_API_KEY`. This is a paid API (roughly a few
  cents to a few tens of cents per memo, three calls). `KEERS_AGENT_MODEL` (default `claude-opus-5-5`) and
  `KEERS_AGENT_EFFORT` (default `medium`) tune it; `KEERS_AGENTS_OFFLINE=1` turns it off. Replies are held to a
  JSON schema. A failed call, a refusal or a reply that fails the checks falls back to the rules, and the memo
  says which agent used what.

Prompts are versioned `agents-v1`. Headlines are not given to the models: they could carry instructions
written by others, and the agents work only from the fact sheet.

## Not done yet

- Strategy C trades come from its systematic signals (docs/quant-signals.md), never from the agents.
- Non-US listings have no filed figures yet, so their memos say the data is blocked.
- The agents do not read filings text, transcripts or news.
- Each run is one company; there is no screen-then-research batch yet.
