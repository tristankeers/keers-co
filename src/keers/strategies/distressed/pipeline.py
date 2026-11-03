"""Strategy A in one run: screen, candidates, research agents and paper-desk checks, with a report.

1. The distress screen (``distress-v1``) runs on every ticker given.
2. ``candidates`` keeps the flagged companies that are big and liquid enough, ranks them and lists what changed
   since the previous run.
3. For the top candidates, the research agents (``keers.agents``) write an IC memo under strategy A. Each memo's
   figures come from the screen, the strategy A analysis and the valuation; the agents only choose words, a
   direction and a conviction.
4. Any trade the committee proposes is checked by the paper desk against the risk limits: previewed by default,
   placed on paper only when asked (``execute``) and only on a named portfolio. Nothing here reaches a broker.

The report (Markdown and JSON) lists every company screened and why it did or did not go further.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from keers.agents import committee
from keers.agents.llm import LLM
from keers.data.store import Store
from keers.strategies import distress

from . import candidates as C
from .analysis import ANALYSIS_VERSION, RECOVERY_MULTIPLES

PIPELINE_VERSION = "a-pipeline-v1"
DEFAULT_TOP = 10


class Bench(committee.Desk, Protocol):
    """What the run needs: the research views and paper desk (``keers.web.service.Workbench``) and its store."""

    def open_store(self) -> Store: ...


def _summary(r: committee.Run, memo_path: str | None) -> dict[str, Any]:
    assert r.analyst and r.bear  # noqa: S101
    tr = r.trade or {}
    risk = tr.get("risk") or {}
    trade = None
    if r.trade is not None:
        trade = {"executed": bool(tr.get("executed")), "filled": tr.get("filled"), "approved": risk.get("approved"),
                 "quantity": tr.get("quantity"), "limitsVersion": risk.get("limitsVersion"), "error": tr.get("error"),
                 "breaches": [f"{b.get('name')}: {b.get('detail')}" for b in risk.get("breaches") or []],
                 "warnings": [f"{w.get('name')}: {w.get('detail')}" for w in risk.get("warnings") or []]}  # fmt: skip
    return {"ticker": r.symbol, "runId": r.id, "stance": r.analyst.data["stance"],
            "conviction": r.analyst.data["conviction"], "analystSource": r.analyst.source,
            "bearVerdict": r.bear.data["verdict"], "propose": bool(r.decision.get("propose")),
            "decision": r.decision.get("headline"), "reasons": r.decision.get("reasons") or [],
            "weight": r.decision.get("weight"), "trade": trade, "memo": memo_path,
            "dataProblems": r.sheet.blocked_by}  # fmt: skip


def run(wb: Bench, tickers: Sequence[str], as_of: date, top: int = DEFAULT_TOP, llm: LLM | None = None,
        why_no_llm: str = "", portfolio_id: int | None = None, execute: bool = False,
        previous: Mapping[str, Any] | None = None, out_dir: Path | None = None) -> dict[str, Any]:  # fmt: skip
    if execute and portfolio_id is None:
        raise committee.RunError("placing paper trades needs a portfolio")
    st = wb.open_store()
    try:
        screen = distress.run(st, tickers, as_of)
        cands = C.build(st, screen, as_of, previous)
    finally:
        st.close()
    runs_dir = out_dir / "agent-runs" if out_dir is not None else None
    research = []
    for c in cands["candidates"][: max(0, top)]:
        r = committee.run(wb, c["ticker"], "A", llm, why_no_llm, portfolio_id, execute, out_dir=runs_dir)
        research.append(_summary(r, f"agent-runs/{r.id}.md" if runs_dir is not None else None))
    res = {"asOf": as_of.isoformat(), "pipelineVersion": PIPELINE_VERSION, "analysisVersion": ANALYSIS_VERSION,
           "recoveryMultiples": dict(RECOVERY_MULTIPLES), "llm": llm.name if llm else f"none ({why_no_llm})",
           "portfolio": portfolio_id, "executed": execute, "top": top, "credit": screen["credit"],
           "candidates": cands, "research": research, "screen": screen}  # fmt: skip
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "strategy-a.json").write_text(json.dumps(res, indent=1, default=str))
        (out_dir / "candidates.json").write_text(json.dumps(cands, indent=1, default=str))
        (out_dir / "strategy-a.md").write_text(report(res))
    return res


def _trade_cell(t: Mapping[str, Any] | None, propose: bool) -> str:
    if not propose:
        return "none"
    if t is None:
        return "not sent (no portfolio)"
    if t.get("error"):
        return f"refused: {t['error']}"
    if t.get("executed"):
        return "filled" if t.get("filled") else "rejected by risk"
    return "would pass risk" if t.get("approved") else "would be blocked by risk"


def report(res: Mapping[str, Any]) -> str:
    c = res["candidates"]
    mults = ", ".join(f"{k} {v:g}x" for k, v in res["recoveryMultiples"].items())
    lines = [f"# Strategy A research run, {res['asOf']} ({res['pipelineVersion']})", "",
             "> Research for paper portfolios only. Not investment advice. Every figure comes from tested code; "
             "being a candidate is not a finding.", "",
             f"- Screened: {c['screened']} compan{'y' if c['screened'] == 1 else 'ies'} ({c['screenVersion']}); candidates: {len(c['candidates'])}; "
             f"researched: {len(res['research'])} (top {res['top']}).",
             f"- Filters ({c['candidatesVersion']}): status {' or '.join(c['filters']['statuses'])}, market value at "
             f"least ${c['filters']['minMarketCapM']:,.0f}m, price at least ${c['filters']['minPrice']:,.2f}, traded "
             f"value at least ${c['filters']['minDollarAdvM']:,.1f}m a day.",
             f"- Recovery value ({res['analysisVersion']}): EV / EBITDA of {mults}, a generic distressed-sale "
             "assumption.",
             f"- Agents: {res['llm']}. Paper desk: "
             + (f"portfolio {res['portfolio']}, {'trades placed' if res['executed'] else 'previews only'}."
                if res["portfolio"] is not None else "no portfolio, so no trade checks."),
             f"- Credit backdrop: {res['credit'].get('reason')}.", ""]  # fmt: skip
    ch = c.get("changes")
    if ch:
        lines += [f"## Changes since {ch['since']}", "",
                  f"- New candidates: {', '.join(ch['new']) or 'none'}",
                  "- Dropped out: " + (", ".join(f"{d['ticker']} (now {d['now']})" for d in ch["dropped"]) or "none"),
                  "- Status changed: " + (", ".join(f"{s['ticker']} {s['from']} → {s['to']}"
                                                     for s in ch["statusChanged"]) or "none"), ""]  # fmt: skip
    lines += ["## Candidates", ""]
    lines += (
        C.table(c["candidates"]) if c["candidates"] else ["No company passed the screen and the filters."]
    )
    lines += ["", "## Research", ""]
    if res["research"]:
        lines += ["| Company | Analyst | Bear | Decision | Paper desk | Memo |", "|---|---|---|---|---|---|"]
        for r in res["research"]:
            memo = f"[memo]({r['memo']})" if r["memo"] else ""
            stance = r["stance"].replace("_", " ") + (
                "" if r["stance"] == "no_trade" else f", {r['conviction']}"
            )
            lines.append(f"| {r['ticker']} | {stance} ({r['analystSource']}) | {r['bearVerdict']} | {r['decision']} | "
                         f"{_trade_cell(r['trade'], r['propose'])} | {memo} |")  # fmt: skip
        lines.append("")
        for r in res["research"]:
            notes = [f"- {x}" for x in r["reasons"]]
            if r["trade"]:
                notes += [f"- Blocked by {b}" for b in r["trade"]["breaches"]]
                notes += [f"- Flagged: {w}" for w in r["trade"]["warnings"]]
            if notes:
                lines += [f"**{r['ticker']}**", "", *notes, ""]
    else:
        lines += ["No candidates to research.", ""]
    if c["excluded"]:
        lines += ["## Flagged but excluded", ""]
        lines += [
            f"- {x['ticker']} ({x['status']}): {'; '.join(x['excludedBecause'])}" for x in c["excluded"]
        ]
        lines.append("")
    if c["needsData"]:
        lines += ["## Needs data (incomplete, never counted as clear)", ""]
        lines += [f"- {x['ticker']}: unknown {', '.join(x['unknownChecks'])}" for x in c["needsData"]]
        lines.append("")
    if c["blocked"]:
        lines += ["## Blocked", ""]
        lines += [f"- {x['ticker']}: {'; '.join(x['blockedBy'])}" for x in c["blocked"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
