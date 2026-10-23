# Global company universe

`keers.universe` holds every listed company Keers & Co can search, price and chart, worldwide. It replaces the
fixed 100-ticker list as the place to *find* companies. The fixed lists in `config/universe/` stay as test sets.

```
python -m keers.universe build                 # all sources (slow without an OpenFIGI key, see below)
python -m keers.universe build --exchanges LN,JT --no-us
python -m keers.universe search "toyota"
python -m keers.universe show VOD.L            # by Yahoo symbol, or by key: XLON:VOD
python -m keers.universe check --sample 5      # check 5 listings per exchange against Yahoo
python -m keers.universe stats                 # counts per exchange: where coverage is thin
python -m keers.universe export universe.csv
```

From code: `Universe(store).search(query, limit)` and `Universe(store).get(key_or_symbol)` return `Listing`
records. The GitHub Actions workflow `universe.yml` rebuilds the table weekly and uploads it as a CSV.

## Sources

| Data | Source | Reliability |
|---|---|---|
| US companies, CIK, exchange | SEC `company_tickers_exchange.json` | Official. Every SEC registrant with a ticker. |
| US venue, ETF and test-issue flags | Nasdaq Trader symbol directory | Official. If it is down, the build uses SEC's exchange labels and says so. |
| Non-US listings on 41 exchanges | OpenFIGI `/v3/filter` | Free, run by Bloomberg, open licence. Names and tickers are good. It gives **no ISIN, currency, country, sector or domicile**. |
| FIGI identifiers for US listings | OpenFIGI | Used to spot the same share class listed in several places. |
| Trading currency and Yahoo symbol | Yahoo chart endpoint (`check`) | Unofficial, personal use only (see data-sources.md). Used to confirm, never as the only source of a listing. |

OpenFIGI allows 5 filter requests a minute without a key and 20 with a free key (sign up at openfigi.com and add
`OPENFIGI_API_KEY` as a repository secret). A full build is roughly 1,000 to 1,500 requests, so about 2 to 4
hours without a key and under 1 hour with one. The key is free; it costs nothing.

## What every listing records

`key` (`<MIC>:<local ticker>`, the primary key), `symbol` (Yahoo style), `name`, `exchange`, `mic`, `country`
(where it trades), `currency`, `cik` (SEC filers), `figi`, `composite_figi`, `share_class_figi`, `security_type`,
`source`, `as_of` (when its source last listed it) and `raw_sha256` (the response it came from). `isin`, `lei`,
`sector` and `industry` exist as fields but are empty until a source for them is added.

It also says how much of it to trust:

