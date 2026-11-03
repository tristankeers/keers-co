"""Command line: ``python -m keers.strategies.distressed run [TICKER ...] [--universe FILE]``.

Refreshes filings and prices (needs ``SEC_USER_AGENT``; ``--no-refresh`` uses the store as it is), runs the screen,
picks candidates, has the research agents write a memo on each of the top ones, previews any proposed trade on a
paper portfolio, and writes ``strategy-a.md``, ``strategy-a.json`` and ``candidates.json`` to ``--out``.

Without ``--portfolio``, a fresh paper portfolio is made for the previews (``--paper-cash``). ``--execute`` places
the proposed trades on paper, and only on a named ``--portfolio``. Without ``ANTHROPIC_API_KEY`` (or with
``--offline-model``) the agents use their stated rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

DEFAULT_UNIVERSE = "config/universe/distress-test.txt"


def main(argv: list[str] | None = None) -> int:
    from keers.agents.llm import from_env
    from keers.data.refresh import read_universe
    from keers.web.service import Workbench

    from . import pipeline

    ap = argparse.ArgumentParser(prog="python -m keers.strategies.distressed")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="screen, candidates, research memos and paper-desk previews")
    r.add_argument("tickers", nargs="*")
    r.add_argument("--universe", default=None, help=f"file of tickers (default {DEFAULT_UNIVERSE})")
    r.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    r.add_argument("--top", type=int, default=pipeline.DEFAULT_TOP, help="candidates to research")
    r.add_argument("--out", default="strategy-a-out")
    r.add_argument("--previous", default=None, help="an earlier candidates.json, to list what changed")
    r.add_argument("--portfolio", type=int, default=None, help="paper portfolio id for the trade checks")
    r.add_argument("--paper-cash", type=float, default=1_000_000.0,
                   help="starting cash of the fresh preview portfolio made when --portfolio is not given")  # fmt: skip
    r.add_argument(
        "--execute", action="store_true", help="place the proposed paper trades (needs --portfolio)"
    )
    r.add_argument("--no-refresh", action="store_true", help="use the stored filings and prices as they are")
    r.add_argument("--offline-model", action="store_true", help="use the rules even if an API key is set")
    a = ap.parse_args(argv)

    tickers = [t.upper() for t in a.tickers] or read_universe(a.universe or DEFAULT_UNIVERSE)
    bad = [t for t in tickers if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t)]
    if bad:
        ap.error(f"not tickers: {bad}")
    if a.execute and a.portfolio is None:
        ap.error("--execute needs --portfolio: trades are placed only on a named paper portfolio")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    db = os.environ.get("KEERS_DB") or str(out / "strategy-a.sqlite")
    previous = None
    if a.previous and Path(a.previous).exists():
        previous = json.loads(Path(a.previous).read_text())

    if not a.no_refresh:
        from keers.data.http import Fetcher
        from keers.data.refresh import price_report, refresh, user_agent
        from keers.data.store import Store

        store = Store(db, keep_bodies=False)
        res = refresh(
            store, tickers, Fetcher(user_agent()), (os.environ.get("TIINGO_TOKEN") or "").strip() or None
        )
        store.close()
        print(
            "refresh:",
            json.dumps({"status": res["status"], "failed": res["failed"]}, indent=1),
            file=sys.stderr,
        )
        print("prices:", json.dumps(price_report(res), indent=1), file=sys.stderr)
        tickers = [t for t in tickers if t not in res["failed"]]

    as_of = a.as_of
    pid = a.portfolio
    # Previews without a named portfolio use a throwaway ledger in --out, so they never touch your own.
    paper = None if pid is not None else str(out / "preview-paper.sqlite")
    wb = Workbench(db, today=lambda: as_of, paper_db=paper)
    if pid is None:
        pid = int(wb.create_portfolio(f"Strategy A previews {as_of}", a.paper_cash)["id"])
    llm, why = (None, "--offline-model given") if a.offline_model else from_env()
    pipeline.run(wb, tickers, as_of, a.top, llm, why, pid, a.execute, previous, out)
    print((out / "strategy-a.md").read_text())
    print(f"saved: {out}/strategy-a.md, strategy-a.json, candidates.json and agent-runs/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
