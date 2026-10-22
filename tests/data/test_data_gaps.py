"""Labels some companies use instead of the common ones, found in the distress screen's first live run."""

from __future__ import annotations

from datetime import date

import pytest

from keers.data import fundamentals as F
from keers.data import snapshot

from . import fixtures as fx
from .test_data_layer import load, with_market

AS_OF = date(2025, 11, 10)
AT = date(2025, 9, 30)
ENDS = (("2024-12-31", fx.K24), ("2025-06-30", fx.Q2_25), ("2025-09-30", fx.Q3_25))


def rows_without(prefix: str) -> list[fx.Row]:
    return [r for r in fx.base_rows() if not r[1].startswith(prefix)]


def interest_rows(concept: str, fy: float, ytd24: float, ytd25: float) -> list[fx.Row]:
    return [fx._dur(concept, "2024-01-01", "2024-12-31", fy, fx.K24),
            fx._dur(concept, "2024-01-01", "2024-09-30", ytd24, fx.Q3_25),
            fx._dur(concept, "2025-01-01", "2025-09-30", ytd25, fx.Q3_25)]  # fmt: skip


def test_cash_filed_as_plain_cash() -> None:  # American Airlines
    rows = rows_without("CashAndCashEquivalents") + [fx._inst("Cash", e, 170, f) for e, f in ENDS]
    snap = snapshot.build(with_market(load(rows)), "EXW", AS_OF)
    assert snap.values["liquidAssets"].value == pytest.approx(170 + 30)
    assert not snap.blocked, snap.blocked_by


def test_debt_and_finance_leases_on_one_line() -> None:  # Lumen, Norwegian Cruise, Sirius XM
    rows = rows_without("LongTermDebt")
    for e, f in ENDS:
        rows += [fx._inst("LongTermDebtAndCapitalLeaseObligations", e, 380, f),
                 fx._inst("LongTermDebtAndCapitalLeaseObligationsCurrent", e, 20, f)]  # fmt: skip
    snap = snapshot.build(with_market(load(rows)), "EXW", AS_OF)
    d = snap.values["debt"]
    assert d.value == pytest.approx(400)
    assert any("finance leases are inside" in w for w in d.warnings)
    assert snap.values["netDebt"].value == pytest.approx(200)


def test_convertible_long_term_notes_payable_count_as_debt() -> None:  # Plug Power
    rows = rows_without("LongTermDebt") + [
        fx._inst("ConvertibleLongTermNotesPayable", e, 400, f) for e, f in ENDS
    ]
    snap = snapshot.build(with_market(load(rows)), "EXW", AS_OF)
    assert snap.values["debt"].value == pytest.approx(400)


def test_net_interest_used_when_gross_is_not_filed() -> None:  # Charter, Kohl's
    rows = fx.base_rows() + interest_rows("InterestIncomeExpenseNonoperatingNet", -40, -30, -33)
    snap = snapshot.build(with_market(load(rows)), "EXW", AS_OF)
    i = snap.values["interestExpense"]
    assert i.value == pytest.approx(43) and any("net interest used" in w for w in i.warnings)


def test_implausibly_small_gross_interest_is_replaced_by_net() -> None:  # Norwegian Cruise
    rows = (fx.base_rows() + interest_rows("InterestExpense", 2, 1, 1)
            + interest_rows("InterestIncomeExpenseNonoperatingNet", -40, -30, -33))  # fmt: skip
    i = snapshot.build(with_market(load(rows)), "EXW", AS_OF).values["interestExpense"]
    assert i.value == pytest.approx(43)  # 2m on 400m of debt (0.5%) is not the interest bill


def test_implausible_gross_with_no_net_is_unknown_not_used() -> None:
    rows = fx.base_rows() + interest_rows("InterestExpense", 2, 1, 1)
    i = snapshot.build(with_market(load(rows)), "EXW", AS_OF).values["interestExpense"]
    assert i.value is None and "only 0.50%" in i.method


def test_gross_interest_is_preferred_when_plausible() -> None:
    rows = (fx.base_rows() + interest_rows("InterestExpense", 40, 30, 33)
            + interest_rows("InterestIncomeExpenseNonoperatingNet", -20, -15, -16))  # fmt: skip
    i = snapshot.build(with_market(load(rows)), "EXW", AS_OF).values["interestExpense"]
    assert i.value == pytest.approx(43) and not i.warnings


def test_operating_income_from_pretax_income_plus_interest() -> None:  # Xerox
    pre = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    rows = (
        rows_without("OperatingIncomeLoss")
        + interest_rows(pre, 160, 110, 135)
        + interest_rows("InterestExpense", 40, 30, 33)
    )
    snap = snapshot.build(with_market(load(rows)), "EXW", AS_OF)
    e = snap.values["ebit"]
    assert e.value == pytest.approx((160 + 135 - 110) + 43)
    assert any("pre-tax income + interest used" in w for w in e.warnings)


def test_undrawn_credit_lines_from_a_recent_filing_only() -> None:
    rows = fx.base_rows() + [
        fx._inst("LineOfCreditFacilityRemainingBorrowingCapacity", "2025-06-30", 500, fx.Q2_25)
    ]
    fu = F.Fundamentals(load(rows), fx.CIK, AS_OF)
    r = fu.revolver_available(AT)
    assert r.value == pytest.approx(500) and "as at 2025-06-30" in r.warnings[0]
    assert fu.revolver_available(date(2026, 3, 1)).value is None  # more than 200 days old
