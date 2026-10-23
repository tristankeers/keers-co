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
# Net interest (income less expense, so negative when a net cost). Used only when gross interest is not usable.
NET_INTEREST = ("us-gaap:InterestIncomeExpenseNonoperatingNet", "us-gaap:InterestIncomeExpenseNet")
PRETAX_INCOME = (
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)
REVOLVER_AVAILABLE = ("us-gaap:LineOfCreditFacilityRemainingBorrowingCapacity",)
SBC = ("us-gaap:ShareBasedCompensation",)
CASH = ("us-gaap:CashAndCashEquivalentsAtCarryingValue", "us-gaap:Cash")
ST_INVESTMENTS = (
    "us-gaap:ShortTermInvestments",
    "us-gaap:MarketableSecuritiesCurrent",
    "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
)
DEPRECIATION = ("us-gaap:Depreciation",)
AMORTIZATION = ("us-gaap:AdjustmentForAmortization", "us-gaap:AmortizationOfIntangibleAssets")
DEBT_TOTAL = ("us-gaap:LongTermDebt",)
DEBT_NONCURRENT = ("us-gaap:LongTermDebtNoncurrent",)
DEBT_CURRENT = ("us-gaap:LongTermDebtCurrent",)
DEBT_CURRENT_ALL = ("us-gaap:DebtCurrent",)  # current maturities plus short-term borrowings
ST_BORROWINGS = ("us-gaap:ShortTermBorrowings", "us-gaap:CommercialPaper")
CONVERTIBLE_NONCURRENT = (
    "us-gaap:ConvertibleDebtNoncurrent",
    "us-gaap:ConvertibleNotesPayable",
    "us-gaap:ConvertibleLongTermNotesPayable",
)
CONVERTIBLE_CURRENT = ("us-gaap:ConvertibleDebtCurrent", "us-gaap:ConvertibleNotesPayableCurrent")
# Borrowings under credit facilities, filed apart from long-term debt (Dropbox's term loan since 2024).
LINES_OF_CREDIT_NONCURRENT = ("us-gaap:LongTermLineOfCredit", "us-gaap:LongTermLineOfCreditNoncurrent")
LINES_OF_CREDIT_CURRENT = ("us-gaap:LinesOfCreditCurrent",)
# Debt and finance leases on one line, as some companies file it (Lumen, Norwegian Cruise, Sirius XM).
DEBT_AND_LEASES_NONCURRENT = ("us-gaap:LongTermDebtAndCapitalLeaseObligations",)
DEBT_AND_LEASES_CURRENT = ("us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",)
DEBT_AND_LEASES_TOTAL = ("us-gaap:DebtAndCapitalLeaseObligations",)
FINANCE_LEASES = ("us-gaap:FinanceLeaseLiability",)
FINANCE_LEASES_SPLIT = ("us-gaap:FinanceLeaseLiabilityCurrent", "us-gaap:FinanceLeaseLiabilityNoncurrent")
OPERATING_LEASES = ("us-gaap:OperatingLeaseLiability",)
OPERATING_LEASES_SPLIT = (
    "us-gaap:OperatingLeaseLiabilityCurrent",
    "us-gaap:OperatingLeaseLiabilityNoncurrent",
)
CASH_AND_STI = ("us-gaap:CashCashEquivalentsAndShortTermInvestments",)
SHARES_COVER = ("dei:EntityCommonStockSharesOutstanding",)
SHARES_BALANCE = ("us-gaap:CommonStockSharesOutstanding",)
SHARES_DILUTED = ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",)

