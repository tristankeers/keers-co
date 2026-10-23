"""The research run: fact sheet, analyst, bear reviewer, a decision in code, a paper trade through the desk, memo.

1. ``facts.build`` reads the deterministic views into a fact sheet.
2. The analyst writes the case; the bear reviewer attacks it. Each reply must pass the number guard (figures only
   as fact keys) or it is sent back once; if it still fails, or no model is configured, ``rules-v1`` stands in.
3. Code decides whether to propose a trade (``decide``) and sizes it (``SIZING``). The models never pick a size.
4. The proposal goes to the paper desk: always a preview through ``keers.risk``; with ``execute`` it is placed
   with ``Desk.execute`` (through ``Workbench.paper_trade``), which checks the risk limits again and fills only
   if every check passes. Nothing here can bypass the risk limits or reach a broker.
5. The IC memo writer writes the memo around the decision; code adds the figures table, the risk result and the
   sources. The whole run (facts, prompts, replies, decision, trade) is saved as JSON next to the store.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from keers.risk.limits import STRATEGIES, STRATEGY_NAMES

from . import facts as F
from . import guard, roles
from .llm import LLM, LLMError

log = logging.getLogger("keers.agents")

SIZING_VERSION = "sizing-v1"
SIZING = {"low": 0.01, "medium": 0.02, "high": 0.03}  # share of NAV for a new position, by analyst conviction
SIDE = {"long": "buy", "short": "sell"}


class Desk(Protocol):
    """What the run needs from the paper desk. ``keers.web.service.Workbench`` provides it."""

    def company(self, symbol: str) -> dict[str, Any]: ...
    def valuation(self, symbol: str) -> dict[str, Any]: ...
    def distress(self, symbol: str) -> dict[str, Any]: ...
    def paper_trade(self, pid: int, body: Mapping[str, Any], execute: bool) -> dict[str, Any]: ...


class RunError(ValueError):
    """The request itself is invalid."""


@dataclass
class Run:
    id: str
    symbol: str
    strategy: str
    created_at: str
    llm: str
    sheet: F.FactSheet
    analyst: roles.Output | None = None
    bear: roles.Output | None = None
    writer: roles.Output | None = None
    decision: dict[str, Any] = field(default_factory=dict)
    trade: dict[str, Any] | None = None
    memo: str = ""
    cited: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "symbol": self.symbol, "strategy": self.strategy, "createdAt": self.created_at,
            "promptVersion": roles.PROMPT_VERSION, "rulesVersion": roles.RULES_VERSION, "sizingVersion": SIZING_VERSION,
            "llm": self.llm, "facts": self.sheet.as_dict(), "factsSha256": self.sheet.sha256(),
            "analyst": self.analyst.as_dict() if self.analyst else None,
            "bear": self.bear.as_dict() if self.bear else None,
            "writer": self.writer.as_dict() if self.writer else None,
            "decision": self.decision, "trade": self.trade, "memo": self.memo, "cited": self.cited,
        }  # fmt: skip


def _ask(llm: LLM | None, why_none: str, role: str, prompt: tuple[str, str], schema: dict[str, Any],
         sheet: F.FactSheet, fallback: Callable[[], dict[str, Any]]) -> roles.Output:  # fmt: skip
    """One agent turn: the model's reply if it passes the checks (one retry with the problems), else the rules."""
    if llm is None:
        return roles.Output(role, fallback(), roles.RULES_VERSION, [f"no model: {why_none}"])
    system, user = prompt
    raw: list[dict[str, Any]] = []
    notes: list[str] = []
    for attempt in (1, 2):
        try:
            reply = llm.complete(system, user, schema)
        except LLMError as e:
            notes.append(str(e))
            break
        raw.append({"attempt": attempt, "model": reply.model, "usage": reply.usage, "system": system, "user": user,
                    "reply": reply.data})  # fmt: skip
        found = roles.problems(role, reply.data, sheet)
        if not found:
            return roles.Output(role, roles.trim(reply.data), f"model {reply.model}", notes, raw)
        notes.append(f"attempt {attempt} failed the checks: " + "; ".join(found))
        user = (user + "\n\nYour previous answer broke the house rules:\n" + "\n".join(f"- {p}" for p in found)
                + "\nAnswer again, writing every figure as a fact key in braces and nothing else numeric.")  # fmt: skip
    notes.append(f"used {roles.RULES_VERSION} instead of the model's answer")
    return roles.Output(role, fallback(), roles.RULES_VERSION, notes, raw)


