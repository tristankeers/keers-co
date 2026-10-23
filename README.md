# Keers & Co

A research platform for three investment strategies: distressed and special situations, durable-moat compounders, and systematic quant. Research only. Paper portfolios only. No live trading.

**Status: Phase 1 in progress (engine ported; live data layer running; calculation version kci-v2 fixes the audit's default assumptions).** Nothing here makes investment decisions yet. This repository currently holds:

- `legacy/kci/`: KCI's calculation engine and dataset, copied unchanged from KCI's published page ([tristankeers/kci](https://github.com/tristankeers/kci), commit `2232610`). KCI's original source code could not be recovered, so the published page is the source of record. See `legacy/kci/SOURCE.json`.
- `tests/legacy/`: tests that lock in what that engine produces today (the 19 hand-worked checks, the Dropbox worked case and the default model for all 100 companies).
- `src/keers/valuation/kci_v1.py` and `kci_v1_defaults.py`: the KCI engine and its default assumptions ported to Python as calculation version `kci-v1`. The port matches the frozen JavaScript on all 100 companies and on 3,864 randomised cases. It keeps KCI's known issues on purpose; fixes come as version 2.

- `src/keers/valuation/kci_v2_defaults.py`: calculation version `kci-v2`. Same engine formulas, three default assumptions fixed: buyouts pay a 30% takeover premium on market equity and exit at the market multiple; the DCF uses each company's own beta (measured weekly against SPY over two years, Blume-adjusted) and the live 10-year Treasury yield, with the size premium only for smaller companies; capex fades from today's ratio to the D&A ratio by the final year. With no beta the DCF is not run. What changed on KCI's 100-company baseline is in `docs/calc/kci-v2-changes.md`. Live snapshots use `kci-v2` by default (`--calc-version kci-v1` for the old one).
- `src/keers/strategies/distress.py`: Strategy A's first step, a distress screen on live data. Five checks per company (interest cover, net debt / EBITDA, debt due within a year against cash, Altman Z, fall from the 52-week high), each with its thresholds and a sentence saying why, plus the Baa credit spread as market backdrop. A check with missing data is `unknown` and never counts as clear. Run `python scripts/distress_screen.py [TICKER ...]` (or the screen step in `live-smoke.yml`); the default test list is `config/universe/distress-test.txt`.
- `src/keers/universe/`: the global company universe. Listed companies on the US exchanges and 41 others, from SEC, Nasdaq Trader and OpenFIGI, with each listing's currency, Yahoo symbol and fundamentals coverage marked as confirmed or assumed. `python -m keers.universe search "toyota"`. Non-US companies have prices, charts and headlines but no fundamentals yet. See `docs/universe.md` for the sources, the thin spots and the plan.
- `src/keers/data/`: the live data layer. It pulls SEC filings (XBRL company facts), daily share prices (Yahoo Finance first, Tiingo as fallback), intraday bars, quotes and headlines (Yahoo), and Treasury and credit rates (FRED). SEC EDGAR stays the source of truth for financial statements. **Yahoo Finance is unofficial and for personal use only; it must be replaced by a licensed feed before any commercial or regulated use.** See `docs/data-sources.md`. Every number is stored with the filing, date and response hash it came from, and is read as of a date, so a back-dated run sees only what had been filed by then. Snapshots with missing or stale figures are blocked and never reach the valuation engine.

KCI itself is a separate project and is never modified from here.

## Live data

SEC requires every client to identify itself, so set `SEC_USER_AGENT` to your name and a contact email first.

```bash
export SEC_USER_AGENT="Your Name you@example.com"
python -m keers.data refresh DBX ADBE            # or: --universe config/universe/kci100.txt
python -m keers.data snapshot DBX --analyse      # snapshot as of today, then kci-v2 if not blocked
python -m keers.data snapshot DBX --as-of 2026-10-15
python -m keers.data status
python -m keers.data quote DBX ADBE              # latest price with freshness warnings (Yahoo)
python -m keers.data bars DBX --interval 5m      # completed intraday bars for charts (Yahoo)
python -m keers.data news DBX                    # headlines: untrusted third-party text (Yahoo)
```

The database defaults to `data/keers.sqlite` (`KEERS_DB` or `--db` to change it). In GitHub Actions, set `SEC_USER_AGENT` as a repository secret (masked in the public logs; a variable also works) to turn on the weekday refresh (`refresh.yml`), and run `live-smoke.yml` by hand to check the live build against Dropbox's figures from filings. Prices come from Yahoo first; `TIINGO_TOKEN` turns on Tiingo as the fallback, and every fallback is reported in the run record. `KEERS_PRICE_SOURCES` changes the order (see `docs/data-sources.md`).

How figures are built: trailing twelve months = latest fiscal year + current year-to-date − prior year-to-date; EBITDA = operating income + D&A; net debt = borrowings (including finance leases, excluding operating leases) − cash − short-term investments; EV = price × shares + net debt. A price more than 3 business days old, or a latest period more than 135 days old, blocks the snapshot. Live figures are labelled `filed`, never `verified`, until a cross-check is built.

## Research workbench

A local web interface over everything above: company search (any listing; US SEC filers get the full view),
a profile with every figure labelled reported, derived, market, assumption or estimate and traced to its filing
or source, the kci-v2 DCF and buyout with editable inputs and a side-by-side kci-v1 comparison, the distress
screen with its reasons, daily and intraday charts drawn from our own stored bars, and headlines.

```bash
pip install -e ".[web]"
export SEC_USER_AGENT="Your Name you@example.com"
python -m keers.web                  # http://127.0.0.1:8000 ; KEERS_OFFLINE=1 for the store only
```

The Portfolio tab holds paper portfolios: positions tagged by strategy (A, B, C), simulated fills at the latest
Yahoo price with a slippage and commission margin, P&L, exposures and drawdown. Every trade goes through the
deterministic risk limits in `src/keers/risk/` first and fills only if every check passes; blocked attempts are
recorded too. See `docs/paper-portfolio.md`.

It runs on your own machine only: Yahoo's data is for personal use, so do not put it on a public address. Why
this stack: `docs/adr/0004-research-workbench-stack.md`. The `workbench-live.yml` workflow runs it against the
live sources and saves screenshots.

## Running the checks

Needs Python 3.11+ and Node.js 20+.

```bash
pip install -e ".[dev]"
ruff check . && mypy && pytest -v
```

`pytest -m "not network"` skips the one test that downloads KCI's published page to confirm the frozen files are byte-for-byte identical.

## Rules this project follows

1. Language models never do financial arithmetic. Calculations live in tested, versioned code.
2. Every figure records its source, retrieval time, period, currency and units.
3. Risk limits are deterministic code, separate from anything that proposes investments. A missing or failed check is never an approval.
4. Calculation changes ship as a new version with their output changes recorded. The frozen KCI outputs are `kci-v1`.
5. No result is claimed until it is tested.

## Known issues inherited from KCI

The audit (22 Oct 2026) found problems in KCI's default assumptions. They are frozen here on purpose so a fix can be measured against them:

- Default buyouts are priced at the market price with no takeover premium. This yields "Proceed" for EQT, Expand Energy and Gen Digital. *Fixed in kci-v2.*
- The default DCF keeps today's capex ratio forever and uses the same beta and a 2% size premium for every company. It values the median company at about 55% of market value, and some (Oracle, Meta, Alphabet) negative. *Fixed in kci-v2.*
- Interest is fully tax-deductible regardless of leverage (no US section 163(j) cap).
- Figures are a one-off snapshot, and "verified" labels reflect AI-assisted checks.

Decision records are in `docs/adr/`.
