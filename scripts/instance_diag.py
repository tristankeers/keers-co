"""Diagnostics: the facts in a company's latest 10-Q or 10-K that the SEC company-facts API leaves out.

Company facts carry only standard (us-gaap, dei, srt) concepts without dimensions. Debt filed under a
company-specific concept, or only with a dimension (for example per debt instrument), is missing there. This
script reads the filing's own XBRL instance and prints those facts, so the data layer's reading of them can be
designed from what companies actually file.

Usage: python scripts/instance_diag.py TICKER [TICKER ...]   (needs SEC_USER_AGENT). Read-only; stores nothing.
"""

from __future__ import annotations

import re
import sys

from keers.data.http import Fetcher
from keers.data.refresh import user_agent
from keers.data.sec import SEC
from keers.data.store import Store
from keers.data.xbrl import fetch_latest_instance

PATTERN = re.compile(
    r"(Debt|Borrow|Notes|Loan|Credit|Revolv|Facility|Convertible|Maturit|Obligation|Interest|OperatingIncome|"
    r"IncomeLoss|BeforeIncomeTax|Lease)",
    re.I,
)


def main() -> int:
    tickers = [t.upper() for t in sys.argv[1:]]
    if not all(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t) for t in tickers):
        print("bad tickers")
        return 2
    f = Fetcher(user_agent())
    sec = SEC(f, Store(":memory:"))
    tmap = sec.tickers()
    for t in tickers:
        print(f"\n===== {t}")
        if t not in tmap:
            print("not in SEC ticker map")
            continue
        cik = tmap[t][0]
        try:
            inst = fetch_latest_instance(f, cik)
        except Exception as e:  # noqa: BLE001 (diagnostics: report and carry on)
            print(f"  instance error: {e}")
            continue
        print(f"  {inst.form} {inst.accn} filed {inst.filed}, instance {inst.url}")
        print(f"  extension prefix(es): {sorted(inst.extension_prefixes)}")
        rows = [x for x in inst.facts if PATTERN.search(x.concept) and x.unit == "USD"]
        ext = [x for x in rows if x.prefix in inst.extension_prefixes and not x.dims]
        dim = [x for x in rows if x.prefix == "us-gaap" and x.dims]
        print(f"  -- company-specific concepts, no dimensions ({len(ext)})")
        for x in sorted(ext, key=lambda x: (x.concept, x.end)):
            print(f"    {x.prefix}:{x.concept} {x.start or ''}..{x.end} = {x.value:,.0f}")
        print(f"  -- us-gaap debt concepts with dimensions ({len(dim)}; first 80)")
        for x in sorted(dim, key=lambda x: (x.concept, x.end))[:80]:
            if re.search(r"Debt|Borrow|Notes|Loan|Credit|Convertible|Maturit", x.concept):
                d = ", ".join(f"{a}={m}" for a, m in x.dims)
                print(f"    us-gaap:{x.concept} {x.start or ''}..{x.end} [{d}] = {x.value:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
