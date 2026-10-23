"""Valuation coverage across SEC filers: how many companies reach the valuation, and why the rest are blocked.

Takes every company in SEC's company_tickers_exchange.json listed on NYSE or Nasdaq (one listing per CIK), or a
random sample of them, refreshes its filings and prices, builds a snapshot as of today and runs kci-v3 on the
unblocked ones. Blocked companies are grouped by reason, with numbers and dates taken out so that the same gap at
different companies lands in one group.

Writes ``coverage-out/coverage.md`` (summary), ``coverage-out/coverage.json`` (every company) and prints the
summary. Nothing here changes a figure: it only reports what the data layer already decides.

Usage: python scripts/coverage.py [--sample N] [--seed S] [--tickers FILE] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from keers.data import snapshot  # noqa: E402
from keers.data.http import Fetcher  # noqa: E402
from keers.data.refresh import read_universe, refresh, user_agent  # noqa: E402
from keers.data.store import Store  # noqa: E402
from keers.universe.sources import SEC_TICKERS_EXCHANGE_URL, parse_sec_tickers_exchange  # noqa: E402
from keers.valuation import kci_v3_defaults as V3  # noqa: E402

EXCHANGES = {"NYSE", "Nasdaq"}
BATCH = 25


def universe(fetcher: Fetcher) -> list[str]:
    rows = parse_sec_tickers_exchange(json.loads(fetcher.get(SEC_TICKERS_EXCHANGE_URL).body))
    seen: set[int] = set()
    out = []
    for r in rows:  # SEC lists a company's main ticker first
        if r.exchange in EXCHANGES and r.cik not in seen:
            seen.add(r.cik)
            out.append(r.ticker)
    return out


def group(reason: str) -> str:
    """The reason with figures, dates and company-specific concept names taken out."""
    s = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", reason)
    s = re.sub(r"-?\d[\d,]*(\.\d+)?(m|x|%)?", "#", s)
    s = re.sub(r"\b[a-z]+:[A-Za-z]+\b", "<concept>", s)
    s = re.sub(r"\(latest is [^)]*\)", "(latest is …)", s)
    return s[:160]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="random sample size; 0 = every company")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tickers", help="a universe file instead of SEC's list")
    ap.add_argument("--out", default="coverage-out")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    fetcher = Fetcher(user_agent())
    tickers = read_universe(a.tickers) if a.tickers else universe(fetcher)
    if a.sample and a.sample < len(tickers):
        tickers = sorted(random.Random(a.seed).sample(tickers, a.sample))  # noqa: S311 - sampling, not security
    print(f"{len(tickers)} companies", flush=True)
    store = Store(out / "coverage.sqlite")
    today = date.today()
    failed: dict[str, str] = {}
    t0 = time.time()
    for i in range(0, len(tickers), BATCH):
        part = tickers[i : i + BATCH]
        res = refresh(store, part, fetcher, rates=(i == 0), today=today)
        failed.update({k: v for k, v in res["failed"].items() if not k.startswith("_")})
        print(f"  refreshed {i + len(part)}/{len(tickers)} ({time.time() - t0:.0f}s)", flush=True)

    rows: list[dict[str, Any]] = []
    for t in tickers:
        if t in failed:
            rows.append({"ticker": t, "status": "refresh failed", "reasons": [failed[t]]})
            continue
        try:
            snap = snapshot.build(store, t, today)
        except Exception as e:  # noqa: BLE001 - a crash is a finding to report, not a reason to stop
            rows.append({"ticker": t, "status": "snapshot error", "reasons": [f"{type(e).__name__}: {e}"]})
            continue
        if snap.blocked:
            rows.append({"ticker": t, "status": "blocked", "reasons": list(snap.blocked_by)})
            continue
        try:
            v = V3.analyse(snapshot.to_kci_deal(snap))
            dcf, lbo = v["dcf"].get("ok", False), v["scenarios"]["base"]["result"].get("ok", False)
            rows.append({"ticker": t, "status": "valued", "dcf": dcf, "lbo": lbo,
                         "reasons": ([] if dcf else [f"DCF: {e}" for e in v["dcf"].get("errors", [])])})  # fmt: skip
        except Exception as e:  # noqa: BLE001
            rows.append({"ticker": t, "status": "engine error", "reasons": [f"{type(e).__name__}: {e}"]})

    status = Counter(r["status"] for r in rows)
    groups: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["status"] != "valued":
            for reason in r["reasons"]:
                groups[f"{r['status']}: {group(reason)}"].append(r["ticker"])
    no_dcf = defaultdict(list)
    for r in rows:
        if r["status"] == "valued" and not r["dcf"]:
            for reason in r["reasons"]:
                no_dcf[group(reason)].append(r["ticker"])
    n = len(rows)
    lines = [
        f"# Valuation coverage, {today}",
        "",
        f"{n} companies ({'random sample of ' + str(a.sample) if a.sample else 'all'} NYSE and Nasdaq SEC filers).",
        "",
        "| Status | Companies | Share |",
        "|---|---:|---:|",
        *[f"| {k} | {c} | {c / n:.0%} |" for k, c in status.most_common()],
        f"| valued with a DCF | {sum(1 for r in rows if r.get('dcf'))} | "
        f"{sum(1 for r in rows if r.get('dcf')) / n:.0%} |",
        "",
        "## Why companies are blocked (a company can have several reasons)",
        "",
        "| Companies | Reason | Examples |",
        "|---:|---|---|",
        *[
            f"| {len(ts)} | {k} | {', '.join(ts[:6])} |"
            for k, ts in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ],  # fmt: skip
        "",
        "## Valued, but no DCF",
        "",
        "| Companies | Reason | Examples |",
        "|---:|---|---|",
        *[
            f"| {len(ts)} | {k} | {', '.join(ts[:6])} |"
            for k, ts in sorted(no_dcf.items(), key=lambda kv: -len(kv[1]))
        ],  # fmt: skip
        "",
    ]
    text = "\n".join(lines)
    (out / "coverage.md").write_text(text)
    (out / "coverage.json").write_text(json.dumps(rows, indent=1))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
