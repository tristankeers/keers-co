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
from keers.data.refresh import refresh, user_agent
from keers.data.store import Store

AS_OF = date(2026, 10, 15)
TICKERS = ["DBX", "ADBE", "CRM", "INTU", "WDAY"]
CHECK = {"DBX": ("revenue", 2531.6, 0.005)}


def main() -> int:
    seed = json.loads(Path("legacy/kci/seed.json").read_text())["deals"]
    out_dir = Path("smoke-out")
    out_dir.mkdir(exist_ok=True)
    store = Store(out_dir / "smoke.sqlite", keep_bodies=False)
    res = refresh(store, TICKERS, Fetcher(user_agent()), os.environ.get("TIINGO_TOKEN") or None)
    print("refresh:", json.dumps({"status": res["status"], "failed": res["failed"]}, indent=2))
    ok = not res["failed"]
    rows = []
    for t in TICKERS:
        snap = snapshot.build(store, t, AS_OF)
        (out_dir / f"{t}.json").write_text(json.dumps(snap.as_dict(), indent=2, default=str))
        kci = seed.get(f"us-{t.lower()}", {})
        for field in ("revenue", "ebitda", "netDebt", "ev"):
            live = snap.values.get(field)
            lv = live.value if live is not None else None
            kv = kci.get(field)
            diff = (lv / kv - 1) if lv is not None and kv else None
            rows.append((t, field, lv, kv, diff, live.period_end if live else None))
        print(f"{t}: blocked={snap.blocked} {snap.blocked_by}")
        if t in CHECK:
            field, want, tol = CHECK[t]
            got = snap.values[field].value
            good = got is not None and abs(got / want - 1) <= tol
            print(
                f"CHECK {t} {field}: live {got} vs KCI {want} (tolerance {tol:.1%}) -> {'PASS' if good else 'FAIL'}"
            )
            ok = ok and good
    print(f"\n{'ticker':6} {'field':8} {'live':>12} {'KCI':>12} {'diff':>8}  period end")
    for t, f, lv, kv, d, pe in rows:
        ls = f"{lv:,.1f}" if lv is not None else "—"
        ks = f"{kv:,.1f}" if isinstance(kv, (int, float)) else "—"
        ds = f"{d:+.1%}" if d is not None else "—"
        print(f"{t:6} {f:8} {ls:>12} {ks:>12} {ds:>8}  {pe}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
