# Financial statements for non-US companies

Until this change only SEC filers had accounts; every other listing in the global universe was prices-only. Now a
non-US listing can get filed accounts from two free, official routes, and the snapshot, the KCI valuation and the
distress screen run on them.

| Route | Who | Source | What it gives |
|---|---|---|---|
| ESEF | Listed companies in the EU and the UK | [filings.xbrl.org](https://filings.xbrl.org) (XBRL International), found by LEI through [GLEIF](https://www.gleif.org) | Annual IFRS reports (`ifrs-full`), in the company's reporting currency |
| SEC foreign filers | 20-F and 40-F filers (many large companies from Japan, Canada, the UK, Europe, Israel, China) | SEC EDGAR company facts, as for US filers | `ifrs-full` or `us-gaap` facts in the reporting currency; annual |
| Exchange rates | Everyone not reporting in dollars | FRED (Federal Reserve H.10) and ECB reference rates | Daily rates; see `keers.data.fx` |

All are free and need no key. UK Companies House is not used: listed UK groups already publish their annual
report in ESEF through the FCA, which filings.xbrl.org carries, while Companies House accounts for listed groups
are often PDFs, and its API needs a key. It could be added later for UK private companies.

## How a listing finds its accounts

A listing is linked to a filer, and every snapshot says how:

```
python -m keers.data link VOD.L --lei <LEI>        # manual: you chose the LEI (look it up on search.gleif.org)
python -m keers.data link SHEL.L --cik 1306965     # manual: a home listing using an SEC 20-F filer's accounts
python -m keers.data find-filer VOD.L              # automatic, by name (see below); prints the candidates
python -m keers.data link VOD.L --remove
python -m keers.data refresh VOD.L                 # links by name first if needed, then fetches everything
```

The workbench does the same when a non-US listing is opened: it tries the automatic link, then loads the reports.

**Automatic links** come first from the filings.xbrl.org filer list (about 7,500 LEIs with names, cached for a
week): a link is made only when exactly one filer's name equals the listing's name after normalising. Failing that,
a link is made only when exactly one active GLEIF record has a legal or other name equal to the
listing's name after normalising (case, accents, punctuation, legal forms such as "Public Limited Company" = "PLC",
and share-class suffixes such as "-REG" or "-B", "A/S" = "AS", and a leading "AB" as in "AB Volvo"), and that record has reports on filings.xbrl.org. Otherwise
nothing is linked and the candidates are listed. Automatic links carry the basis `name-match` and a warning on
every snapshot until a person confirms them with `link`.

## Rules

- **Unknown stays unknown.** Only `ifrs-full` facts without dimensions are read. A line a company tags only with its
  own extension concept is missing, never estimated. A report that gives two different values for the same line
  and period loses both. Facts for any entity other than the LEI asked about are ignored.
- **Reporting currency.** Figures stay in the currency of the accounts (`EURm`, `GBPm`, `USDm`...). The share price
  stays in its quote currency (London prices are usually pence, `GBp`). Market cap = price × shares × the rate
  from the price currency to the reporting currency on the price date. A price whose currency the source did not
  state is not assumed to be anything, so market cap is missing.
- **Valuation in dollars.** The KCI engines work in US dollars (size-premium bands, US risk-free rate). Accounts in
  another currency are translated with one spot rate on the price date. Ratios do not change. The deal records
  the rate and its source (`translation`). Without that rate the snapshot is blocked for valuation.
- **Beta.** A non-dollar share's prices are converted to dollars day by day before the weekly regression against
  SPY, so the beta is a dollar investor's beta, consistent with the dollar valuation. A local-index beta and local
  risk-free rates are not built yet.
- **IFRS 16 leases.** Under IFRS every lease is on the balance sheet and its cost sits below EBITDA. So debt
  includes lease liabilities, and there is no separate operating-lease line. US GAAP companies are unchanged.
- **Interest.** IFRS filers use finance costs (the income-statement line). The 1% implied-rate floor that catches
  stray US GAAP tags is not applied to them, because yen, franc and euro borrowers can pay less than 1%.
- **Altman Z.** Filers outside SEC have no SIC code, so the Z'' (non-manufacturer) model is used and says so.
- **American depositary shares.** A US listing of a 20-F filer (SAP, Shell) usually trades as depositary shares
  that stand for some number of ordinary shares. That ratio is not in the XBRL facts, so its market cap is missing
  (this also fixes US GAAP 20-F filers, which were priced as if one ADS were one share). Use the home listing
  linked to the CIK instead. 40-F filers list their own shares in New York and are unaffected.

## Line items that IFRS filers tag differently

- **D&A**: the total line, else depreciation plus amortisation, else the sum of its IFRS parts (property, plant and
  equipment depreciation, right-of-use depreciation, intangible amortisation; parts not filed are named in a warning).
  As a last resort a line that also includes impairment is used, with a warning that EBITDA may be overstated.
- **Shares**: basic weighted-average shares are used when neither a current count nor diluted shares are filed.
- **Interest check**: finance costs include lease interest and the unwinding of provisions, so the "debt looks
  incomplete" check allows up to 25% of debt found for them (15% for an interest-expense line).
- **Cadence of SEC filers** comes from their forms: a 10-Q in the last 400 days means quarterly; a filer of 20-F or
  40-F (and 6-K) only is annual, even though its 20-F carries quarterly comparatives.

## Reporting cadence and staleness

ESEF covers annual reports only, and 20-F and 40-F filers send annual XBRL only. So how old figures may be depends on
how often they arrive, measured from the periods filed:

| Cadence | Limit | Why |
|---|---|---|
| quarterly | 135 days | a quarter, plus the 10-Q deadline, plus a full quarter (unchanged) |
| half-yearly | 320 days | six months, plus the four months EU and UK rules allow for the annual report, plus two weeks |
| annual | 500 days | a year, plus four months, plus two weeks |

Beyond the limit a snapshot is blocked. Past 135 days it carries a warning saying how old the figures are. **These
limits are a policy choice**: 500 days means a December year-end company is valued in April on figures 16 months
old. A tighter limit would block most European companies for part of each year.

## Not covered yet

- Half-year and quarterly figures for European companies (not in ESEF).
- Markets without free structured filings (Switzerland, China, Hong Kong, India, Canada outside SEC, Australia,
  South-East Asia), and Japan's EDINET, Korea's DART and Brazil's CVM (phase 4).
- Local benchmarks and local risk-free rates (the valuation is in dollars instead).
- The paper portfolio still trades only in its base currency (USD).
- Currencies with no free official rate (Saudi riyal, Chilean peso, a few others): accounts can be read, but market
  cap and valuation stay missing.
- ISINs: the universe has none, so links go by LEI and name rather than by security identifier.
