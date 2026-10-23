"""Live smoke test: fetch real filings and prices, then compare with the figures KCI recorded.

Run in GitHub Actions (the build sandbox cannot reach sec.gov). Needs SEC_USER_AGENT.

The pass condition is narrow on purpose. Dropbox's KCI revenue ($2,531.6m, twelve months to 30 Jun 2026) was built
from its filings the same way this layer builds TTM, so the live figure as of 15 Oct 2026 must match it within 0.5%.
The other companies are reported for comparison only, because KCI took them from a third-party site.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from keers.data import snapshot
from keers.data.http import Fetcher
from keers.data.refresh import price_report, refresh, user_agent
from keers.data.sec import FACTS_URL, concept_inventory
from keers.data.store import Store
from keers.valuation import kci_v1_defaults as V1
from keers.valuation import kci_v2_defaults as V2
from keers.valuation import kci_v3_defaults as V3

AS_OF = date(2026, 10, 15)
TICKERS = ["DBX", "ADBE", "CRM", "INTU", "WDAY", "OPEN"]
# Debt filed as lines of credit used to block these two; they must now reach the valuation.
MUST_VALUE = {"DBX", "OPEN"}
BETA_CHECKED = {"DBX", "ADBE", "CRM", "INTU", "WDAY"}
DIAG_PATTERN = r"^(LongTermDebt|DebtCurrent|ShortTermBorrowings|CommercialPaper|Convertible(Debt|Notes)|SecuredDebt|.*TermLoan|LineOfCredit$|InterestExpense|Depreciation|AmortizationOfIntangibleAssets|AdjustmentForAmortization|CashCashEquivalentsAndShortTermInvestments|EntityCommonStockSharesOutstanding|WeightedAverageNumberOfDilutedSharesOutstanding|Interest|ProceedsFrom.*(Debt|Loan|Borrow|Credit)|Liabilities$|.*Loan|DebtInstrument(Carrying|Face))"
# (ticker, field, KCI figure, tolerance). Pinned to AS_OF, so later filings cannot move them.
# Dropbox's revenue and EBITDA were built by KCI from filings. The others come from KCI's third-party source;
# "netDebtInclLeases" adds operating leases because that source counts them as debt.
CHECKS = [
    ("DBX", "revenue", 2531.6, 0.005),
    ("DBX", "ebitda", 830.3, 0.01),
    ("ADBE", "revenue", 25970.0, 0.01),
    ("CRM", "revenue", 43940.0, 0.01),
    ("INTU", "revenue", 21450.0, 0.01),
    ("WDAY", "revenue", 10160.0, 0.01),
    ("ADBE", "netDebtInclLeases", 1150.0, 0.05),
    ("CRM", "netDebtInclLeases", 30980.0, 0.05),
    ("INTU", "netDebtInclLeases", 1220.0, 0.05),
    ("WDAY", "netDebtInclLeases", 370.0, 0.05),
]
BETA_RANGE = (0.3, 3.0)  # sanity bounds: every company here must get a measured beta inside them


def main() -> int:
    seed = json.loads(Path("legacy/kci/seed.json").read_text())["deals"]
    out_dir = Path("smoke-out")
    out_dir.mkdir(exist_ok=True)
    store = Store(out_dir / "smoke.sqlite", keep_bodies=True)
    res = refresh(
        store, TICKERS, Fetcher(user_agent()), (os.environ.get("TIINGO_TOKEN") or "").strip() or None
    )
    print("refresh:", json.dumps({"status": res["status"], "failed": res["failed"]}, indent=2))
    print("prices:", json.dumps(price_report(res), indent=2))
    ok = not res["failed"]
    rows = []
    for t in TICKERS:
        snap = snapshot.build(store, t, AS_OF)
        (out_dir / f"{t}.json").write_text(json.dumps(snap.as_dict(), indent=2, default=str))
        kci = seed.get(f"us-{t.lower()}", {})
        lease = snap.values.get("operatingLeases")
        nd = snap.values.get("netDebt")
        if nd is not None and nd.known and lease is not None and lease.known:
            rows.append((t, "nd+lease", (nd.value or 0) + (lease.value or 0), kci.get("netDebt"),
                         ((nd.value or 0) + (lease.value or 0)) / kci["netDebt"] - 1 if kci.get("netDebt") else None,
                         nd.period_end))  # fmt: skip
        for field in ("revenue", "ebitda", "netDebt", "ev"):
            live = snap.values.get(field)
            lv = live.value if live is not None else None
            kv = kci.get(field)
            diff = (lv / kv - 1) if lv is not None and kv else None
            rows.append((t, field, lv, kv, diff, live.period_end if live else None))
        print(f"{t}: blocked={snap.blocked} {snap.blocked_by}")
        for k in ("ebit", "da", "interestExpense", "liquidAssets", "debt", "operatingLeases", "shares"):
            x = snap.values.get(k)
            if x is not None:
                print(f"    {k}: {x.value} | {x.method} | end {x.period_end} | {list(x.warnings)}")
        if snap.cik:
            body = store.latest_body(FACTS_URL.format(cik=snap.cik))
            if body:
                inv = concept_inventory(json.loads(body), DIAG_PATTERN, date(2025, 7, 1))
                print(f"    concepts filed since 2025-07-01 matching {DIAG_PATTERN}:")
                for r in inv:
                    print(
                        f"      {r['concept']} [{r['unit']}] {r['start'] or ''}..{r['end']} = {r['value']:,} ({r['form']})"
                    )
        nd, lease = snap.values.get("netDebt"), snap.values.get("operatingLeases")
        derived = {
            "netDebtInclLeases": (nd.value or 0) + (lease.value or 0)
            if nd is not None and nd.known and lease is not None and lease.known
            else None
        }
        for ct, field, want, tol in CHECKS:
            if ct != t:
                continue
            x = snap.values.get(field)
            got = derived[field] if field in derived else (x.value if x is not None else None)
            good = got is not None and abs(got / want - 1) <= tol
            print(
                f"CHECK {t} {field}: live {got} vs KCI {want} (tolerance {tol:.1%}) -> {'PASS' if good else 'FAIL'}"
            )
            ok = ok and good
        b = snap.values.get("beta")
        if t in BETA_CHECKED:
            good = b is not None and b.known and BETA_RANGE[0] <= (b.value or 0) <= BETA_RANGE[1]
            print(
                f"CHECK {t} beta: {b.value if b else None} ({b.method if b else 'not computed'}) -> {'PASS' if good else 'FAIL'}"
            )
            ok = ok and good
        if t in MUST_VALUE:
            good = not snap.blocked
            print(f"CHECK {t} not blocked: {snap.blocked_by or 'clear'} -> {'PASS' if good else 'FAIL'}")
            ok = ok and good
        if not snap.blocked:
            deal = snapshot.to_kci_deal(snap)
            for name, mod in (("kci-v1", V1), ("kci-v2", V2), ("kci-v3", V3)):
                a = mod.analyse(deal)
                base = a["scenarios"]["base"]["result"]
                dcf = a["dcf"]
                print(
                    f"    {name}: entry {base['entry']['entryMultiple']:.2f}x, base IRR {base['returns']['irr']:.1%}, "
                    f"WACC {a['wacc']['value']:.2%}, DCF EV / market EV "
                    f"{dcf['primary']['ev'] / deal['ev']:.0%}, verdict {a['rec']['decision']}"
                    if base["ok"] and dcf["ok"] and a["wacc"]["ok"]
                    else f"    {name}: LBO {base.get('errors')} DCF {dcf.get('errors')}"
                )
    print(f"\n{'ticker':6} {'field':8} {'live':>12} {'KCI':>12} {'diff':>8}  period end")
    for t, f, lv, kv, d, pe in rows:
        ls = f"{lv:,.1f}" if lv is not None else "—"
        ks = f"{kv:,.1f}" if isinstance(kv, (int, float)) else "—"
        ds = f"{d:+.1%}" if d is not None else "—"
        print(f"{t:6} {f:8} {ls:>12} {ks:>12} {ds:>8}  {pe}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
