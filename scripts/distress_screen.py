"""Run the Strategy A distress screen on live data: refresh, screen, print a table, save JSON and Markdown.

Usage: python scripts/distress_screen.py [--as-of YYYY-MM-DD] [--universe FILE | TICKER ...]
Needs SEC_USER_AGENT. Prices come from Yahoo, with TIINGO_TOKEN as the fallback. Run in GitHub Actions (the build sandbox cannot reach sec.gov).
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
from keers.data.refresh import read_universe, refresh, user_agent
from keers.data.store import Store
from keers.strategies import distress

DEFAULT_UNIVERSE = "config/universe/distress-test.txt"


def fmt(c: dict[str, Any]) -> str:
    v = c["value"]
    if c["level"] == "unknown":
        return "?"
    if v is None:
        return c["level"]
    if c["name"] in ("debtDue", "drawdown"):
        return f"{v:.0%}"
    return f"{v:.2f}" if c["name"] == "altmanZ" else f"{v:.1f}x"


def markdown(res: dict[str, Any], failed: dict[str, str]) -> str:
    names = ["interestCover", "netLeverage", "debtDue", "altmanZ", "drawdown"]
    head = [
        "Company",
        "Status",
        "Interest cover",
        "Net debt / EBITDA",
        "Debt due / cash",
        "Altman Z",
        "From 52w high",
    ]
    mark = {"severe": "🔴 ", "watch": "🟡 ", "clear": "", "unknown": ""}
    lines = [f"# Distress screen, {res['asOf']} ({res['screenVersion']})", "",
             f"Credit backdrop: {res['credit'].get('reason')}", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]  # fmt: skip
    for r in res["companies"]:
        cs = {c["name"]: c for c in r["checks"]}
        cells = [mark[cs[n]["level"]] + fmt(cs[n]) if n in cs else "—" for n in names]
        lines.append(f"| {r['ticker']} | {r['status']} | " + " | ".join(cells) + " |")
    lines += ["", "🔴 severe, 🟡 watch, ? unknown (never counted as clear).", ""]
    for r in res["companies"]:
        lines.append(f"## {r['ticker']}: {r['status']}")
        if r["status"] == "blocked":
            lines += [f"- blocked: {b}" for b in r["blockedBy"]]
        for c in r["checks"]:
            lines.append(f"- **{c['name']}** ({c['level']}): {c['reason']}. Thresholds: {c['thresholds']}.")
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
    ok = [t for t in tickers if t not in res_r["failed"]]
    res = distress.run(store, ok, a.as_of)
    (out / "distress.json").write_text(json.dumps(res, indent=2, default=str))
    md = markdown(res, {k: v for k, v in res_r["failed"].items()})
    (out / "distress.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
