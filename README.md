# Keers & Co

A research platform for three investment strategies: distressed and special situations, durable-moat compounders, and systematic quant. Research only. Paper portfolios only. No live trading.

**Status: Phase 0 (foundation).** Nothing here makes investment decisions yet. This repository currently holds:

- `legacy/kci/`: KCI's calculation engine and dataset, copied unchanged from KCI's published page ([tristankeers/kci](https://github.com/tristankeers/kci), commit `2232610`). KCI's original source code could not be recovered, so the published page is the source of record. See `legacy/kci/SOURCE.json`.
- `tests/legacy/`: tests that lock in what that engine produces today (the 19 hand-worked checks, the Dropbox worked case and the default model for all 100 companies).
- `src/keers/`: the Python core. Empty for now. The first module will be a Python port of the KCI engine, checked against the frozen original.

KCI itself is a separate project and is never modified from here.

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

- Default buyouts are priced at the market price with no takeover premium. This yields "Proceed" for EQT, Expand Energy and Gen Digital.
- The default DCF keeps today's capex ratio forever and uses the same beta and a 2% size premium for every company. It values the median company at about 55% of market value, and some (Oracle, Meta, Alphabet) negative.
- Interest is fully tax-deductible regardless of leverage (no US section 163(j) cap).
- Figures are a one-off snapshot, and "verified" labels reflect AI-assisted checks.

Decision records are in `docs/adr/`.
