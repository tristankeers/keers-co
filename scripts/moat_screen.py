"""Run the Strategy B moat screen on live data: refresh, screen, print a table, save JSON and Markdown.

Usage: python scripts/moat_screen.py [--as-of YYYY-MM-DD] [--universe FILE | TICKER ...]
Needs SEC_USER_AGENT. Prices come from Yahoo, with TIINGO_TOKEN as the fallback. Run in GitHub Actions (the build
sandbox cannot reach sec.gov).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from keers.data.http import Fetcher
from keers.data.refresh import price_report, read_universe, refresh, user_agent
from keers.data.store import Store
from keers.strategies import moat

DEFAULT_UNIVERSE = "config/universe/moat-test.txt"
NAMES = ["roic", "roicConsistency", "marginStability", "growth", "cashConversion", "balanceSheet", "dilution"]
HEAD = ["Median ROIC", "Years ROIC ≥10%", "Op. margin s.d.", "Revenue CAGR", "FCF / NOPAT", "Net debt / EBITDA",
        "Share change / yr"]  # fmt: skip


def fmt(c: dict[str, Any]) -> str:
    v = c["value"]
    if c["level"] == "unknown" or v is None:
        return "?" if c["level"] == "unknown" else c["level"]
    if c["name"] == "balanceSheet":
        return f"{v:.1f}x"
    if c["name"] == "marginStability":
        return f"{v * 100:.1f}pp"
    return f"{v:.0%}" if c["name"] == "roicConsistency" else f"{v:.1%}"


def markdown(res: dict[str, Any], failed: dict[str, str]) -> str:
    mark = {"fail": "🔴 ", "watch": "🟡 ", "pass": "", "unknown": ""}
    lines = [f"# Moat screen, {res['asOf']} ({res['screenVersion']})", "",
             "| Company | Status | Years | " + " | ".join(HEAD) + " | Earnings yield |",
             "|" + "---|" * (len(HEAD) + 4)]  # fmt: skip
    for r in res["companies"]:
        cs = {c["name"]: c for c in r["checks"]}
        cells = [mark[cs[n]["level"]] + fmt(cs[n]) if n in cs else "—" for n in NAMES]
        ey = (r.get("price") or {}).get("earningsYield")
        lines.append(f"| {r['ticker']} | {r['status']} | {r.get('window', '—')} | " + " | ".join(cells)
                     + f" | {f'{ey:.1%}' if ey is not None else '—'} |")  # fmt: skip
    lines += [
        "",
        "🔴 fail, 🟡 watch, ? unknown (never counted as a pass). Earnings yield is shown, not graded.",
        "",
    ]
    for r in res["companies"]:
        lines.append(f"## {r['ticker']}: {r['status']}")
        if r.get("blockedBy"):
            lines += [f"- blocked: {b}" for b in r["blockedBy"]]
        if r.get("reason"):
            lines.append(f"- {r['reason']}")
        for c in r["checks"]:
            lines.append(f"- **{c['name']}** ({c['level']}): {c['reason']}. Thresholds: {c['thresholds']}.")
        notes = r.get("dataWarnings") or []
        if notes:
            lines.append("- data notes: " + "; ".join(notes))
        lines.append("")
    if failed:
        lines += ["## Not refreshed", "", *[f"- {k}: {v}" for k, v in failed.items()], ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--universe", default=None)
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    a = ap.parse_args()
    tickers = [t.upper() for t in a.tickers] or read_universe(a.universe or DEFAULT_UNIVERSE)
    bad = [t for t in tickers if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t)]
    if bad:
        ap.error(f"not tickers: {bad}")
    out = Path("screen-out")
    out.mkdir(exist_ok=True)
    store = Store(out / "screen.sqlite", keep_bodies=False)
    res_r = refresh(
        store, tickers, Fetcher(user_agent()), (os.environ.get("TIINGO_TOKEN") or "").strip() or None
    )
    print("refresh:", json.dumps({"status": res_r["status"], "failed": res_r["failed"]}, indent=2))
    print("prices:", json.dumps(price_report(res_r), indent=2))
    ok = [t for t in tickers if t not in res_r["failed"]]
    res = moat.run(store, ok, a.as_of)
    (out / "moat.json").write_text(json.dumps(res, indent=2, default=str))
    md = markdown(res, dict(res_r["failed"].items()))
    (out / "moat.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
