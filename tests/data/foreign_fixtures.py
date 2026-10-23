"""Synthetic ESEF and GLEIF documents for a FICTIONAL company, "Exemplar Industrial plc" (LEI KEERSTEST0EXEMPLAR42,
London listing EXM.L, accounts in euros, shares quoted in pence).

None of these numbers or identifiers are real. They exist only to exercise the parsers, the currency rules and the
IFRS line items.
"""

from __future__ import annotations

from typing import Any

LEI = "KEERSTEST0EXEMPLAR42"
SYMBOL = "EXM.L"
NAME = "Exemplar Industrial plc (fictional)"
LEGAL_NAME = "EXEMPLAR INDUSTRIAL PUBLIC LIMITED COMPANY (FICTIONAL)"
M = 1_000_000

IFRS_NS = "https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full"

# name -> value for FY2023, FY2024, FY2025 (EUR millions)
FLOWS = {
    "Revenue": (900, 1000, 1100),
    "ProfitLossFromOperatingActivities": (90, 120, 130),
    "DepreciationAndAmortisationExpense": (40, 45, 50),
    "ProfitLossAttributableToOwnersOfParent": (50, 70, 80),
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": (30, 35, 40),
    "FinanceCosts": (20, 22, 24),
    "ProceedsFromBorrowingsClassifiedAsFinancingActivities": (10, 30, 30),
    "RepaymentsOfBorrowingsClassifiedAsFinancingActivities": (10, 30, 30),
}
# name -> balance at 2023-12-31, 2024-12-31, 2025-12-31 (EUR millions)
BALANCES = {
    "CashAndCashEquivalents": (100, 110, 120),
    "NoncurrentPortionOfNoncurrentBorrowings": (300, 290, 280),
    "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings": (40, 50, 60),
    "LeaseLiabilities": (80, 85, 90),
    "Assets": (1900, 1950, 2000),
    "CurrentAssets": (580, 590, 600),
    "CurrentLiabilities": (380, 390, 400),
    "Liabilities": (1150, 1180, 1200),
    "RetainedEarnings": (260, 280, 300),
    "EquityAttributableToOwnersOfParent": (700, 720, 750),
    "Equity": (750, 770, 800),
}
YEARS = (2023, 2024, 2025)


def _fact(concept: str, period: str, value: float, unit: str = "iso4217:EUR", decimals: int = -5,
          lei: str = LEI, **dims: str) -> dict[str, Any]:  # fmt: skip
    return {"value": str(value), "decimals": decimals,
            "dimensions": {"concept": f"ifrs-full:{concept}", "entity": f"lei:{lei}", "period": period, "unit": unit,
                           **dims}}  # fmt: skip


def duration(year: int) -> str:
    """xBRL-JSON writes a calendar year as midnight 1 January to midnight 1 January."""
    return f"{year}-01-01T00:00:00/{year + 1}-01-01T00:00:00"


def instant(year: int) -> str:
    """The balance at 31 December, written as midnight 1 January (OIM convention)."""
    return f"{year + 1}-01-01T00:00:00"


def report(year: int, shares_m: float = 200.0) -> dict[str, Any]:
    """The annual report for ``year``, with the prior year as comparatives, as filings.xbrl.org publishes it."""
    facts: dict[str, Any] = {}
    n = 0
    for y in (year - 1, year):
        i = YEARS.index(y)
        for c, vals in FLOWS.items():
            facts[f"f{(n := n + 1)}"] = _fact(c, duration(y), vals[i] * M)
        for c, vals in BALANCES.items():
            facts[f"f{(n := n + 1)}"] = _fact(c, instant(y), vals[i] * M)
    facts["shares"] = _fact("NumberOfSharesOutstanding", instant(year), shares_m * M, "xbrli:shares", 0)
    # Left out by the parser: a segment line (has a dimension), another entity, a text fact, an extension concept.
    facts["seg"] = _fact("Revenue", duration(year), 1 * M, **{"ifrs-full:SegmentsAxis": "exm:EuropeMember"})
    facts["sub"] = _fact("Revenue", duration(year), 5 * M, lei="KEERSTEST0SUBSIDIA01")
    facts["txt"] = {"value": "Exemplar Industrial plc",
                    "dimensions": {"concept": "ifrs-full:NameOfReportingEntityOrOtherMeansOfIdentification",
                                   "entity": f"lei:{LEI}", "period": instant(year)}}  # fmt: skip
    facts["ext"] = {"value": "123", "decimals": 0,
                    "dimensions": {"concept": "exm:AdjustedRevenue", "entity": f"lei:{LEI}",
                                   "period": duration(year), "unit": "iso4217:EUR"}}  # fmt: skip
    # A consistent duplicate at lower precision (kept once) .
    facts["dup"] = _fact("Revenue", duration(year), FLOWS["Revenue"][YEARS.index(year)] * M, decimals=-6)
    return {
        "documentInfo": {
            "documentType": "https://xbrl.org/2021/xbrl-json",
            "namespaces": {
                "ifrs-full": IFRS_NS,
                "lei": "http://standards.iso.org/iso/17442",
                "exm": "http://example.com/exm/2025",
                "xbrli": "http://www.xbrl.org/2003/instance",
                "iso4217": "http://www.xbrl.org/2003/iso4217",
            },  # fmt: skip
        },
        "facts": facts,
    }


def filing(year: int, added: str, n: int = 0) -> dict[str, Any]:
    base = f"/{LEI}/{year}-12-31/ESEF/GB/{n}"
    return {
        "type": "filing",
        "id": str(1000 + year * 10 + n),
        "attributes": {
            "fxo_id": f"{LEI}-{year}-12-31-ESEF-GB-{n}",
            "country": "GB",
            "period_end": f"{year}-12-31",
            "date_added": f"{added} 10:00:00",
            "error_count": 0,
            "json_url": f"{base}/exm-{year}-12-31.json",
            "viewer_url": f"{base}/reports/ixbrlviewer.html",
        },
        "relationships": {"entity": {"data": {"type": "entity", "id": "77"}}},
    }


def filings_list(items: list[dict[str, Any]], lei: str = LEI) -> dict[str, Any]:
    return {"data": items, "included": [{"type": "entity", "id": "77", "attributes": {"identifier": lei,
                                                                                     "name": LEGAL_NAME}}],
            "links": {"next": None}}  # fmt: skip


def gleif(records: list[tuple[str, str]]) -> dict[str, Any]:
    return {"data": [{"type": "lei-records", "id": lei,
                      "attributes": {"lei": lei, "entity": {"legalName": {"name": name},
                                                            "legalAddress": {"country": "GB"}, "status": "ACTIVE"}}}
                     for lei, name in records]}  # fmt: skip


def ecb_csv(cur: str, rows: list[tuple[str, float]]) -> str:
    head = "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    return head + "".join(f"EXR.D.{cur}.EUR.SP00.A,D,{cur},EUR,SP00,A,{d},{v},A\n" for d, v in rows)