def decide(
    sheet: F.FactSheet, strategy: str, case: Mapping[str, Any], bear: Mapping[str, Any]
) -> dict[str, Any]:
    """Whether to propose a paper trade. Deterministic: the agents' labels plus the data gates, nothing else."""
    reasons: list[str] = []
    if sheet.blocked_by:
        reasons.append("the figures are blocked by data problems")
    if any(f.stale for f in sheet.facts.values()):
        reasons.append("some figures are stale")
    if not sheet.valuation_ran:
        reasons.append("the valuation did not run")
    if strategy == "C":
        reasons.append(
            "strategy C trades come from its systematic signals (keers.strategies.quant), not from a single-company case"
        )
    if case["stance"] == "no_trade":
        reasons.append("the analyst found no case")
    if strategy == "B" and case["stance"] == "short":
        reasons.append("strategy B is long only")
    if bear["verdict"] == "block":
        reasons.append("the bear reviewer blocked it")
    if reasons:
        return {"propose": False, "reasons": reasons, "headline": "no trade",
                "text": "No trade is proposed, because " + "; ".join(reasons) + "."}  # fmt: skip
    weight = SIZING[case["conviction"]]
    side = SIDE[case["stance"]]
    sheet.add(F.Fact("trade.weight", "Proposed position size (share of NAV)", weight, "ratio", "assumption",
                     f"{SIZING_VERSION}: {case['conviction']} conviction", f"{SIZING_VERSION} sizing rule"))  # fmt: skip
    caveat = " The bear reviewer raised concerns, set out below." if bear["verdict"] == "concerns" else ""
    return {"propose": True, "reasons": [], "side": side, "weight": weight, "conviction": case["conviction"],
            "headline": f"proposed paper {'buy' if side == 'buy' else 'short sale'}",
            "text": (f"A paper {'buy' if side == 'buy' else 'short sale'} of {{trade.weight}} of NAV is proposed "
                     f"({SIZING_VERSION}, {case['conviction']} conviction), subject to the risk limits.{caveat}")}  # fmt: skip


