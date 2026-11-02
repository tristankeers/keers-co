"""The research agents on the FICTIONAL company (Example Widgets, EXW): offline, with fake models, no network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from keers.agents import committee, guard, roles
from keers.agents import facts as F
from keers.agents.llm import Claude, LLMError, Reply
from keers.portfolio.ledger import Ledger
from keers.risk.limits import Limits
from keers.web.app import create_app
from keers.web.service import Workbench
from tests.web import demo

NOW = datetime(2025, 11, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return demo.build(tmp_path / "keers.sqlite")


@pytest.fixture(autouse=True)
def long_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under kci-v3 the demo company's DCF upside is 14.97%, just under the 15% compounder bar. These tests are
    # about the run's plumbing (desk, risk, memo), so the bar is lowered to keep a long case to run through it.
    monkeypatch.setattr(roles, "B_MIN_UPSIDE", 0.10)


@pytest.fixture
def wb(db: Path) -> Workbench:
    return Workbench(str(db), today=lambda: demo.AS_OF)


def go(wb: Workbench, strategy: str, llm: Any = None, **kw: Any) -> committee.Run:
    return committee.run(wb, "EXW", strategy, llm, "test", now=lambda: NOW, **kw)


class Fake:
    """A stand-in model: replies are taken in order, per role (worked out from the schema it is asked for)."""

    name = "fake-model"

    def __init__(self, **replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Reply:
        role = {"stance": "analyst", "verdict": "bear", "headline": "writer"}[
            next(iter(schema["properties"]))
        ]
        self.calls.append((role, user))
        r = self.replies[role].pop(0)
        if isinstance(r, Exception):
            raise r
        return Reply(r, "fake-model")


GOOD_CASE = {
    "stance": "long",
    "conviction": "high",
    "thesis": "The DCF value of {dcf.perShare} is above the price of {price}, a gap of {dcf.vsPrice}.",
    "points": [{"claim": "Margins are strong: quality score {score.quality}.", "facts": ["score.quality"]}],
    "risks": ["Most of the value is in the terminal value ({dcf.tvShare})."],
    "changeMyMind": "A weaker 10-K.",
}
GOOD_BEAR = {"verdict": "concerns", "objections": [], "missing": []}
GOOD_MEMO = {
    "headline": "Widgets at a discount",
    "summary": "A good business at {evEbitda}.",
    "discussion": "Fine.",
}


# --------------------------------------------------------------------------------------------------------------
# facts and the number guard
# --------------------------------------------------------------------------------------------------------------
def test_fact_sheet_reads_the_engines_with_sources(wb: Workbench) -> None:
    s = F.build(wb, "EXW")
    assert s.valuation_ran and not s.blocked_by
    assert s.facts["price"].value == 51.63 and s.facts["price"].label == "market"
    assert "sec.gov/Archives" in (s.facts["revenue"].link or "")
    assert s.facts["dcf.vsPrice"].label == "derived" and s.facts["distress.status"].value == "no flags"
    assert "kci-v3" in s.facts["lbo.base.irr"].source
    assert "{dcf.vsPrice} | DCF value per share against the last close | 15.0%" in s.table()


def test_guard_rejects_typed_numbers_and_unknown_keys(wb: Workbench) -> None:
    s = F.build(wb, "EXW")
    assert guard.check("EV is {ev}, per the 10-K and Q3 10-Q.", s) == []
    assert guard.check("EV is 2,781m.", s)[0].startswith("a number was typed")
    assert "quantity was written in words" in guard.check("Revenue rose twelve percent.", s)[0]
    assert guard.check("Price {nope}.", s) == ["unknown fact key {nope}"]
    assert "has no value" in guard.check("SBC was {sbc}.", s)[0]  # not filed in the fixture


def test_render_fills_values_and_numbers_sources_once(wb: Workbench) -> None:
    s = F.build(wb, "EXW")
    cited: list[str] = []
    a = guard.render("Price {price}, EV {ev}.", s, cited=cited)
    b = guard.render("Again {ev}.", s, cited=cited)
    assert a.text == "Price $51.63 [1], EV $2,781.5m [2]." and b.text == "Again $2,781.5m [2]."


# --------------------------------------------------------------------------------------------------------------
# a run without a model (rules-v1)
# --------------------------------------------------------------------------------------------------------------
def test_offline_run_previews_without_writing(wb: Workbench, tmp_path: Path) -> None:
    pid = wb.create_portfolio("P", 1_000_000)["id"]
    r = go(wb, "B", portfolio_id=pid, out_dir=tmp_path / "runs")
    assert r.analyst and r.analyst.source == "rules-v1" and r.analyst.data["stance"] == "long"
    assert r.decision["propose"] and r.decision["weight"] == committee.SIZING["low"]
    assert r.trade and r.trade["executed"] is False and r.trade["risk"]["approved"]
    assert wb.portfolio(pid)["orders"] == []  # a preview writes nothing
    rec = json.loads((tmp_path / "runs" / f"{r.id}.json").read_text())
    assert rec["decision"]["propose"] and rec["factsSha256"] == r.sheet.sha256()
    md = (tmp_path / "runs" / f"{r.id}.md").read_text()
    assert "Previewed on the paper desk (not placed)" in md and "## Sources" in md and "rules-v1" in md


def test_execute_goes_through_the_desk_and_risk(wb: Workbench) -> None:
    pid = wb.create_portfolio("P", 1_000_000)["id"]
    r = go(wb, "B", portfolio_id=pid, execute=True)
    assert r.trade and r.trade["filled"] and r.trade["order"]["note"] == f"agent run {r.id}"
    orders = Ledger(wb.paper_db).orders(pid)
    assert len(orders) == 1 and orders[0].status == "filled" and orders[0].risk["approved"]


def test_risk_limits_still_block_an_agent_trade(db: Path) -> None:
    wb = Workbench(str(db), today=lambda: demo.AS_OF, limits=Limits(max_position=0.005))
    pid = wb.create_portfolio("P", 1_000_000)["id"]
    r = go(wb, "B", portfolio_id=pid, execute=True)
    assert r.decision["propose"] and r.trade and not r.trade["filled"]
    assert Ledger(wb.paper_db).orders(pid)[0].status == "rejected"
    assert "Blocked by position.size" in r.memo


def test_strategy_c_and_stale_data_never_propose(db: Path, wb: Workbench) -> None:
    assert not go(wb, "C").decision["propose"]
    late = Workbench(str(db), today=lambda: demo.AS_OF + timedelta(days=30))
    r = committee.run(late, "EXW", "B", None, "test", now=lambda: NOW)
    assert not r.decision["propose"] and r.bear and r.bear.data["verdict"] == "block"


def test_bad_requests(wb: Workbench) -> None:
    with pytest.raises(committee.RunError):
        go(wb, "D")
    with pytest.raises(committee.RunError):
        go(wb, "B", execute=True)


# --------------------------------------------------------------------------------------------------------------
# with a (fake) model
# --------------------------------------------------------------------------------------------------------------
def test_model_reply_is_used_and_conviction_sets_size_in_code(wb: Workbench) -> None:
    llm = Fake(analyst=[GOOD_CASE], bear=[GOOD_BEAR], writer=[GOOD_MEMO])
    r = go(wb, "B", llm)
    assert r.analyst and r.analyst.source == "model fake-model"
    assert r.decision["weight"] == committee.SIZING["high"]
    assert "$59.36 [" in r.memo and "Widgets at a discount" in r.memo
    assert "{trade.weight}" in llm.calls[2][1]  # the writer sees the size as a fact key, not a number


def test_typed_numbers_get_one_retry_then_rules(wb: Workbench) -> None:
    bad = {**GOOD_CASE, "thesis": "Cheap at 9.8x EBITDA."}
    llm = Fake(analyst=[bad, GOOD_CASE], bear=[GOOD_BEAR], writer=[GOOD_MEMO])
    r = go(wb, "B", llm)
    assert r.analyst and r.analyst.source == "model fake-model" and "attempt 1 failed" in r.analyst.notes[0]
    assert "broke the house rules" in llm.calls[1][1]

    llm = Fake(analyst=[bad, bad], bear=[GOOD_BEAR], writer=[GOOD_MEMO])
    r = go(wb, "B", llm)
    assert r.analyst and r.analyst.source == "rules-v1" and r.analyst.data["conviction"] == "low"
    assert "9.8x" not in r.memo


def test_model_failure_falls_back_and_bear_block_stops_trade(wb: Workbench) -> None:
    llm = Fake(analyst=[GOOD_CASE], bear=[{"verdict": "block", "objections": [], "missing": []}],
               writer=[LLMError("network down")])  # fmt: skip
    r = go(wb, "B", llm)
    assert not r.decision["propose"] and "the bear reviewer blocked it" in r.decision["reasons"]
    assert r.writer and r.writer.source == "rules-v1" and "network down" in r.writer.notes[0]


def test_short_under_b_is_refused_in_code(wb: Workbench) -> None:
    llm = Fake(analyst=[{**GOOD_CASE, "stance": "short"}], bear=[GOOD_BEAR], writer=[GOOD_MEMO])
    assert "strategy B is long only" in go(wb, "B", llm).decision["reasons"]


def test_problems_checks_fact_lists_and_headline(wb: Workbench) -> None:
    s = F.build(wb, "EXW")
    case = {**GOOD_CASE, "points": [{"claim": "ok", "facts": ["made.up"]}]}
    assert "unknown fact key in a facts list: made.up" in roles.problems("analyst", case, s)
    assert "the headline must not contain figures" in roles.problems(
        "writer", {**GOOD_MEMO, "headline": "{ev}"}, s
    )


def test_claude_client_request_and_refusal() -> None:
    sent: dict[str, Any] = {}

    def create(**kw: Any) -> Any:
        sent.update(kw)
        return SimpleNamespace(stop_reason="end_turn", model="claude-x", usage=None,
                               content=[SimpleNamespace(type="text", text='{"a": 1}')])  # fmt: skip

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    reply = Claude("claude-x", "low", client).complete("sys", "user", {"type": "object"})
    assert reply.data == {"a": 1} and sent["model"] == "claude-x"
    assert (
        sent["output_config"]["format"]["type"] == "json_schema" and sent["output_config"]["effort"] == "low"
    )

    def refuse(**kw: Any) -> Any:
        return SimpleNamespace(stop_reason="refusal", content=[])

    client.beta.messages.create = refuse
    with pytest.raises(LLMError):
        Claude("claude-x", "low", client).complete("sys", "user", {})


# --------------------------------------------------------------------------------------------------------------
# the workbench route
# --------------------------------------------------------------------------------------------------------------
def test_research_route_offline(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    c = TestClient(create_app(Workbench(str(db), today=lambda: demo.AS_OF)))
    pid = c.post("/api/portfolios", json={"name": "P", "startingCash": 1_000_000}).json()["id"]
    r = c.post("/api/companies/EXW/research", json={"strategy": "B", "portfolio": pid}).json()
    assert r["decision"]["propose"] and r["llm"].startswith("none") and r["memo"].startswith("# ")
    assert (db.parent / "agent-runs" / f"{r['id']}.md").exists()
    assert c.post("/api/companies/EXW/research", json={"strategy": "Z"}).status_code == 422
