"""The workbench's paper-portfolio API on the fictional company (offline: prices come from the demo store)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keers.web.app import create_app
from keers.web.service import Workbench
from tests.web import demo


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db = demo.build(tmp_path / "keers.sqlite")
    return TestClient(create_app(Workbench(str(db), today=lambda: demo.AS_OF)))


def test_create_trade_view_and_close(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/portfolios").json()["portfolios"] == []
    p = client.post("/api/portfolios", json={"name": "Paper", "startingCash": 1_000_000}).json()
    assert (tmp_path / "paper.sqlite").exists()  # its own file, next to the store
    pid = p["id"]

    pv = client.post(f"/api/portfolios/{pid}/preview", json={"symbol": "EXW", "strategy": "B", "side": "buy",
                                                             "shares": 1000}).json()  # fmt: skip
    assert pv["mark"]["source"] == "yahoo close" and pv["mark"]["day"] == demo.LAST_CLOSE.isoformat()
    assert pv["mark"]["currency"] == "USD" and pv["mark"]["advShares"] > 0
    assert pv["sector"] and pv["sectorBasis"] == "sic"
    assert pv["risk"]["approved"], pv["risk"]["breaches"]
    assert client.get(f"/api/portfolios/{pid}").json()["orders"] == []

    t = client.post(f"/api/portfolios/{pid}/trades", json={"symbol": "EXW", "strategy": "B", "side": "buy",
                                                           "shares": 1000, "sector": "Industrials"}).json()  # fmt: skip
    assert t["filled"] and t["order"]["sector"] == "Industrials" and t["order"]["sector_basis"] == "entered"

    v = client.get(f"/api/portfolios/{pid}").json()
    assert v["positions"][0]["quantity"] == 1000 and v["positions"][0]["strategy"] == "B"
    assert v["summary"]["nav"] is not None and v["risk"]["approved"]
    assert "Paper trading only" in v["notice"]

    c = client.post(f"/api/portfolios/{pid}/trades", json={"symbol": "EXW", "strategy": "B", "side": "sell",
                                                           "close": True}).json()  # fmt: skip
    assert c["filled"] and c["quantity"] == -1000


def test_blocked_trade_is_reported_not_filled(client: TestClient) -> None:
    pid = client.post("/api/portfolios", json={"name": "P", "startingCash": 100_000}).json()["id"]
    r = client.post(f"/api/portfolios/{pid}/trades", json={"symbol": "EXW", "strategy": "A", "side": "buy",
                                                           "weight": 0.5}).json()  # fmt: skip
    assert not r["filled"] and not r["risk"]["approved"]
    assert {b["name"] for b in r["risk"]["breaches"]} >= {"position.size"}
    v = client.get(f"/api/portfolios/{pid}").json()
    assert v["orders"][0]["status"] == "rejected" and v["positions"] == []


def test_bad_requests_are_422(client: TestClient) -> None:
    assert client.post("/api/portfolios", json={"name": "", "startingCash": 10}).status_code == 422
    pid = client.post("/api/portfolios", json={"name": "P", "startingCash": 100_000}).json()["id"]
    for body in ({"symbol": "EXW", "strategy": "Z", "shares": 1}, {"symbol": "EXW", "strategy": "A"},
                 {"symbol": "EXW", "strategy": "A", "shares": 1, "weight": 0.1}):  # fmt: skip
        assert client.post(f"/api/portfolios/{pid}/preview", json=body).status_code == 422
    assert client.get("/api/portfolios/999").status_code == 422
