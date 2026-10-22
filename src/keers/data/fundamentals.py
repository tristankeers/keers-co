"""Point-in-time fundamentals from filed XBRL facts.

Rules (all deterministic, no estimates):

* Only facts filed on or before the "as of" date are used, so a back-dated snapshot sees what the market saw.
* Where a period has been reported more than once (the original 10-Q, then as a comparative in later filings), the
  latest filing known at the as-of date wins. That includes restatements.
* Trailing twelve months (TTM) = latest fiscal year + current year-to-date − prior-year year-to-date. When the
  latest period is a fiscal year end, TTM is that year as reported. If neither works, the latest fiscal year is used
  and flagged; nothing is extrapolated.
* Several concept names can carry the same line item (companies move between them). The candidate with the most
  recent period wins, ties going to the first in the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .models import Fact, Kind, Value, missing
from .store import Store

MILLION = 1_000_000.0

# taxonomy:concept candidates, in priority order.
REVENUE = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
)
OPERATING_INCOME = ("us-gaap:OperatingIncomeLoss",)
DA = (
    "us-gaap:DepreciationDepletionAndAmortization",
    "us-gaap:DepreciationAmortizationAndAccretionNet",
    "us-gaap:DepreciationAndAmortization",
)
NET_INCOME = ("us-gaap:NetIncomeLoss",)
CAPEX = ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets")
INTEREST = ("us-gaap:InterestExpense", "us-gaap:InterestExpenseNonoperating", "us-gaap:InterestExpenseDebt")
SBC = ("us-gaap:ShareBasedCompensation",)
CASH = ("us-gaap:CashAndCashEquivalentsAtCarryingValue",)
ST_INVESTMENTS = (
    "us-gaap:ShortTermInvestments",
    "us-gaap:MarketableSecuritiesCurrent",
    "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
)
DEBT_TOTAL = ("us-gaap:LongTermDebt",)
DEBT_NONCURRENT = ("us-gaap:LongTermDebtNoncurrent",)
DEBT_CURRENT = ("us-gaap:LongTermDebtCurrent",)
ST_BORROWINGS = ("us-gaap:ShortTermBorrowings", "us-gaap:CommercialPaper")
FINANCE_LEASES = ("us-gaap:FinanceLeaseLiability",)
SHARES_COVER = ("dei:EntityCommonStockSharesOutstanding",)
SHARES_BALANCE = ("us-gaap:CommonStockSharesOutstanding",)
SHARES_DILUTED = ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",)

ALL_CONCEPTS: tuple[str, ...] = (
    REVENUE + OPERATING_INCOME + DA + NET_INCOME + CAPEX + INTEREST + SBC + CASH + ST_INVESTMENTS + DEBT_TOTAL
    + DEBT_NONCURRENT + DEBT_CURRENT + ST_BORROWINGS + FINANCE_LEASES + SHARES_COVER + SHARES_BALANCE + SHARES_DILUTED
)  # fmt: skip

YEAR = (350, 380)  # days in a fiscal year (52/53-week years included)
NEAR = 10  # days of slack when matching period boundaries


def _close(a: date, b: date, slack: int = NEAR) -> bool:
    return abs((a - b).days) <= slack


def latest_known(facts: Sequence[Fact]) -> list[Fact]:
    """One fact per (start, end): the most recently filed. Input must already be filtered to the as-of date."""
    best: dict[tuple[date | None, date], Fact] = {}
    for f in facts:
        k = (f.start, f.end)
        if k not in best or (f.filed, f.accn) > (best[k].filed, best[k].accn):
            best[k] = f
    return sorted(best.values(), key=lambda f: (f.end, f.start or f.end))


@dataclass(frozen=True)
class Period:
    value: float
    end: date
    method: str
    kind: Kind
    used: tuple[Fact, ...]
    warnings: tuple[str, ...] = ()


def _annual(durations: Sequence[Fact]) -> list[Fact]:
    return [f for f in durations if f.days is not None and YEAR[0] <= f.days <= YEAR[1]]


def ttm_at(facts: Sequence[Fact], end: date) -> Period | None:
    """TTM for the twelve months ending near ``end``, or None if the filed facts cannot give it exactly."""
    durations = [f for f in latest_known(facts) if f.start is not None]
    for fy in _annual(durations):
        if _close(fy.end, end, 3):
            return Period(fy.value, fy.end, "fiscal year as reported", Kind.REPORTED, (fy,))
    for cur in durations:
        if not _close(cur.end, end, 3) or cur.days is None or cur.days >= YEAR[0]:
            continue
        assert cur.start is not None  # noqa: S101 (durations only)
        prior_fy = next(
            (f for f in _annual(durations) if _close(f.end, cur.start - timedelta(days=1), 3)), None
        )
        if prior_fy is None:
            continue
        prev = next(
            (
                f
                for f in durations
                if f.start is not None
                and f.days is not None
                and _close(f.end, cur.end - timedelta(days=365), NEAR)
                and abs(f.days - cur.days) <= NEAR
                and prior_fy.start is not None
                and _close(f.start, prior_fy.start, NEAR)
            ),
            None,
        )
        if prev is None:
            continue
        value = prior_fy.value + cur.value - prev.value
        return Period(
            value, cur.end, "TTM = fiscal year + current year-to-date − prior year-to-date", Kind.DERIVED,
            (prior_fy, cur, prev),
        )  # fmt: skip
    return None


def ttm_latest(facts: Sequence[Fact]) -> Period | None:
    durations = [f for f in latest_known(facts) if f.start is not None]
    if not durations:
        return None
    for end in sorted({f.end for f in durations}, reverse=True):
        p = ttm_at(facts, end)
        if p is not None:
            if end != max(f.end for f in durations):
                return Period(p.value, p.end, p.method, p.kind, p.used,
                              (f"TTM could not be built to {max(f.end for f in durations)}; latest exact TTM is to {p.end}",))  # fmt: skip
            return p
    return None


def instant_at(facts: Sequence[Fact], end: date | None = None) -> Period | None:
    """Balance-sheet value at ``end`` (or the latest one if ``end`` is None)."""
    inst = [f for f in latest_known(facts) if f.start is None]
    if not inst:
        return None
    if end is not None:
        hit = [f for f in inst if _close(f.end, end, 3)]
        if hit:
            f = hit[-1]
            return Period(f.value, f.end, "as reported", Kind.REPORTED, (f,))
    f = inst[-1]
    warn = (f"balance at {f.end}, not at the income period end {end}",) if end and f.end != end else ()
    return Period(f.value, f.end, "as reported", Kind.REPORTED, (f,), warn)


class Fundamentals:
    """Reads facts from the store for one company as of a date."""

    def __init__(self, store: Store, cik: int, as_of: date) -> None:
        self.store, self.cik, self.as_of = store, cik, as_of

    def _facts(self, concept: str) -> list[Fact]:
        tax, name = concept.split(":", 1)
        unit = "shares" if "Shares" in name else "USD"
        return self.store.facts(self.cik, name, unit=unit, known_by=self.as_of, taxonomy=tax)

    def _pick(self, candidates: Sequence[str], instant: bool) -> tuple[str, list[Fact]] | None:
        best: tuple[date, int, str, list[Fact]] | None = None
        for i, c in enumerate(candidates):
            fs = [f for f in self._facts(c) if (f.start is None) == instant]
            if not fs:
                continue
            key = (max(f.end for f in fs), -i)
            if best is None or key > best[:2]:
                best = (key[0], key[1], c, fs)
        return None if best is None else (best[2], best[3])

    def _value(self, p: Period | None, concept: str, unit: str, scale: float, why: str) -> Value:
        if p is None:
            return missing(unit, why)
        return Value(
            p.value / scale, p.kind, unit, f"{concept}: {p.method}", p.end,
            tuple(f.cite() for f in p.used), p.warnings,
        )  # fmt: skip

    def flow(self, candidates: Sequence[str], label: str) -> Value:
        hit = self._pick(candidates, instant=False)
        if hit is None:
            return missing("USDm", f"{label}: not filed under {', '.join(candidates)}")
        concept, facts = hit
        p = ttm_latest(facts)
        if p is None:
            annual = _annual([f for f in latest_known(facts) if f.start is not None])
            if annual:
                f = annual[-1]
                p = Period(f.value, f.end, "latest fiscal year (TTM could not be built)", Kind.REPORTED, (f,),
                           ("TTM could not be built; latest fiscal year used",))  # fmt: skip
        return self._value(p, concept, "USDm", MILLION, f"{label}: no usable period")

    def flow_growth(self, candidates: Sequence[str], label: str) -> Value:
        """TTM growth versus the TTM a year earlier, both built the same way."""
        hit = self._pick(candidates, instant=False)
        if hit is None:
            return missing("ratio", f"{label} growth: not filed")
        concept, facts = hit
        now = ttm_latest(facts)
        if now is None:
            return missing("ratio", f"{label} growth: no TTM")
        before = ttm_at(facts, now.end - timedelta(days=364)) or ttm_at(facts, now.end - timedelta(days=371))
        if before is None or before.value <= 0:
            return missing("ratio", f"{label} growth: no comparable TTM a year earlier")
        return Value(
            now.value / before.value - 1, Kind.DERIVED, "ratio", f"{concept}: TTM to {now.end} / TTM to {before.end} − 1",
            now.end, tuple(f.cite() for f in now.used + before.used), now.warnings + before.warnings,
        )  # fmt: skip

    def balance(self, candidates: Sequence[str], label: str, at: date | None, unit: str = "USDm") -> Value:
        hit = self._pick(candidates, instant=True)
        if hit is None:
            return missing(unit, f"{label}: not filed under {', '.join(candidates)}")
        concept, facts = hit
        return self._value(instant_at(facts, at), concept, unit, MILLION, f"{label}: no balance")

    def total_debt(self, at: date | None) -> Value:
        """Borrowings: long-term debt (current + non-current) plus short-term borrowings and finance leases."""
        parts: list[Value] = []
        total = self.balance(DEBT_TOTAL, "long-term debt", at)
        if total.known:
            parts.append(total)
        else:
            nc, cur = (
                self.balance(DEBT_NONCURRENT, "long-term debt", at),
                self.balance(DEBT_CURRENT, "current debt", at),
            )
            parts += [v for v in (nc, cur) if v.known]
        for cands, lab in ((ST_BORROWINGS, "short-term borrowings"), (FINANCE_LEASES, "finance leases")):
            v = self.balance(cands, lab, at)
            if v.known and (at is None or v.period_end is None or _close(v.period_end, at, 3)):
                parts.append(v)
        if not parts:
            # A company that files no debt concept at all is treated as having none, and says so.
            return Value(0.0, Kind.DERIVED, "USDm", "no debt concepts filed; taken as zero", at,
                         warnings=("no debt concepts filed; debt taken as zero",))  # fmt: skip
        return Value(
            sum(v.value or 0 for v in parts), Kind.DERIVED, "USDm",
            " + ".join(v.method.split(":")[0] for v in parts) + " (operating leases excluded)",
            max((v.period_end for v in parts if v.period_end), default=None),
            tuple(s for v in parts for s in v.sources), tuple(w for v in parts for w in v.warnings),
        )  # fmt: skip

    def shares(self, at: date | None) -> Value:
        for cands, note in (
            (SHARES_COVER, None),
            (SHARES_BALANCE, None),
            (SHARES_DILUTED, "diluted weighted-average shares used; no shares-outstanding figure filed"),
        ):
            hit = self._pick(cands, instant=cands is not SHARES_DILUTED)
            if hit is None:
                continue
            concept, facts = hit
            p: Period | None
            if cands is SHARES_DILUTED:
                durations = [f for f in latest_known(facts) if f.start is not None]
                f = durations[-1]
                p = Period(f.value, f.end, "latest reported period", Kind.REPORTED, (f,))
            else:
                p = instant_at(facts)  # latest count, which is usually the cover page date
            assert p is not None  # noqa: S101 (facts is non-empty)
            v = self._value(p, concept, "m shares", MILLION, "shares")
            return Value(v.value, v.kind, v.unit, v.method, v.period_end, v.sources,
                         v.warnings + ((note,) if note else ()))  # fmt: skip
        return missing("m shares", "shares outstanding: not filed")
