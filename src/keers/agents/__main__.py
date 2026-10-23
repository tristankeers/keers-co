"""Command line: ``python -m keers.agents run DBX --strategy B [--portfolio 1 [--execute]]``.

Without ``ANTHROPIC_API_KEY`` the agents use their stated rules (rules-v1) and the memo says so. The memo is
printed and saved, with the full record of the run, under ``agent-runs/`` next to the store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from keers.data.refresh import DEFAULT_DB
    from keers.web.app import workbench_from_env

    from . import committee
    from .llm import from_env

    ap = argparse.ArgumentParser(prog="python -m keers.agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="research one company under one strategy and write an IC memo")
    r.add_argument("symbol")
    r.add_argument("--strategy", required=True, choices=["A", "B", "C"])
    r.add_argument("--portfolio", type=int, help="paper portfolio id: preview the proposed trade against it")
    r.add_argument(
        "--execute", action="store_true", help="place the proposed paper trade (risk limits still apply)"
    )
    r.add_argument("--offline-model", action="store_true", help="use the rules even if an API key is set")
    r.add_argument("--json", action="store_true", help="print the full run record instead of the memo")
    a = ap.parse_args(argv)

    db = os.environ.get("KEERS_DB", DEFAULT_DB)
    wb = workbench_from_env()  # KEERS_OFFLINE=1 reads the store only
    if wb.live is not None:  # bring filings and prices up to date first (the views read the store)
        try:
            wb.refresh(a.symbol)
        except Exception as e:  # noqa: BLE001 - a failed refresh leaves stored data, which the memo labels stale
            print(f"refresh failed, using stored data: {e}", file=sys.stderr)
    llm, why = (None, "--offline-model given") if a.offline_model else from_env()
    try:
        run = committee.run(wb, a.symbol, a.strategy, llm, why, a.portfolio, a.execute,
                            out_dir=Path(db).resolve().parent / "agent-runs")  # fmt: skip
    except committee.RunError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(run.as_dict(), indent=1, default=str) if a.json else run.memo)
    print(f"saved: agent-runs/{run.id}.md and .json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
