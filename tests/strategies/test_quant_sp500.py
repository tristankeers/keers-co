"""Point-in-time S&P 500 membership and the wider backtest, on a FICTIONAL page and fictional prices."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from keers.data.models import PricePoint
from keers.data.store import Store
from keers.strategies.quant import backtest, history, model, wide


def page(current: list[tuple[str, str, int]], changes: list[tuple[str, str, str, str, str]]) -> str:
    """A page shaped like Wikipedia's, with filler so the size checks pass."""
    cons = "".join(f"<tr><td><a>{t}</a></td><td>{n}</td><td>X</td><td>2000-01-01</td><td>{c:010d}</td></tr>"
                   for t, n, c in current)  # fmt: skip
    rows = "".join(f"<tr><td>{d}</td><td>{a}</td><td>{an}</td><td>{r}</td><td>{rn}</td><td>why</td></tr>"
                   for d, a, an, r, rn in changes)  # fmt: skip
    return (
        "<html><table id='constituents'><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th>"
        f"<th>Date added</th><th>CIK</th></tr>{cons}</table>"
        "<table class='wikitable'><tr><th rowspan='2'>Effective Date</th><th colspan='2'>Added</th><th colspan='2'>Removed</th>"
        f"<th rowspan='2'>Reason</th></tr><tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>"
        f"{rows}</table></html>"
    )


FILLER = [(f"F{i:03d}", f"Filler {i} Inc.", 1000 + i) for i in range(460)]
FILLER_CHANGES = [(f"January {1 + i % 28}, 2010", f"Q{i:03d}", f"Q {i}", "", "") for i in range(100)]


def membership() -> history.Membership:
    current = [*FILLER, ("NEWCO", "NewCo Inc.", 1), ("KEEP", "Keep Corp", 2), ("XYZ", "Xyz Holdings", 3)]
    q_removed = [(f"January {1 + i % 28}, 2009", "", "", f"Q{i:03d}", f"Q {i}") for i in range(100)]
    changes = [
        ("June 1, 2021", "NEWCO", "NewCo Inc.", "GONE", "Gone Away, Inc."),
        ("March 3, 2020", "XYZ", "Xyz Holdings", "XYZ", "Old Xyz Corp"),  # same ticker, another company
        *FILLER_CHANGES, *q_removed,
    ]  # fmt: skip
    return history.parse(page(current, changes), date(2026, 11, 2), "sha-page")


def test_membership_walks_back_through_changes() -> None:
    m = membership()
    assert "GONE" in m.members(date(2021, 5, 31)) and "NEWCO" not in m.members(date(2021, 5, 31))
    assert "NEWCO" in m.members(date(2021, 6, 1)) and "GONE" not in m.members(date(2021, 6, 1))
    assert "GONE" in m.ever(date(2020, 1, 1), date(2026, 1, 1))
    assert m.reused() == {"XYZ"}
    assert m.ciks["KEEP"] == 2 and m.names()["GONE"] == "Gone Away, Inc."
    again = history.Membership.from_dict(m.as_dict())
    assert again.members(date(2021, 5, 31)) == m.members(date(2021, 5, 31))


def test_parse_refuses_a_page_without_the_tables() -> None:
    with pytest.raises(history.ParseError):
        history.parse("<html><table id='other'></table></html>", date(2026, 1, 1))


def test_tables_without_ids_are_found_by_header_and_nested_tables_ignored() -> None:
    html = page(FILLER, FILLER_CHANGES).replace("id='constituents'", "class='x'")
    html = html.replace("<td>X</td>", "<td><table><tr><td>inner</td></tr></table>X</td>", 1)
    m = history.parse(html, date(2026, 1, 1))
    assert len(m.current) == 460 and len(m.changes) == 100


def test_rowspan_dates_carry_down() -> None:
    p = history._Tables()
    p.feed(
        "<table id='changes'><tr><td rowspan='2'>June 1, 2021</td><td>A</td></tr><tr><td>B</td></tr></table>"
    )
    assert [[c[1] for c in r] for r in p.tables["changes"]] == [["June 1, 2021", "A"], ["June 1, 2021", "B"]]


def test_resolve_checks_reused_tickers_and_matches_names() -> None:
    m = membership()
    sec_map = {"KEEP": (2, "KEEP CORP"), "NEWCO": (1, "NEWCO INC")}
    lookup = wide.parse_cik_lookup("GONE AWAY INC:0000000077:\nOTHER CO:0000000088:\n")
    plan = wide.resolve(m, sec_map, lookup, {"GONE", "NEWCO", "XYZ", "KEEP"})
    assert plan["GONE"]["cik"] == 77 and plan["GONE"]["basis"] == "name match"
    assert plan["XYZ"]["prices"] is False  # two companies under one ticker
    sec_map["GONE"] = (99, "SOMEONE ELSE INC")
    plan = wide.resolve(m, sec_map, lookup, {"GONE"})
    assert plan["GONE"]["prices"] is False and "now belongs to" in plan["GONE"]["note"]


def sessions(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


def test_backtest_ranks_only_members_on_each_signal_date(tmp_path: Path) -> None:
    s = Store(tmp_path / "w.sqlite")
    days = sessions(date(2019, 1, 1), date(2023, 12, 29))
    for k, t in enumerate(["SPY", "RSP", *[f"T{i:02d}" for i in range(12)]]):
        rng, px = random.Random(k), 40.0
        pts = []
        for d in days:
            if t == "T00" and d > date(2021, 8, 31):
                break  # delisted: no prices after August 2021
            px *= math.exp(0.0002 + rng.gauss(0, 0.01))
            pts.append(PricePoint(t, d, px, "yahoo", px, px, px, 1e6, "sha", px))
        s.put_prices(pts)
    early = frozenset(f"T{i:02d}" for i in range(6))
    late = frozenset(f"T{i:02d}" for i in range(1, 12))

    def members(d: date) -> frozenset[str]:
        return early if d < date(2021, 9, 1) else late

    tickers = sorted(early | late)
    cfg = model.Config.preset("v1")
    res = backtest.run(s, tickers, date(2020, 2, 1), date(2023, 12, 29), cfg, members=members,
                       extra_benchmarks=["RSP"])  # fmt: skip
    assert res["ran"] and "point in time" in res["warnings"][0]
    for mo in res["months"]:
        allowed = early if mo["signalDay"] < "2021-09-01" else late
        assert set(mo["held"]) <= allowed and mo["members"] == len(allowed)
    assert any("T00" in mo["held"] for mo in res["months"] if mo["signalDay"] < "2021-08-01")
    assert res["benchmarks"]["RSP"]["cagr"] is not None and res["universeCoverage"]["membersMax"] == 11