# Cash-flow lines for the debt roll-forward check (borrowed, repaid, and net lines filed as one figure).
BORROWED = tuple(f"us-gaap:{c}" for c in (
    "ProceedsFromLinesOfCredit", "ProceedsFromLongTermLinesOfCredit", "ProceedsFromSecuredLinesOfCredit",
    "ProceedsFromUnsecuredLinesOfCredit", "ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt",
    "ProceedsFromIssuanceOfSecuredDebt", "ProceedsFromIssuanceOfUnsecuredDebt", "ProceedsFromIssuanceOfSeniorLongTermDebt",
    "ProceedsFromConvertibleDebt", "ProceedsFromBankDebt", "ProceedsFromShortTermDebt",
))  # fmt: skip
REPAID = tuple(f"us-gaap:{c}" for c in (
    "RepaymentsOfLinesOfCredit", "RepaymentsOfLongTermLinesOfCredit", "RepaymentsOfSecuredDebt",
    "RepaymentsOfUnsecuredDebt", "RepaymentsOfLongTermDebt", "RepaymentsOfDebt", "RepaymentsOfSeniorDebt",
    "RepaymentsOfConvertibleDebt", "RepaymentsOfBankDebt", "RepaymentsOfShortTermDebt",
))  # fmt: skip
NET_BORROWED = tuple(f"us-gaap:{c}" for c in (
    "ProceedsFromRepaymentsOfCommercialPaper", "ProceedsFromRepaymentsOfShortTermDebt", "ProceedsFromRepaymentsOfDebt",
    "ProceedsFromRepaymentsOfLinesOfCredit",
))  # fmt: skip
# Balance-sheet lines for the distress screen (Altman Z and near-term maturities).
ASSETS = ("us-gaap:Assets",)
ASSETS_CURRENT = ("us-gaap:AssetsCurrent",)
LIABILITIES = ("us-gaap:Liabilities",)
LIABILITIES_CURRENT = ("us-gaap:LiabilitiesCurrent",)
LIABILITIES_AND_EQUITY = ("us-gaap:LiabilitiesAndStockholdersEquity",)
RETAINED_EARNINGS = ("us-gaap:RetainedEarningsAccumulatedDeficit",)
EQUITY = ("us-gaap:StockholdersEquity",)
EQUITY_INCL_NCI = ("us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",)
DEBT_DUE_12M = ("us-gaap:LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",)
BALANCE_SHEET = (
    ASSETS + ASSETS_CURRENT + LIABILITIES + LIABILITIES_CURRENT + LIABILITIES_AND_EQUITY + RETAINED_EARNINGS
    + EQUITY + EQUITY_INCL_NCI + DEBT_DUE_12M
)  # fmt: skip

ROLLFORWARD_TOLERANCE = 0.15  # of the larger of expected and found debt
ROLLFORWARD_MIN_USDM = 50.0

DEBT_CONCEPTS = (
    DEBT_TOTAL + DEBT_NONCURRENT + DEBT_CURRENT + DEBT_CURRENT_ALL + ST_BORROWINGS + CONVERTIBLE_NONCURRENT
    + CONVERTIBLE_CURRENT + FINANCE_LEASES + FINANCE_LEASES_SPLIT + DEBT_AND_LEASES_NONCURRENT
    + DEBT_AND_LEASES_CURRENT + DEBT_AND_LEASES_TOTAL + LINES_OF_CREDIT_NONCURRENT + LINES_OF_CREDIT_CURRENT
)  # fmt: skip

ALL_CONCEPTS: tuple[str, ...] = (
    REVENUE + OPERATING_INCOME + DA + DEPRECIATION + AMORTIZATION + NET_INCOME + CAPEX + INTEREST + SBC + CASH
    + ST_INVESTMENTS + CASH_AND_STI + DEBT_CONCEPTS + OPERATING_LEASES + OPERATING_LEASES_SPLIT + SHARES_COVER
    + SHARES_BALANCE + SHARES_DILUTED + BORROWED + REPAID + NET_BORROWED + BALANCE_SHEET + NET_INTEREST
    + PRETAX_INCOME + REVOLVER_AVAILABLE
)  # fmt: skip


