"""Diagnostics: which XBRL concepts companies file, so the data layer's candidate lists can be extended.

Usage: python scripts/concept_diag.py TICKER [TICKER ...]   (needs SEC_USER_AGENT; TIINGO_TOKEN optional)
Prints, for each company, every concept filed since 2025-01-01 matching the gap patterns, and every period of
the interest-expense concepts, so a failed TTM can be traced. Read-only; stores nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta

from keers.data.http import Fetcher
from keers.data.market import TIINGO_URL
from keers.data.refresh import user_agent
from keers.data.sec import FACTS_URL, SEC, SUBMISSIONS_URL, concept_inventory
from keers.data.store import Store

PATTERN = (r"(Interest|BeforeIncomeTax|IncomeLoss|Cash|Debt|Borrow|Notes|Loan|Credit|Revolv|OperatingIncome|CostsAndExpenses|Revenues$|"
           r"Liabilities|Assets$|AssetsCurrent|RetainedEarnings|StockholdersEquity)")  # fmt: skip
SINCE = date(2025, 1, 1)


def main() -> int:
    tickers = [t.upper() for t in sys.argv[1:]]
    if not all(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}|CIK\d{1,10}", t) for t in tickers):
        print("bad tickers")
        return 2
    f = Fetcher(user_agent())
    sec = SEC(f, Store(":memory:"))
    tmap = sec.tickers()
    token = (os.environ.get("TIINGO_TOKEN") or "").strip()
    for t in tickers:
        print(f"\n===== {t}")
        if t.startswith("CIK"):
            subs = json.loads(f.get(SUBMISSIONS_URL.format(cik=int(t[3:]))).body)
            recent = subs.get("filings", {}).get("recent", {})
            last = list(zip(recent.get("form", [])[:8], recent.get("filingDate", [])[:8], strict=False))
            print(
                f"name {subs.get('name')}; tickers {subs.get('tickers')}; exchanges {subs.get('exchanges')}; "
                f"former names {[x.get('name') for x in subs.get('formerNames', [])]}; latest filings {last}"
            )
            continue
        if t not in tmap:
            near = sorted(k for k, (_, name) in tmap.items() if t[:2] in k and len(k) <= 5)[:20]
            print(f"not in SEC ticker map; tickers starting {t[:2]}: {near}")
            continue
        cik, name = tmap[t]
        print(f"cik {cik} {name}")
        doc = json.loads(f.get(FACTS_URL.format(cik=cik)).body)
        for r in concept_inventory(doc, PATTERN, SINCE):
            print(
                f"  {r['concept']} [{r['unit']}] {r['start'] or ''}..{r['end']} = {r['value']:,} ({r['form']}, n={r['n']})"
            )
        for tax, cs in doc.get("facts", {}).items():
            for c, body in cs.items():
                if not re.search(r"^Interest(Expense|Paid)", c):
                    continue
                rows = [
                    x for x in body["units"].get("USD", []) if x["end"] >= "2024-01-01" and x.get("start")
                ]
                seen = sorted({(x["start"], x["end"], x["val"], x["form"]) for x in rows})
                print(
                    f"  periods {tax}:{c}: "
                    + "; ".join(f"{s}..{e}={v / 1e6:,.0f}m({fm})" for s, e, v, fm in seen)
                )
        if token:
            start = (date.today() - timedelta(days=400)).isoformat()
            try:
                body = json.loads(f.get(TIINGO_URL.format(ticker=t.lower(), start=start),
                                        headers={"Authorization": f"Token {token}"}).body)  # fmt: skip
                adj = [x for x in body if x.get("adjClose") is not None]
                print(
                    f"  tiingo: {len(body)} rows since {start}, {len(adj)} with adjClose, first {body[0]['date'][:10] if body else None}"
                )
            except Exception as e:  # noqa: BLE001
                print(f"  tiingo error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
