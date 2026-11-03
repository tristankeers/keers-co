"""The agent scorecard on FICTIONAL prices and hand-written run records (nothing here is real)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keers.agents import scorecard as sc
from keers.web.app import create_app
from keers.web.service import Workbench
from tests.web import demo

D0 = date(2025, 1, 6)  # a Monday


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return demo.build(tmp_path / "keers.sqlite")


def sessions(n: int) -> list[date]:
    out, d = [], D0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = sessions(400)
# SPY rises 0.1% a session; UP rises 0.3%; DOWN falls 0.1%.
PX = {
    "SPY": [100 * 1.001**i for i in range(400)],
    "UP": [50 * 1.003**i for i in range(400)],
    "DOWN": [50 * 0.999**i for i in range(400)],
}


def prices(t: str, a: date, b: date) -> list[tuple[date, float | None]]:
    return [(d, p) for d, p in zip(DAYS, PX.get(t, []), strict=False) if a <= d <= b]


def run(sym: str, stance: str, conviction: str = "medium", strategy: str = "A", day: date = D0,
        bear: str = "proceed", propose: bool = True, source: str = "rules-v1", t: str = "10:00:00") -> dict:  # fmt: skip
    return {"id": f"{sym}-{day}", "symbol": sym, "strategy": strategy, "createdAt": f"{day.isoformat()}T{t}+00:00",
            "analyst": {"source": source, "data": {"stance": stance, "conviction": conviction}},
            "bear": {"data": {"verdict": bear}}, "decision": {"propose": propose}}  # fmt: skip


def test_graded_call_by_hand() -> None:
    today = DAYS[-1]
    r = sc.scorecard([run("UP", "long")], prices, today)
    c = r["calls"][0]
    h = sc.HORIZON_SESSIONS["A"]
    assert c["status"] == "graded" and c["entry"] == D0.isoformat() and c["through"] == DAYS[h].isoformat()
    assert c["stockReturn"] == pytest.approx(1.003**h - 1)
    assert c["excessReturn"] == pytest.approx(1.003**h - 1.001**h)
    assert r["analyst"]["hitRate"] == 1.0 and r["analyst"]["ic"] is None and "needs" in r["analyst"]["icNote"]


def test_short_hits_on_a_falling_share_and_long_misses() -> None:
    r = sc.scorecard([run("DOWN", "short"), run("DOWN", "long", day=DAYS[1])], prices, DAYS[-1])
    hits = [c["signedExcess"] > 0 for c in r["calls"]]
    assert hits == [True, False] and r["analyst"]["hitRate"] == 0.5
    assert r["analyst"]["payoff"] == pytest.approx(
        r["calls"][0]["signedExcess"] / -r["calls"][1]["signedExcess"]
    )


def test_pending_until_the_horizon_and_no_trade_is_not_directional() -> None:
    r = sc.scorecard(
        [run("UP", "long", strategy="B"), run("UP", "no_trade", propose=False)], prices, DAYS[100]
    )
    assert [c["status"] for c in r["calls"]] == ["pending", "pending"]
    assert (
        r["calls"][0]["sessions"] == 100 and r["analyst"]["graded"] == 0 and r["analyst"]["hitRate"] is None
    )
    later = sc.scorecard([run("UP", "no_trade", propose=False)], prices, DAYS[-1])
    assert later["analyst"]["graded"] == 1 and later["analyst"]["directional"] == 0


def test_no_price_is_reported_not_guessed() -> None:
    c = sc.scorecard([run("NOPE", "long")], prices, DAYS[-1])["calls"][0]
    assert c["status"] == "no price" and "excessReturn" not in c


def test_bear_split_and_committee_proposals() -> None:
    runs = [run("UP", "long", bear="proceed"), run("DOWN", "long", bear="block", propose=False, day=DAYS[2])]
    r = sc.scorecard(runs, prices, DAYS[-1])
    assert r["bear"]["letThrough"] == 1 and r["bear"]["objected"] == 1
    assert r["bear"]["meanSignedExcessObjected"] < 0 < r["bear"]["meanSignedExcessLetThrough"]
    assert r["committeeProposals"]["calls"] == 1


def test_ic_from_enough_calls() -> None:
    runs = []
    for i in range(sc.MIN_FOR_IC):
        sym, stance = ("UP", "long") if i % 2 else ("DOWN", "short")
        runs.append(run(sym, stance, conviction=("low", "high")[i % 2], day=DAYS[i]))
    r = sc.scorecard(runs, prices, DAYS[-1])["analyst"]
    assert r["graded"] == sc.MIN_FOR_IC and r["ic"] > 0.8 and r["icT"] > 3  # conviction lined up with returns


def test_load_runs_keeps_the_latest_run_per_company_strategy_and_day(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps(run("UP", "long", t="09:00:00")))
    (tmp_path / "b.json").write_text(json.dumps(run("UP", "short", t="11:00:00")))
    (tmp_path / "c.json").write_text("not json")
    (tmp_path / "d.json").write_text(json.dumps({"something": "else"}))
    runs = sc.load_runs(tmp_path)
    assert len(runs) == 1 and runs[0]["analyst"]["data"]["stance"] == "short"
    assert sc.load_runs(tmp_path / "missing") == []


def test_scorecard_route(db: Path) -> None:
    out = db.parent / "agent-runs"
    out.mkdir()
    (out / "x.json").write_text(json.dumps(run("EXW", "long", day=demo.AS_OF)))
    c = TestClient(create_app(Workbench(str(db), today=lambda: demo.AS_OF)))
    r = c.get("/api/agents/scorecard", params={"symbol": "EXW"}).json()
    assert r["version"] == sc.SCORECARD_VERSION and len(r["calls"]) == 1
    assert r["calls"][0]["status"] in ("pending", "no price")
    assert c.get("/api/agents/scorecard", params={"symbol": "ZZZ"}).json()["calls"] == []