def run(wb: Desk, symbol: str, strategy: str, llm: LLM | None, why_no_llm: str = "", portfolio_id: int | None = None,
        execute: bool = False, out_dir: Path | None = None, now: Callable[[], datetime] | None = None) -> Run:  # fmt: skip
    strategy = strategy.strip().upper()
    if strategy not in STRATEGIES:
        raise RunError(f"choose a strategy: {', '.join(STRATEGIES)}")
    if execute and portfolio_id is None:
        raise RunError("placing a paper trade needs a portfolio")
    t = (now or (lambda: datetime.now(UTC)))()
    sheet = F.build(wb, symbol)
    rid = f"{t.strftime('%Y%m%dT%H%M%SZ')}-{re.sub(r'[^A-Za-z0-9]+', '_', sheet.symbol)}-{strategy}"
    r = Run(rid, sheet.symbol, strategy, t.isoformat(timespec="seconds"), llm.name if llm else f"none ({why_no_llm})",
            sheet)  # fmt: skip

    r.analyst = _ask(llm, why_no_llm, "analyst", roles.analyst_prompt(sheet, strategy), roles.ANALYST_SCHEMA, sheet,
                     lambda: roles.rules_analyst(sheet, strategy))  # fmt: skip
    case = r.analyst.data
    r.bear = _ask(llm, why_no_llm, "bear", roles.bear_prompt(sheet, strategy, case), roles.BEAR_SCHEMA, sheet,
                  lambda: roles.rules_bear(sheet, strategy, case))  # fmt: skip
    bear = r.bear.data
    r.decision = decide(sheet, strategy, case, bear)

    if r.decision["propose"] and portfolio_id is not None:
        body = {"symbol": sheet.symbol, "strategy": strategy, "side": r.decision["side"],
                "weight": r.decision["weight"], "note": f"agent run {rid}"}  # fmt: skip
        try:
            result = wb.paper_trade(portfolio_id, body, execute=execute)
            r.trade = {"request": body, "executed": execute, **result}
        except ValueError as e:  # an invalid request (no price, unknown portfolio): recorded, not raised
            r.trade = {"request": body, "executed": False, "error": str(e)}
        _trade_facts(sheet, r.trade)

    r.writer = _ask(llm, why_no_llm, "writer", roles.writer_prompt(sheet, strategy, case, bear, r.decision),
                    roles.WRITER_SCHEMA, sheet,
                    lambda: roles.rules_writer(sheet, strategy, case, bear, r.decision))  # fmt: skip
    r.memo = memo(r)
    if out_dir is not None:
        save(r, out_dir)
    return r


def _trade_facts(sheet: F.FactSheet, trade: Mapping[str, Any]) -> None:
    fill = trade.get("fill") or {}
    if trade.get("quantity") is not None:
        sheet.add(F.Fact("trade.shares", "Proposed shares (negative is a sale)", trade["quantity"], "shares",
                         "derived", f"{SIZING_VERSION} weight × NAV ÷ fill price, whole shares", "paper desk"))  # fmt: skip
    if fill.get("price") is not None:
        sheet.add(F.Fact("trade.fillPrice", "Simulated fill price", fill["price"], "USD", "derived",
                         "latest price moved against the trade by the slippage margin", "paper desk, costs-v1"))  # fmt: skip


# --------------------------------------------------------------------------------------------------------------
# The memo (Markdown)
# --------------------------------------------------------------------------------------------------------------
MEMO_FACTS = ("price", "marketCap", "ev", "revenue", "revenueGrowth", "ebitda", "netIncome", "netDebt", "evEbitda",
              "leverage", "dcf.perShare", "dcf.vsPrice", "dcf.wacc", "dcf.tvShare", "lbo.base.irr",
              "lbo.downside.moic", "score.total", "distress.status", "credit.backdrop")  # fmt: skip


