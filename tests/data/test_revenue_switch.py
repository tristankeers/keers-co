"""A company that moves revenue to a new concept mid-year (ASC 606 in 2018): its 10-Qs carry the year to date under
the new concept while the prior fiscal year sits under the old one. In the S&P 500 backtest this blocked up to 233
companies in late 2018 ("revenue: no usable period"). The company and every number here are fictional.
"""

from __future__ import annotations

from datetime import date

import pytest

from keers.data import fundamentals as F
from keers.data import snapshot

from . import fixtures as fx
from .test_data_layer import load, with_market

NEW = "RevenueFromContractWithCustomerExcludingAssessedTax"
AS_OF = date(2025, 11, 10)


def switched(comparatives_new: bool, new_2024_9m: float = 700) -> list[fx.Row]:
    """The 2025 10-Qs tag 2025 revenue under NEW. Either the 2024 comparatives stay under Revenues and 2025 is
    tagged under both (as Honeywell did), or the comparatives are tagged under NEW as well (as Abbott did)."""
    rows = []
    for r in fx.base_rows():
        if r[1] == "Revenues" and r[6] in (fx.Q2_25[0], fx.Q3_25[0]):
            if r[3] and r[3].startswith("2025"):
                rows.append((r[0], NEW, *r[2:]))
                if not comparatives_new:
                    rows.append(r)
                continue
            if comparatives_new:
                start, end = r[3], r[4]
                value = new_2024_9m * fx.M if (start, end) == ("2024-01-01", "2024-09-30") else r[5]
                rows.append((r[0], NEW, r[2], start, end, value, *r[6:]))
                continue
        rows.append(r)
    if not comparatives_new:  # Honeywell's old concept was SalesRevenueNet, which ranks below the new one
        rows = [(r[0], "SalesRevenueNet", *r[2:]) if r[1] == "Revenues" else r for r in rows]
    return rows


@pytest.mark.parametrize("comparatives_new", [False, True])
def test_ttm_is_built_across_the_old_and_new_concepts(comparatives_new: bool) -> None:
    store = with_market(load(switched(comparatives_new)))
    v = F.Fundamentals(store, fx.CIK, AS_OF).flow(F.REVENUE, "revenue")
    assert v.value == pytest.approx(1000 + 800 - 700) and v.period_end == date(2025, 9, 30)
    old = "Revenues" if comparatives_new else "SalesRevenueNet"
    assert f"us-gaap:{old})" in v.method and NEW in v.method
    assert any("report the same value" in w for w in v.warnings)
    snap = snapshot.build(store, fx.TICKER, AS_OF)
    assert not snap.blocked, snap.blocked_by
    assert snap.values["ebit"].known  # every other figure lines up on the same period end


def test_concepts_that_disagree_are_never_mixed() -> None:
    # The new concept restates the 2024 comparative (690 against 700 under the old concept): a TTM taking the fiscal
    # year from one and the year to date from the other would mix bases, so revenue stays unknown.
    store = with_market(load(switched(True, new_2024_9m=690)))
    v = F.Fundamentals(store, fx.CIK, AS_OF).flow(F.REVENUE, "revenue")
    assert not v.known
    snap = snapshot.build(store, fx.TICKER, AS_OF)
    assert snap.blocked and any(b.startswith("missing revenue") for b in snap.blocked_by)


def test_concepts_with_no_period_in_common_are_never_mixed() -> None:
    # Only the 2025 year to date under the new concept, and nothing it shares with the old concept: no check is
    # possible, so no TTM.
    rows = [r for r in switched(True) if not (r[1] == NEW and r[3] and r[3].startswith("2024"))]
    rows = [r for r in rows if not (r[1] == "Revenues" and r[6] in (fx.Q2_25[0], fx.Q3_25[0]))]
    rows = [r for r in rows if not (r[1] == "Revenues" and r[3] == "2024-01-01" and r[4] == "2024-09-30")]
    rows.append(
        fx._dur("Revenues", "2024-01-01", "2024-09-30", 700, fx.Q3_24)
    )  # filed under the old concept only
    store = load(rows)
    assert F.concepts_agree(store.facts(fx.CIK, "Revenues", unit="USD", known_by=AS_OF),
                            store.facts(fx.CIK, NEW, unit="USD", known_by=AS_OF)) is None  # fmt: skip
    v = F.Fundamentals(store, fx.CIK, AS_OF).flow(F.REVENUE, "revenue")
    assert not v.known


def test_one_concept_with_a_full_history_is_unchanged() -> None:
    v = F.Fundamentals(load(), fx.CIK, AS_OF).flow(F.REVENUE, "revenue")
    assert v.value == pytest.approx(1100) and v.method.startswith("us-gaap:Revenues: TTM")
    assert not v.warnings