- `currency_basis`: `yahoo` (confirmed by a Yahoo check), `exchange-rule` (US: always USD) or `exchange-default`
  (the exchange's usual currency, assumed). London is the main trap: most shares trade in pence (GBX), but some
  trade in dollars or euros. GBX, ZAC and ILA are pence, South African cents and Israeli agorot.
- `symbol_basis`: `derived` (a reliable rule, e.g. Tokyo's numeric codes), `derived-unsure` (a rule with known
  exceptions) or `yahoo-verified`.
- `coverage`: `sec` (the full SEC filings pipeline is available) or `prices-only`.
- `is_primary` and `primary_key`: when one share class trades on several exchanges (for example a US company
  also quoted on Xetra), the home listing is primary and search shows only that one by default.
- `warnings`, added when a listing is read: unconfirmed currency or symbol, a failed Yahoo check, or **stale**
  (its source has not listed it for more than 14 days, usually because that exchange's fetch failed).

## Rules

- An exchange is written only when it was fetched in full. If its fetch fails, last week's rows are kept and
  become stale; they are never replaced by an empty or half-read list. A capped test build (`--max-pages`) says
  `"capped": true` in its report.
- If OpenFIGI reports more matches than paging returned, that exchange fails instead of being stored short.
- Every request and response is stored by SHA-256, OpenFIGI's POST body included (never the API key).
- Funds, ETFs, warrants, units, rights and preferred shares are left out, and the build report counts them.

## Where coverage is thin

**Fundamentals.** SEC filers (`coverage = "sec"`) have financial statements. EU and UK listed companies (ESEF
annual reports) and home listings of SEC 20-F/40-F filers get them once linked to their filer: see
[foreign-financials.md](foreign-financials.md). The `coverage` field still says `prices-only` for those until the
universe build records links. Other markets are prices-only.

**Listings.** About 41 exchanges are covered. Not yet covered: Vietnam, Argentina, Colombia, Peru, Egypt,
Nigeria, Kenya, Pakistan, the Gulf markets other than Saudi Arabia, Central and Eastern Europe other than
Warsaw and Athens, Taiwan's TPEx, India's BSE-only companies, Japan's regional exchanges and Canada's CSE.
Adding one is a row in `exchanges.py` once its OpenFIGI code and Yahoo suffix are confirmed.

**Yahoo symbols.** Malaysia is unreliable (Yahoo uses numeric codes, Bloomberg uses letters). Nordic share
classes (VOLVB vs VOLV-B) are fixed by the check. India, Mexico and London have occasional punctuation
mismatches. `check` finds these; until a listing is checked its symbol is marked unconfirmed.

**Identity.** No ISINs, LEIs, sectors or company domicile yet. `country` is where the share trades, not where
the company is based.

## What failed for a non-US company before phase 2

These are the checks that kept non-US listings `prices-only`. [foreign-financials.md](foreign-financials.md) says
how each is handled now: IFRS concepts, reporting currencies and FX, a reporting-cadence staleness rule, a dollar
beta, translation to dollars for the engines. Local benchmarks, local risk-free rates and KCI's US LBO defaults
are still US-based.

| Check | What goes wrong |
|---|---|
| Fundamentals come only from SEC XBRL, read as `us-gaap` concepts in USD | IFRS filers (even those filing 20-F with SEC) and all local filings are not read. Every core figure is missing, so the snapshot is blocked. |
| Fundamentals older than 135 days block a snapshot | Built for US quarterly 10-Qs. Most European, UK and Australian companies report twice a year, and 20-F filers once. They would be blocked for most of the year. This needs a reporting-cadence rule, which is a policy choice. |
| Market cap = price × shares | A London price in pence with accounts in dollars (common for miners and oil companies) is off by 100× and by the exchange rate. Needs FX rates and the minor-unit rule. |
| Beta against SPY | Wrong benchmark for a non-US share. Needs a local index. |
| Risk-free rate = 10-year US Treasury | Wrong for a non-USD valuation. Needs local government bond yields. |
| Debt roll-forward, interest and D&A checks | Read `us-gaap` tags. IFRS uses different concepts. |
| KCI's LBO defaults | US leverage, US tax and US interest deductibility rules. |

## Phased plan

1. **Listing master (this change).** Global search, identifiers, currency and symbol checks, coverage stats,
   weekly rebuild.
2. **SEC foreign filers (done, see foreign-financials.md).** Read the `ifrs-full` facts that 20-F and 40-F filers already send to SEC (many of the
   largest non-US companies), keep facts in their reporting currency, add FX rates from FRED, link those
   companies' home listings to their CIK, and add a reporting-cadence staleness rule.
3. **Europe and UK (done except local benchmarks and rates; valuation is in dollars instead).** Annual IFRS reports in ESEF format from filings.xbrl.org (free). Local benchmarks and
   local risk-free rates.
4. **Asia and Brazil.** Japan's EDINET and Korea's DART (both free with a key), Brazil's CVM open data.
5. **Markets with no free structured filings** (China, Hong Kong, India, Canada, Australia, South-East Asia):
   Yahoo's fundamentals as a labelled, unofficial cross-check, or prices only. That choice is the owner's.
