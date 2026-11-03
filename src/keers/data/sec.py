"""SEC EDGAR sources: the ticker map, company submissions (filing index) and XBRL company facts.

SEC asks every client to send a User-Agent naming the organisation and a contact, and to stay under 10 requests a
second. The User-Agent comes from configuration (``SEC_USER_AGENT``); it is never hard-coded here.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET  # noqa: S405 (only for its ParseError)
from collections.abc import Iterable
from datetime import date
from typing import Any

from . import fundamentals as F
from . import lines, own_lines
from .http import Fetcher, FetchError, Response
from .models import Fact, Filing
from .store import Store
from .xbrl import Instance, InstanceError, fetch_instance

# Debt lines whose blanks on the face balance sheet are recorded, for companies in company_lines.toml.
LINE_WATCH = set(F.DEBT_CONCEPTS + F.DEBT_DUE_12M)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"}
CATCH_UP_FORMS = ("10-K", "10-Q")  # periodic filings read from their own instance when company facts lags
CATCH_UP_MAX = 3  # at most this many filings per company per refresh (newest first)


class ParseError(ValueError):
    """A document did not have the shape we expect. Raised rather than guessing."""


def _json(resp: Response) -> Any:
    try:
        return json.loads(resp.body)
    except json.JSONDecodeError as e:
        raise ParseError(f"{resp.url}: not JSON ({e})") from e


def parse_tickers(doc: Any) -> dict[str, tuple[int, str]]:
    """``company_tickers.json`` → {TICKER: (cik, name)}."""
    if not isinstance(doc, dict):
        raise ParseError("company_tickers.json: expected an object")
    out: dict[str, tuple[int, str]] = {}
    for row in doc.values():
        try:
            out[str(row["ticker"]).upper()] = (int(row["cik_str"]), str(row["title"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(f"company_tickers.json: bad row {row!r}") from e
    return out


def parse_submissions(doc: Any) -> tuple[dict[str, Any], list[Filing]]:
    """Submissions JSON → (company info, recent periodic filings). Only the ``recent`` block is read."""
    try:
        cik = int(doc["cik"])
        recent = doc["filings"]["recent"]
        cols = [recent[k] for k in ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")]
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f"submissions: missing field ({e})") from e
    if len({len(c) for c in cols}) != 1:
        raise ParseError("submissions: column lengths differ")
    filings = [
        Filing(cik, accn, form, date.fromisoformat(fd), date.fromisoformat(rd) if rd else None, pd or None)
        for accn, form, fd, rd, pd in zip(*cols, strict=True)
        if form in FORMS
    ]
    info = {
        "cik": cik,
        "name": str(doc.get("name", "")),
        "tickers": [str(t).upper() for t in doc.get("tickers", [])],
        "sic": doc.get("sic") or None,
        "fiscalYearEnd": doc.get("fiscalYearEnd") or None,
    }
    return info, filings


def parse_company_facts(doc: Any, raw_sha256: str, concepts: Iterable[str] | None = None) -> list[Fact]:
    """Company facts JSON → flat list of facts. ``concepts`` limits which ``taxonomy:Concept`` names are kept."""
    keep = set(concepts) if concepts is not None else None
    try:
        cik = int(doc["cik"])
        taxonomies = doc["facts"]
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f"companyfacts: missing field ({e})") from e
    out: list[Fact] = []
    for tax, concepts_doc in taxonomies.items():
        for concept, body in concepts_doc.items():
            if keep is not None and f"{tax}:{concept}" not in keep:
                continue
            for unit, rows in body.get("units", {}).items():
                for r in rows:
                    try:
                        out.append(
                            Fact(
                                cik=cik,
                                taxonomy=tax,
                                concept=concept,
                                unit=unit,
                                start=date.fromisoformat(r["start"]) if r.get("start") else None,
                                end=date.fromisoformat(r["end"]),
                                value=float(r["val"]),
                                accn=str(r["accn"]),
                                form=str(r.get("form", "")),
                                filed=date.fromisoformat(r["filed"]),
                                fy=int(r["fy"]) if r.get("fy") is not None else None,
                                fp=r.get("fp"),
                                frame=r.get("frame"),
                                raw_sha256=raw_sha256,
                            )
                        )
                    except (KeyError, TypeError, ValueError) as e:
                        raise ParseError(f"companyfacts {tax}:{concept}: bad row {r!r}") from e
    return out


def concept_inventory(doc: Any, pattern: str, since: date | None = None) -> list[dict[str, Any]]:
    """Which concepts a company files whose names match ``pattern`` (a regex), with the latest period of each.

    For diagnosis only: it shows which tags a company actually uses so the candidate lists can be extended.
    """
    import re

    rx = re.compile(pattern)
    out = []
    for tax, concepts_doc in doc.get("facts", {}).items():
        for concept, body in concepts_doc.items():
            if not rx.search(concept):
                continue
            for unit, rows in body.get("units", {}).items():
                last = max(rows, key=lambda r: (r["end"], r["filed"]))
                end = date.fromisoformat(last["end"])
                if since and end < since:
                    continue
                out.append({"concept": f"{tax}:{concept}", "unit": unit, "end": last["end"],
                            "start": last.get("start"), "value": last["val"], "form": last.get("form"), "n": len(rows)})  # fmt: skip
    return sorted(out, key=lambda r: r["concept"])


def _unit(raw: str) -> str:
    """An instance unit in company-facts spelling: ``iso4217:USD/xbrli:shares`` → ``USD/shares``."""
    return "/".join(m.split(":")[-1] for m in raw.split("/"))


def instance_to_facts(inst: Instance, cik: int, concepts: Iterable[str] | None) -> list[Fact]:
    """The standard (us-gaap, dei, srt) facts in a filing's instance that company facts would carry: those without
    dimensions and with a value, limited to ``concepts``. Values are as filed; the filing date is the filing's."""
    keep = set(concepts) if concepts is not None else None
    out = []
    for f in inst.facts:
        if f.dims or f.prefix not in ("us-gaap", "dei", "srt") or f.value != f.value:  # NaN: tagged nil
            continue
        if keep is not None and f"{f.prefix}:{f.concept}" not in keep:
            continue
        out.append(Fact(cik, f.prefix, f.concept, _unit(f.unit), f.start, f.end, f.value, inst.accn, inst.form,
                        inst.filed, raw_sha256=inst.sha256))  # fmt: skip
    return out


