"""Parsers for the listing sources. Each one raises ``ParseError`` on a document it does not recognise.

- SEC ``company_tickers_exchange.json``: every SEC registrant with a ticker, its CIK and exchange. Official.
- Nasdaq Trader symbol directory: every security on a US exchange, with ETF and test-issue flags. Official.
- OpenFIGI ``/v3/filter``: listings on exchanges worldwide, with Bloomberg's open FIGI identifiers. Free, run by
  Bloomberg under an open licence. It gives no ISIN, currency, country or sector; those come from the exchange
  table or later checks, and are marked as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..data.sec import ParseError
from .exchanges import NASDAQ_TRADER_MIC, local_ticker

SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
OPENFIGI_FILTER_URL = "https://api.openfigi.com/v3/filter"
OPENFIGI_EXCH_CODES_URL = "https://api.openfigi.com/v3/mapping/values/exchCode"


@dataclass(frozen=True)
class SecRow:
    cik: int
    name: str
    ticker: str  # local spelling, BRK.B
    exchange: str  # SEC's label: Nasdaq, NYSE, OTC, CBOE, or "" when SEC gives none


@dataclass(frozen=True)
class UsVenue:
    ticker: str
    name: str
    mic: str
    etf: bool
    test_issue: bool


@dataclass(frozen=True)
class FigiRow:
    figi: str
    composite_figi: str
    share_class_figi: str
    ticker: str  # as OpenFIGI gives it (BT/A, 700, VOLVB)
    name: str
    exch_code: str
    security_type: str
    security_type2: str
    market_sector: str


def parse_sec_tickers_exchange(doc: Any) -> list[SecRow]:
    try:
        fields = [str(f) for f in doc["fields"]]
        data = doc["data"]
        i_cik, i_name, i_t, i_ex = (fields.index(k) for k in ("cik", "name", "ticker", "exchange"))
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f"company_tickers_exchange.json: unexpected shape ({e})") from e
    out = []
    for r in data:
        try:
            out.append(SecRow(int(r[i_cik]), str(r[i_name]).strip(), local_ticker(str(r[i_t])),
                              str(r[i_ex] or "").strip()))  # fmt: skip
        except (IndexError, TypeError, ValueError) as e:
            raise ParseError(f"company_tickers_exchange.json: bad row {r!r}") from e
    if not out:
        raise ParseError("company_tickers_exchange.json: no rows")
    return out


def _pipe_rows(text: str, want: list[str], what: str) -> list[dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ParseError(f"{what}: empty file")
    header = [h.strip() for h in lines[0].split("|")]
    if any(w not in header for w in want):
        raise ParseError(f"{what}: header {header[:8]} lacks {want}")
    if not lines[-1].startswith("File Creation Time"):
        raise ParseError(f"{what}: no 'File Creation Time' footer, so the file may be cut short")
    return [dict(zip(header, ln.split("|"), strict=False)) for ln in lines[1:-1]]


def parse_nasdaq_listed(text: str) -> list[UsVenue]:
    rows = _pipe_rows(text, ["Symbol", "Security Name", "Test Issue", "ETF"], "nasdaqlisted.txt")
    return [UsVenue(local_ticker(r["Symbol"]), r["Security Name"].strip(), "XNAS", r["ETF"] == "Y",
                    r["Test Issue"] == "Y") for r in rows]  # fmt: skip


def parse_other_listed(text: str) -> list[UsVenue]:
    rows = _pipe_rows(
        text, ["ACT Symbol", "Security Name", "Exchange", "ETF", "Test Issue"], "otherlisted.txt"
    )
    out = []
    for r in rows:
        # A venue letter added after this code was written (e.g. "F") keeps the row: its ETF and test-issue
        # flags still count, and the build falls back to SEC's exchange label for the venue.
        mic = NASDAQ_TRADER_MIC.get(r["Exchange"].strip(), "")
        out.append(UsVenue(local_ticker(r["ACT Symbol"]), r["Security Name"].strip(), mic, r["ETF"] == "Y",
                           r["Test Issue"] == "Y"))  # fmt: skip
    return out


def figi_filter_body(exch_code: str, security_type2: str, start: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"exchCode": exch_code, "marketSecDes": "Equity", "securityType2": security_type2}
    if start:
        body["start"] = start
    return body


def parse_figi_page(doc: Any, exch_code: str) -> tuple[list[FigiRow], str | None, int | None]:
    """One page of ``/v3/filter`` → (rows, next-page token, total the server says matched)."""
    if isinstance(doc, dict) and doc.get("error"):
        raise ParseError(f"openfigi {exch_code}: {doc['error']}")
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ParseError(f"openfigi {exch_code}: not a filter response")
    out = []
    for r in doc["data"]:
        try:
            out.append(
                FigiRow(
                    figi=str(r["figi"]),
                    composite_figi=str(r.get("compositeFIGI") or ""),
                    share_class_figi=str(r.get("shareClassFIGI") or ""),
                    ticker=str(r["ticker"]),
                    name=str(r.get("name") or "").strip(),
                    exch_code=str(r.get("exchCode") or ""),
                    security_type=str(r.get("securityType") or ""),
                    security_type2=str(r.get("securityType2") or ""),
                    market_sector=str(r.get("marketSector") or ""),
                )
            )
        except (KeyError, TypeError) as e:
            raise ParseError(f"openfigi {exch_code}: bad row {r!r}") from e
    total = doc.get("total")
    return out, (str(doc["next"]) if doc.get("next") else None), (int(total) if total is not None else None)


def parse_exch_codes(doc: Any) -> set[str]:
    if not isinstance(doc, dict) or not isinstance(doc.get("values"), list):
        raise ParseError("openfigi exchCode values: unexpected shape")
    return {str(v) for v in doc["values"]}