def memo(r: Run) -> str:
    assert r.analyst and r.bear and r.writer  # noqa: S101
    sheet, cited = r.sheet, r.cited
    case, bear, w = r.analyst.data, r.bear.data, r.writer.data

    def t(text: str) -> str:
        return guard.render(text, sheet, cited=cited).text

    name = STRATEGY_NAMES.get(r.strategy, r.strategy)
    lines = [
        f"# {w['headline']}",
        "",
        f"**{sheet.name} ({sheet.symbol})** · Strategy {r.strategy}, {name} · facts as of {sheet.as_of or 'unknown'}"
        f" · run `{r.id}`",
        "",
        "> Research memo for a paper portfolio. Not investment advice. Every figure below was produced by tested "
        "code and is numbered to its source; the agents chose words, not numbers.",
        "",
        "## Summary",
        "",
        t(w["summary"]),
        "",
        "## Decision",
        "",
        t(r.decision["text"]),
    ]
    if r.decision.get("reasons"):
        lines += [""] + [f"- {x}" for x in r.decision["reasons"]]
    lines += ["", *trade_lines(r, t), "", "## Discussion", "", t(w["discussion"]), "",
              f"## Analyst's case ({r.analyst.source})", "",
              f"Stance: **{case['stance'].replace('_', ' ')}**, conviction: **{case['conviction']}**.", "",
              t(case["thesis"]), ""]  # fmt: skip
    lines += [f"- {t(p['claim'])}" for p in case["points"]]
    if case["risks"]:
        lines += ["", "Risks the analyst sees:", ""] + [f"- {t(x)}" for x in case["risks"]]
    lines += ["", f"What would change the analyst's mind: {t(case['changeMyMind'])}", "",
              f"## Bear review ({r.bear.source})", "", f"Verdict: **{bear['verdict']}**.", ""]  # fmt: skip
    lines += [f"- *{o['severity']}*: {t(o['claim'])}" for o in bear["objections"]] or ["- No objections."]
    if bear["missing"]:
        lines += ["", "Missing information:", ""] + [f"- {t(x)}" for x in bear["missing"]]
    lines += ["", "## Key figures", "", "| Figure | Value | Label |", "|---|---|---|"]
    for k in MEMO_FACTS:
        f = sheet.facts.get(k)
        if f is not None and f.known:
            if k not in cited:
                cited.append(k)
            lines.append(f"| {f.title} | {f.formatted()}{' (stale)' if f.stale else ''} [{cited.index(k) + 1}] "
                         f"| {f.label} |")  # fmt: skip
    if sheet.blocked_by or sheet.warnings:
        lines += ["", "## Data notes", ""] + [f"- Blocked: {b}" for b in sheet.blocked_by] + [
            f"- {x}" for x in sheet.warnings]  # fmt: skip
    # The checks' details quote the rejected text, which may hold typed numbers; they stay in the JSON record only.
    notes = [
        f"- {o.role}: {n}"
        for o in (r.analyst, r.bear, r.writer)
        for n in o.notes
        if not n.startswith("attempt")
    ]
    notes += [f"- {o.role}: the model's reply failed the number checks {sum(n.startswith('attempt') for n in o.notes)}"
              " time(s); details in the run record" for o in (r.analyst, r.bear, r.writer)
              if any(n.startswith("attempt") for n in o.notes)]  # fmt: skip
    if notes:
        lines += ["", "## How this memo was made", ""] + notes
    lines += ["", "## Sources", ""]
    for i, k in enumerate(cited, 1):
        f = sheet.facts[k]
        src = f"[{f.source}]({f.link})" if f.link else f.source
        lines.append(f"{i}. **{f.title}**: {f.formatted().rstrip('.')}. {f.label}; {f.method}. Source: {src}")
    return "\n".join(lines).rstrip() + "\n"


def trade_lines(r: Run, t: Callable[[str], str]) -> list[str]:
    tr = r.trade
    if not r.decision.get("propose"):
        return []
    if tr is None:
        return ["No portfolio was given, so the proposal was not sent to the paper desk or the risk checks."]
    if tr.get("error"):
        return [f"The paper desk refused the request: {tr['error']}"]
    risk = tr.get("risk") or {}
    head = "Placed on the paper desk" if tr.get("executed") else "Previewed on the paper desk (not placed)"
    status = ("filled" if tr.get("filled") else "rejected") if tr.get("executed") else (
        "would pass the risk limits" if risk.get("approved") else "would be blocked by the risk limits")  # fmt: skip
    out = [f"{head}: {t('{trade.shares}')} at {t('{trade.fillPrice}')}, {status} "
           f"(risk limits {risk.get('limitsVersion') or 'unknown version'})."]  # fmt: skip
    for b in risk.get("breaches") or []:
        out.append(f"- Blocked by {b.get('name')}: {b.get('detail')}")
    for n in risk.get("missing") or []:
        out.append(f"- Check missing: {n}")
    return out


def save(r: Run, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{r.id}.json"
    path.write_text(json.dumps(r.as_dict(), indent=1, default=str))
    (out_dir / f"{r.id}.md").write_text(r.memo)
    return path