def missing_filings(store: Store, cik: int, filings: Iterable[Filing]) -> list[Filing]:
    """10-Qs and 10-Ks filed after the newest fact in the store that no stored fact comes from, newest first.

    Company facts sometimes lags a filing by months (Visa's 10-Q of 29 July 2026 was not in it on 3 November), which
    left the latest accounts looking stale. Only filings newer than every stored fact count, so a filing that
    company facts carries under another accession, or an old one it never carried, is not fetched again and again.
    """
    row = store.db.execute("SELECT MAX(filed) FROM facts WHERE cik=?", (cik,)).fetchone()
    newest = date.fromisoformat(row[0]) if row and row[0] else None
    if newest is None:
        return []
    out = []
    for f in sorted(filings, key=lambda f: f.filing_date, reverse=True):
        if f.form not in CATCH_UP_FORMS or f.filing_date <= newest:
            continue
        if store.db.execute("SELECT 1 FROM facts WHERE cik=? AND accn=? LIMIT 1", (cik, f.accn)).fetchone():
            continue
        out.append(f)
    return out[:CATCH_UP_MAX]


class SEC:
    """Fetches SEC documents, stores the raw bytes, and writes parsed records to the store."""

    def __init__(self, fetcher: Fetcher, store: Store) -> None:
        self.fetcher = fetcher
        self.store = store
        self._tickers: dict[str, tuple[int, str]] | None = None

    def _get(self, url: str) -> Response:
        resp = self.fetcher.get(url)
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        return resp

    def tickers(self) -> dict[str, tuple[int, str]]:
        if self._tickers is None:
            self._tickers = parse_tickers(_json(self._get(TICKERS_URL)))
        return self._tickers

    def cik(self, ticker: str) -> int:
        hit = self.tickers().get(ticker.upper())
        if hit is None:
            raise KeyError(f"{ticker}: not in the SEC ticker map")
        return hit[0]

    def refresh_company(self, ticker: str, concepts: Iterable[str] | None = None) -> dict[str, Any]:
        return self.refresh_cik(self.cik(ticker), concepts, ticker)

    def refresh_cik(
        self, cik: int, concepts: Iterable[str] | None = None, ticker: str | None = None
    ) -> dict[str, Any]:
        """Filings and facts for one CIK. Without ``ticker``, the company is stored under its first SEC ticker
        (or ``CIK<number>``), so a linked home-market listing never takes over the SEC filer's own row."""
        info, filings = parse_submissions(_json(self._get(SUBMISSIONS_URL.format(cik=cik))))
        ticker = ticker or (info["tickers"][0] if info["tickers"] else f"CIK{cik}")
        self.store.put_company(cik, ticker, info["name"], info["sic"], info["fiscalYearEnd"])
        self.store.put_filings(filings)
        resp = self._get(FACTS_URL.format(cik=cik))
        own = own_lines.for_cik(cik)
        if own is not None and concepts is not None:
            concepts = list(concepts) + own.concepts()
        facts = parse_company_facts(_json(resp), resp.sha256, concepts)
        added = self.store.put_facts(facts)
        out: dict[str, Any] = {
            "ticker": ticker.upper(),
            "cik": cik,
            "forms": sorted({f.form for f in filings}),
            "filings": len(filings),
            "facts": len(facts),
            "newFacts": added,
        }
        # Filings company facts does not carry yet are read from their own XBRL instance. On failure they stay
        # unread, so the accounts stay as old as company facts left them (and may read as stale): never guessed.
        for f in missing_filings(self.store, cik, filings):
            try:
                inst = fetch_instance(self.fetcher, cik, f.accn, f.form, f.filing_date)
                got = instance_to_facts(inst, cik, concepts)
                if not got:
                    raise InstanceError("no standard facts in the instance")
                self.store.put_facts(got)
                out.setdefault("fromInstance", []).append({"filing": f.accn, "form": f.form,
                                                           "filed": f.filing_date.isoformat(), "facts": len(got)})  # fmt: skip
            except (FetchError, InstanceError, ET.ParseError) as e:
                out.setdefault("fromInstanceErrors", []).append(f"{f.form} {f.accn}: {str(e)[:200]}")
        if cik in lines.config():
            # A failure here leaves the company's own lines unread, so its debt reads as unknown: never zero.
            try:
                out["companyLines"] = lines.refresh_lines(self.fetcher, self.store, cik, LINE_WATCH)
            except (FetchError, InstanceError, ValueError, ET.ParseError) as e:
                out["companyLinesError"] = str(e)
        if own is not None and own.extension_concepts():
            # Reviewed company-specific lines (own_lines.toml) that company facts leaves out. On failure they stay
            # unread, so the line they stand for reads as unknown.
            try:
                out["ownLines"] = own_lines.refresh_sec(self.fetcher, self.store, cik)
            except (FetchError, InstanceError, ValueError, ET.ParseError) as e:
                out["ownLinesError"] = str(e)
        return out