# An implied interest rate on the debt found above this means some debt is probably tagged in a way we do not
# read (for example a company-specific tag), so net debt is not trusted.
MAX_IMPLIED_RATE = 0.15
# Gross interest implying less than this on the debt found is probably a side line (Norwegian Cruise files a small
# InterestExpenseNonoperating beside its real, net interest), so it is not used.
MIN_IMPLIED_RATE = 0.01
SHARES_MAX_AGE_DAYS = 200

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

    def flow(self, candidates: Sequence[str], label: str, at: date | None = None) -> Value:
        """TTM of a flow. With ``at``, only a TTM ending at ``at`` is accepted, so every figure covers one period."""
        if at is not None:
            latest: tuple[date, str] | None = None
            for c in candidates:
                facts = self._facts(c)
                p = ttm_at(facts, at)
                if p is not None:
                    return self._value(p, c, "USDm", MILLION, "")
                ends = [f.end for f in facts if f.start is not None]
                if ends and (latest is None or max(ends) > latest[0]):
                    latest = (max(ends), c)
            if latest is None:
                return missing("USDm", f"{label}: not filed under {', '.join(candidates)}")
            return missing("USDm", f"{label}: no TTM to {at} (latest is {latest[1]} to {latest[0]})")
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

    def depreciation_amortisation(self, at: date) -> Value:
        """Total D&A if filed; otherwise depreciation plus amortisation filed as separate lines."""
        total = self.flow(DA, "depreciation and amortisation", at)
        if total.known:
            return total
        dep = self.flow(DEPRECIATION, "depreciation", at)
        if not dep.known:
            return missing("USDm", f"D&A: {total.method}; {dep.method}")
        amo = self.flow(AMORTIZATION, "amortisation", at)
        parts = [dep, amo] if amo.known else [dep]
        warn = () if amo.known else ("no amortisation line filed; D&A is depreciation only",)
        return _sum(parts, " + ".join(_name(v) for v in parts), warn)

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
        """Balance at ``at`` (within 3 days), first matching candidate in priority order.

        An older balance is never used in place of the one at ``at``: the result is missing instead.
        With ``at`` None, the latest balance of the newest candidate is used.
        """
        if at is None:
            hit = self._pick(candidates, instant=True)
            if hit is None:
                return missing(unit, f"{label}: not filed under {', '.join(candidates)}")
            return self._value(instant_at(hit[1]), hit[0], unit, MILLION, f"{label}: no balance")
        latest: tuple[date, str] | None = None
        for c in candidates:
            inst = [f for f in latest_known(self._facts(c)) if f.start is None]
            hit_at = [f for f in inst if _close(f.end, at, 3)]
            if hit_at:
                f = hit_at[-1]
                return self._value(
                    Period(f.value, f.end, "as reported", Kind.REPORTED, (f,)), c, unit, MILLION, ""
                )
            if inst and (latest is None or inst[-1].end > latest[0]):
                latest = (inst[-1].end, c)
        if latest is None:
            return missing(unit, f"{label}: not filed under {', '.join(candidates)}")
        return missing(unit, f"{label}: no balance at {at} (latest is {latest[1]} at {latest[0]})")

    def ever_filed(self, candidates: Sequence[str]) -> bool:
        return any(self._facts(c) for c in candidates)

    def _first(self, groups: Sequence[Sequence[str]], label: str, at: date) -> Value:
        """First group whose concepts are all filed at ``at``; a group of several is summed."""
        for g in groups:
            vals = [self.balance((c,), label, at) for c in g]
            if all(v.known for v in vals):
                return vals[0] if len(vals) == 1 else _sum(vals, " + ".join(c.split(":")[1] for c in g))
        return missing("USDm", f"{label}: no balance at {at}")

    def total_debt(self, at: date | None, include_leases: bool = True) -> Value:
        """Borrowings at ``at``, excluding operating leases. Never reads a missing balance as zero.

        Non-current and current debt come from the balance-sheet lines where filed, because companies use
        ``LongTermDebt`` both for total debt and for the non-current line only. Convertible notes and finance
        leases are added when filed as separate lines. Zero is used only for a company that has never filed a debt
        concept at all.
        """
        if at is None:
            return missing("USDm", "debt: no period to read the balance at")
        notes: list[str] = []
        parts: list[Value] = []
        leases_inside = False
        nc = self.balance(DEBT_NONCURRENT, "non-current debt", at)
        cur_all = self.balance(DEBT_CURRENT_ALL, "current debt", at)
        if nc.known:
            parts.append(nc)
            if cur_all.known:
                parts.append(cur_all)
            else:
                parts += [v for v in (self.balance(DEBT_CURRENT, "current debt", at),
                                      self.balance(ST_BORROWINGS, "short-term borrowings", at)) if v.known]  # fmt: skip
        else:
            ltd = self.balance(DEBT_TOTAL, "long-term debt", at)
            if ltd.known:
                parts.append(ltd)
                if cur_all.known:
                    # Seen at Adobe: LongTermDebt carries the non-current line and DebtCurrent the current one.
                    parts.append(cur_all)
                    notes.append(
                        "LongTermDebt read as the non-current line because DebtCurrent is filed beside it"
                    )
                else:
                    stb = self.balance(ST_BORROWINGS, "short-term borrowings", at)
                    if stb.known:
                        parts.append(stb)
            else:
                dl = self._first((DEBT_AND_LEASES_NONCURRENT + DEBT_AND_LEASES_CURRENT, DEBT_AND_LEASES_NONCURRENT,
                                  DEBT_AND_LEASES_TOTAL), "debt and finance leases", at)  # fmt: skip
                if dl.known:
                    parts.append(dl)
                    leases_inside = True
                elif cur_all.known:
                    parts.append(cur_all)
        has_base = nc.known or any(
            "LongTermDebt" in _name(p_) or "DebtAndCapitalLease" in _name(p_) for p_ in parts
        )
        for groups, lab in (
            (tuple((c,) for c in CONVERTIBLE_NONCURRENT), "convertible notes"),
            (tuple((c,) for c in CONVERTIBLE_CURRENT), "current convertible notes"),
        ):
            v = self._first(groups, lab, at)
            if not v.known or not v.value:
                continue
            if has_base:
                notes.append(
                    f"{lab} filed separately ({v.value:,.1f}m); taken as already inside long-term debt"
                )
            else:
                parts.append(v)
        # Credit-facility borrowings on their own lines. Like convertible notes, the non-current line is taken as
        # already inside long-term debt when that is filed, since double counting would pass every check.
        loc = self._first(tuple((c,) for c in LINES_OF_CREDIT_NONCURRENT), "credit facility borrowings", at)
        if loc.known and loc.value:
            if has_base:
                notes.append(f"credit facility borrowings filed separately ({loc.value:,.1f}m); taken as already "
                             "inside long-term debt")  # fmt: skip
            else:
                parts.append(loc)
        loc_cur = self.balance(LINES_OF_CREDIT_CURRENT, "current credit facility borrowings", at)
        if loc_cur.known and loc_cur.value:
            if has_base or cur_all.known:
                notes.append(f"current credit facility borrowings filed separately ({loc_cur.value:,.1f}m); taken "
                             "as already inside current debt")  # fmt: skip
            else:
                parts.append(loc_cur)
        lease = self._first((FINANCE_LEASES, FINANCE_LEASES_SPLIT), "finance leases", at)
        if leases_inside:
            notes.append("finance leases are inside the debt line filed")
        elif lease.known and include_leases:
            parts.append(lease)
        if not parts:
            if not self.ever_filed(DEBT_CONCEPTS):
                return Value(0.0, Kind.DERIVED, "USDm", "no debt concept ever filed; taken as zero", at,
                             warnings=("no debt concept ever filed; debt taken as zero",))  # fmt: skip
            return missing(
                "USDm", f"debt: no debt balance at {at}, though debt concepts have been filed before"
            )
        v = _sum(parts, " + ".join(_name(p_) for p_ in parts) + " (operating leases excluded)",
                 tuple(notes))  # fmt: skip
        return v

    def operating_income(self, at: date) -> Value:
        """Operating income (TTM). Without it, pre-tax income plus interest expense, with a warning."""
        ebit = self.flow(OPERATING_INCOME, "operating income", at)
        if ebit.known:
            return ebit
        pretax = self.flow(PRETAX_INCOME, "pre-tax income", at)
        interest = self.flow(INTEREST, "interest expense", at)
        if pretax.known and interest.known:
            return _sum([pretax, interest], "pre-tax income + interest expense (no operating income filed)",
                        ("operating income not filed; pre-tax income + interest used, so other non-operating "
                         "items are included",))  # fmt: skip
        return ebit

    def interest_expense(self, at: date, debt: Value) -> Value:
        """Interest expense (TTM): gross where it is plausible for the debt found, else net interest cost."""
        gross = self.flow(INTEREST, "interest expense", at)
        d = debt.value if debt.known else None
        plausible = gross.known and (d is None or d <= 0 or (gross.value or 0) / d >= MIN_IMPLIED_RATE)
        if plausible:
            return gross
        net = self.flow(NET_INTEREST, "net interest", at)
        if net.known and (net.value or 0) < 0:
            return Value(-(net.value or 0), net.kind, net.unit, f"−{net.method}", net.period_end, net.sources,
                         net.warnings + ("net interest used (interest income netted off), so cover is overstated "
                                         "slightly",))  # fmt: skip
        if gross.known:
            assert gross.value is not None and d is not None  # noqa: S101
            return missing("USDm", f"interest expense: {_name(gross)} of {gross.value:,.1f}m is only "
                                   f"{gross.value / d:.2%} of the {d:,.1f}m of debt found, and no net interest is filed")  # fmt: skip
        return gross

    def revolver_available(self, at: date, max_age_days: int = 200) -> Value:
        """Undrawn committed credit lines, from the latest filing within ``max_age_days`` of ``at``."""
        facts = [f for c in REVOLVER_AVAILABLE for f in latest_known(self._facts(c)) if f.start is None]
        facts = [f for f in facts if f.end <= at and (at - f.end).days <= max_age_days]
        if not facts:
            return missing("USDm", "undrawn credit lines: none filed in the last 200 days")
        last = max(f.end for f in facts)
        same = [f for f in facts if f.end == last]
        f = same[-1]
        warn = (f"undrawn credit lines as at {f.end}, not {at}",) if f.end != at else ()
        return Value(f.value / MILLION, Kind.REPORTED, "USDm", f"{REVOLVER_AVAILABLE[0]}: as reported", f.end,
                     (f.cite(),), warn)  # fmt: skip

    def debt_check(self, debt: Value, at: date) -> str | None:
        """Deterministic completeness check: interest expense far too high for the debt found means debt is missing."""
        interest = self.flow(INTEREST, "interest expense", at)
        if not (interest.known and debt.known) or interest.value is None or debt.value is None:
            return None
        if debt.value <= 0:
            return (
                f"debt looks incomplete: interest expense of {interest.value:,.1f}m with no debt found"
                if interest.value > 1
                else None
            )
        rate = interest.value / debt.value
        if rate > MAX_IMPLIED_RATE:
            return (f"debt looks incomplete: interest expense {interest.value:,.1f}m is {rate:.0%} of the "
                    f"{debt.value:,.1f}m of debt found (limit {MAX_IMPLIED_RATE:.0%}); some debt is probably filed "
                    "under a tag we do not read")  # fmt: skip
        return None

    def _ytd_sum(self, concepts: Sequence[str], start: date, end: date) -> tuple[float, list[Fact]]:
        total, used = 0.0, []
        for c in concepts:
            hit = [f for f in latest_known(self._facts(c))
                   if f.start is not None and _close(f.start, start, 3) and _close(f.end, end, 3)]  # fmt: skip
            if hit:
                total += hit[-1].value
                used.append(hit[-1])
        return total / MILLION, used

    def debt_rollforward(self, at: date, year_start: date) -> str | None:
        """Debt at the last year end, plus borrowed less repaid since, should roughly equal debt now.

        Much less debt than expected means a borrowing is filed under a tag we do not read. Finance leases are left
        out on both sides because new leases are not cash borrowings.
        """
        before = self.total_debt(year_start - timedelta(days=1), include_leases=False)
        now = self.total_debt(at, include_leases=False)
        if not (before.known and now.known) or before.value is None or now.value is None:
            return None
        borrowed, b_used = self._ytd_sum(BORROWED, year_start, at)
        repaid, r_used = self._ytd_sum(REPAID, year_start, at)
        net, n_used = self._ytd_sum(NET_BORROWED, year_start, at)
        if not (b_used or r_used or n_used):
            return None
        expected = before.value + borrowed - repaid + net
        gap = expected - now.value
        limit = max(ROLLFORWARD_TOLERANCE * max(expected, now.value), ROLLFORWARD_MIN_USDM)
        if gap > limit:
            return (f"debt looks incomplete: {before.value:,.1f}m at {year_start - timedelta(days=1)} + "
                    f"{borrowed:,.1f}m borrowed − {repaid:,.1f}m repaid{f' + {net:,.1f}m net' if n_used else ''} "
                    f"= {expected:,.1f}m expected, but only {now.value:,.1f}m found at {at}; some debt is probably "
                    "filed under a tag we do not read")  # fmt: skip
        return None

    def year_start(self, at: date) -> date | None:
        """First day of the fiscal year that contains ``at``, from the revenue periods filed."""
        for c in REVENUE:
            durations = [f for f in latest_known(self._facts(c)) if f.start is not None]
            for f in durations:
                if _close(f.end, at, 3) and f.days is not None and f.days <= YEAR[1] and f.start is not None:
                    prior = [a for a in _annual(durations) if _close(a.end, f.start - timedelta(days=1), 3)]
                    if prior or (f.days >= YEAR[0]):
                        return f.start
        return None

    def liquid_assets(self, at: date) -> Value:
        """Cash plus short-term investments at ``at``. The combined line is used where a company files it."""
        both = self.balance(CASH_AND_STI, "cash and short-term investments", at)
        if both.known:
            return both
        cash = self.balance(CASH, "cash", at)
        if not cash.known:
            return cash
        sti = self.balance(ST_INVESTMENTS, "short-term investments", at)
        return (
            _sum([cash, sti], "cash + short-term investments")
            if sti.known
            else _sum([cash], "cash (no short-term investments filed at this date)")
        )

    def operating_leases(self, at: date) -> Value:
        return self._first((OPERATING_LEASES, OPERATING_LEASES_SPLIT), "operating lease liabilities", at)

    def shares(self, at: date | None) -> Value:
        """Shares outstanding near ``at``: the cover-page count, else the balance sheet, else diluted average.

        A count older than ``SHARES_MAX_AGE_DAYS`` before the as-of date, or from before the period, is not used.
        """
        lo = (at - timedelta(days=10)) if at else self.as_of - timedelta(days=SHARES_MAX_AGE_DAYS)
        lo = max(lo, self.as_of - timedelta(days=SHARES_MAX_AGE_DAYS))
        for cands, instant, note in (
            (SHARES_COVER, True, None),
            (SHARES_BALANCE, True, None),
            (
                SHARES_DILUTED,
                False,
                "diluted weighted-average shares used; no current shares-outstanding count filed",
            ),
        ):
            for c in cands:
                fs = [f for f in latest_known(self._facts(c)) if (f.start is None) == instant and f.end >= lo]
                if instant is False and at is not None:
                    fs = [f for f in fs if _close(f.end, at, 3)]
                if not fs:
                    continue
                f = fs[-1]
                v = self._value(
                    Period(f.value, f.end, "as reported", Kind.REPORTED, (f,)), c, "m shares", MILLION, ""
                )
                return Value(
                    v.value, v.kind, v.unit, v.method, v.period_end, v.sources, (note,) if note else ()
                )
        return missing("m shares", f"shares outstanding: no count filed since {lo}")


def _name(v: Value) -> str:
    """Concept name from a method like ``us-gaap:LongTermDebt: as reported``; composite methods are returned whole."""
    bits = v.method.split(":")
    return bits[1].split(" ")[0] if len(bits) > 2 else v.method


def _sum(parts: Sequence[Value], method: str, warnings: tuple[str, ...] = ()) -> Value:
    ends = [p.period_end for p in parts if p.period_end]
    return Value(
        sum(p.value or 0 for p in parts), Kind.DERIVED if len(parts) > 1 else parts[0].kind, parts[0].unit,
        method, max(ends) if ends else None, tuple(s for p in parts for s in p.sources),
        tuple(w for p in parts for w in p.warnings) + warnings,
    )  # fmt: skip
