"""Synthetic SEC and market documents for a FICTIONAL company, "Example Widgets Inc" (CIK 9999999, ticker EXW).

None of these numbers are real. They exist only to exercise the parsers and the TTM / point-in-time rules.
"""

from __future__ import annotations

import json
from typing import Any

CIK = 9999999
TICKER = "EXW"
NAME = "Example Widgets Inc (fictional)"

# (taxonomy, concept, unit, start, end, value, accn, form, filed)
Row = tuple[str, str, str, str | None, str, float, str, str, str]

K24 = ("0000000001-25-000001", "10-K", "2025-02-14")  # FY2024 10-K
Q3_24 = ("0000000001-24-000003", "10-Q", "2024-11-05")
Q2_25 = ("0000000001-25-000002", "10-Q", "2025-08-05")
Q3_25 = ("0000000001-25-000003", "10-Q", "2025-11-04")

M = 1_000_000


def _dur(concept: str, start: str, end: str, val: float, filing: tuple[str, str, str]) -> Row:
    return ("us-gaap", concept, "USD", start, end, val * M, *filing)


def _inst(concept: str, end: str, val: float, filing: tuple[str, str, str], unit: str = "USD") -> Row:
    return ("us-gaap", concept, unit, None, end, val * M, *filing)


def base_rows() -> list[Row]:
    """FY2024 plus Q2 and Q3 2025 filings, each with its prior-year comparatives (calendar fiscal year)."""
    rows: list[Row] = []
    # name: (FY2024, 9M 2024, 9M 2025, 6M 2024, 6M 2025, Q2 2025 three-month)
    flows = {
        "Revenues": (1000, 700, 800, 450, 520, 270),
        "OperatingIncomeLoss": (200, 140, 170, 90, 110, 60),
        "DepreciationDepletionAndAmortization": (50, 37, 40, 25, 27, 14),
        "NetIncomeLoss": (120, 80, 100, 50, 64, 34),
        "PaymentsToAcquirePropertyPlantAndEquipment": (30, 20, 25, 13, 16, 8),
    }
    for c, (fy, ytd9_24, ytd9_25, ytd6_24, ytd6_25, q2_25) in flows.items():
        rows.append(_dur(c, "2024-01-01", "2024-12-31", fy, K24))
        rows.append(_dur(c, "2024-01-01", "2024-09-30", ytd9_24, Q3_24))
        rows.append(_dur(c, "2023-01-01", "2023-09-30", ytd9_24 * 0.9, Q3_24))  # prior-year comparative
        rows.append(
            _dur(c, "2023-01-01", "2023-12-31", fy * 0.9, K24)
        )  # FY2023 comparative in the FY2024 10-K
        rows.append(_dur(c, "2024-01-01", "2024-06-30", ytd6_24, Q2_25))  # prior-year comparative
        rows.append(_dur(c, "2025-01-01", "2025-06-30", ytd6_25, Q2_25))
        rows.append(
            _dur(c, "2025-04-01", "2025-06-30", q2_25, Q2_25)
        )  # three-month figure, same end as 6M YTD
        rows.append(
            _dur(c, "2024-01-01", "2024-09-30", ytd9_24, Q3_25)
        )  # comparative again in the Q3 2025 10-Q
        rows.append(_dur(c, "2025-01-01", "2025-09-30", ytd9_25, Q3_25))
    # Balance sheet at each period end.
    for end, filing, cash, sti, ltd, cur in (
        ("2024-12-31", K24, 150, 50, 400, 20),
        ("2025-06-30", Q2_25, 160, 40, 390, 20),
        ("2025-09-30", Q3_25, 170, 30, 380, 20),
    ):
        rows += [
            _inst("CashAndCashEquivalentsAtCarryingValue", end, cash, filing),
            _inst("ShortTermInvestments", end, sti, filing),
            _inst("LongTermDebtNoncurrent", end, ltd, filing),
            _inst("LongTermDebtCurrent", end, cur, filing),
        ]
    rows.append(("dei", "EntityCommonStockSharesOutstanding", "shares", None, "2025-10-28", 50 * M, *Q3_25))
    rows.append(("dei", "EntityCommonStockSharesOutstanding", "shares", None, "2025-07-29", 51 * M, *Q2_25))
    return rows


def company_facts(rows: list[Row], cik: int = CIK) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for tax, concept, unit, start, end, val, accn, form, filed in rows:
        entry: dict[str, Any] = {"end": end, "val": val, "accn": accn, "fy": int(filed[:4]), "fp": "FY",
                                 "form": form, "filed": filed}  # fmt: skip
        if start:
            entry["start"] = start
        facts.setdefault(tax, {}).setdefault(concept, {"label": concept, "units": {}})["units"].setdefault(
            unit, []
        ).append(entry)
    return {"cik": cik, "entityName": NAME, "facts": facts}


def submissions(cik: int = CIK) -> dict[str, Any]:
    filings = [K24, Q2_25, Q3_25, ("0000000001-25-000009", "8-K", "2025-09-01")]
    return {
        "cik": str(cik),
        "name": NAME,
        "tickers": [TICKER],
        "sic": "3990",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [f[0] for f in filings],
                "form": [f[1] for f in filings],
                "filingDate": [f[2] for f in filings],
                "reportDate": ["2024-12-31", "2025-06-30", "2025-09-30", ""],
                "primaryDocument": ["exw-10k.htm", "exw-10q.htm", "exw-10q.htm", ""],
            }
        },
    }


def tickers() -> dict[str, Any]:
    return {"0": {"cik_str": CIK, "ticker": TICKER, "title": NAME}}


def stooq_csv(days: list[tuple[str, float]]) -> str:
    lines = ["Date,Open,High,Low,Close,Volume"]
    lines += [f"{d},{c - 1},{c + 1},{c - 2},{c},100000" for d, c in days]
    return "\n".join(lines) + "\n"


def fred_csv(series: str, points: list[tuple[str, str]]) -> str:
    return "observation_date," + series + "\n" + "".join(f"{d},{v}\n" for d, v in points)


NY = -18000  # New York in November (EST, UTC-5)


def yahoo_chart(
    ticker: str,
    rows: list[tuple[int, float | None]],
    market_time: int,
    session: tuple[int, int] | None,
    interval: str = "1d",
    adj: bool = True,
    delayed: int = 0,
) -> dict[str, Any]:
    """A Yahoo v8 chart response in the shape the live endpoint returns. Rows are (unix time, close)."""
    meta: dict[str, Any] = {
        "currency": "USD", "symbol": ticker, "exchangeName": "NMS", "gmtoffset": NY,
        "exchangeTimezoneName": "America/New_York", "regularMarketTime": market_time,
        "regularMarketPrice": rows[-1][1] if rows else None, "dataGranularity": interval,
    }  # fmt: skip
    if delayed:
        meta["exchangeDataDelayedBy"] = delayed
    if session:
        meta["currentTradingPeriod"] = {"regular": {"start": session[0], "end": session[1], "gmtoffset": NY}}
    closes = [c for _, c in rows]
    quote = {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(rows)}
    ind: dict[str, Any] = {"quote": [quote]}
    if adj:
        ind["adjclose"] = [{"adjclose": [None if c is None else c * 0.98 for c in closes]}]
    return {"chart": {"result": [{"meta": meta, "timestamp": [t for t, _ in rows], "indicators": ind}],
                      "error": None}}  # fmt: skip


def yahoo_news(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"explains": [], "count": len(items), "quotes": [], "news": items}


def as_bytes(doc: Any) -> bytes:
    return doc.encode() if isinstance(doc, str) else json.dumps(doc).encode()
