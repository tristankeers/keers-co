"""Financial statements for listings outside the SEC's own US tickers: which filer a listing belongs to, and
fetching that filer's accounts.

Two routes, both free and official:

1. **ESEF** (EU and UK listed companies): the listing is linked to a Legal Entity Identifier (LEI), and the
   annual reports for that LEI are read from filings.xbrl.org (``keers.data.esef``).
2. **SEC foreign private issuers** (20-F and 40-F filers): their XBRL facts, often in ``ifrs-full`` and in their
   own reporting currency, come from SEC EDGAR like any SEC filer. A home-market listing (SAP.DE) can be linked to
   the filer's CIK so it uses those accounts.

Linking. A wrong link would put another company's accounts under a share price, so links are made in only two
ways, and every snapshot says which:

- ``manual``: a person ran ``python -m keers.data link SYMBOL --lei ...`` (or ``--cik``).
- ``name-match``: automatic, only when exactly one active LEI record in GLEIF (the official LEI register) has a
  legal or other name that equals the listing's name after normalising legal forms and punctuation and has at
  least one report on filings.xbrl.org. Anything less (no match, several matches, no reports) is not linked;
  the candidates are returned so a person can choose.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .esef import Esef, is_lei
from .http import Fetcher, FetchError, Response
from .sec import SEC, ParseError
from .store import Store, is_sec_cik

GLEIF_RECORD = "https://api.gleif.org/api/v1/lei-records/{lei}"
GLEIF_SEARCH = "https://api.gleif.org/api/v1/lei-records?filter%5Bfulltext%5D={q}&page%5Bsize%5D=20"

# Long legal forms written out, and the short form they are compared as.
LEGAL_FORMS = (
    ("PUBLIC LIMITED COMPANY", "PLC"), ("AKTIENGESELLSCHAFT", "AG"), ("SOCIETE ANONYME", "SA"),
    ("SOCIEDAD ANONIMA", "SA"), ("SOCIETAS EUROPAEA", "SE"), ("NAAMLOZE VENNOOTSCHAP", "NV"),
    ("SOCIETA PER AZIONI", "SPA"), ("AKTIEBOLAGET", "AB"), ("AKTIEBOLAG", "AB"), ("AKTIESELSKAB", "AS"),
    ("ALLMENNAKSJESELSKAP", "ASA"), ("JULKINEN OSAKEYHTIO", "OYJ"), ("OSAKEYHTIO", "OY"),
    ("KOMMANDITGESELLSCHAFT AUF AKTIEN", "KGAA"), ("SOCIETE EUROPEENNE", "SE"), ("LIMITED", "LTD"),
    ("P L C", "PLC"), ("N V", "NV"), ("S A", "SA"), ("S P A", "SPA"), ("A S A", "ASA"), ("A G", "AG"),
)  # fmt: skip
# Legal-form words dropped from the end of a name before comparing.
TRAILING = {"PLC", "AG", "SA", "SE", "NV", "SPA", "AB", "PUBL", "AS", "ASA", "OYJ", "OY", "KGAA", "LTD", "CO",
            "INC", "CORP", "SCA", "SGPS", "SAB", "DE", "CV", "AND", "GMBH",
            # share-class and register suffixes OpenFIGI adds to names ("SIEMENS AG-REG", "NOVO NORDISK A/S-B")
            "REG", "BR", "A", "B", "C", "VTG", "PREF", "ORD", "SHS", "NPV"}  # fmt: skip


def normalise(name: str) -> str:
    """Upper case, accents and punctuation removed, legal forms shortened and dropped from the end."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = " ".join(s.split())
    for long, short in LEGAL_FORMS:
        s = re.sub(rf"\b{long}\b", short, s)
    words = s.split()
    while len(words) > 1 and words[-1] in TRAILING:
        words.pop()
    return " ".join(words)


@dataclass(frozen=True)
class LeiRecord:
    lei: str
    name: str
    other_names: tuple[str, ...]
    country: str
    status: str

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.other_names)

    def as_dict(self) -> dict[str, Any]:
        return {"lei": self.lei, "name": self.name, "otherNames": list(self.other_names), "country": self.country,
                "status": self.status}  # fmt: skip


def parse_lei_records(doc: Any) -> list[LeiRecord]:
    data = doc.get("data") if isinstance(doc, dict) else None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ParseError("GLEIF: not a JSON:API response")
    out = []
    for d in data:
        a = (d or {}).get("attributes") or {}
        ent = a.get("entity") or {}
        lei = str(a.get("lei") or d.get("id") or "").upper()
        name = str((ent.get("legalName") or {}).get("name") or "")
        if not is_lei(lei) or not name:
            continue
        others = tuple(
            str(o.get("name")) for o in ent.get("otherNames") or [] if isinstance(o, dict) and o.get("name")
        )
        out.append(LeiRecord(lei, name, others, str((ent.get("legalAddress") or {}).get("country") or ""),
                             str(ent.get("status") or "")))  # fmt: skip
    return out


def name_matches(listing_name: str, records: Sequence[LeiRecord]) -> list[LeiRecord]:
    """Active records with a name equal to ``listing_name`` once both are normalised."""
    want = normalise(listing_name)
    if not want:
        return []
    return [
        r
        for r in records
        if r.status.upper() in ("ACTIVE", "") and any(normalise(n) == want for n in r.names())
    ]


class Filers:
    def __init__(self, fetcher: Fetcher, store: Store, sec: SEC | None = None) -> None:
        self.fetcher, self.store = fetcher, store
        self.sec = sec or SEC(fetcher, store)
        self.esef = Esef(fetcher, store)

    def _get(self, url: str) -> Response:
        resp = self.fetcher.get(url, headers={"Accept": "application/vnd.api+json"})
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        return resp

    def lei_record(self, lei: str) -> LeiRecord:
        recs = parse_lei_records(_json(self._get(GLEIF_RECORD.format(lei=lei.upper()))))
        if not recs or recs[0].lei != lei.upper():
            raise ParseError(f"GLEIF: no record for {lei}")
        return recs[0]

    def search(self, name: str) -> list[LeiRecord]:
        return parse_lei_records(_json(self._get(GLEIF_SEARCH.format(q=quote(name[:80])))))

    def _entity(self, rec: LeiRecord) -> int:
        cik = self.store.entity_id("lei", rec.lei, rec.name, rec.country or None, "gleif")
        if self.store.company_by_cik(cik) is None:
            self.store.put_company(cik, rec.lei, rec.name, None, None)
        return cik

    def link_lei(self, symbol: str, lei: str, basis: str = "manual", detail: str = "") -> dict[str, Any]:
        if not is_lei(lei):
            raise ValueError(f"{lei!r} is not a valid LEI (20 characters, letters and digits)")
        rec = self.lei_record(lei)
        cik = self._entity(rec)
        self.store.put_link(symbol, cik, basis, detail or f"LEI {rec.lei} ({rec.name})")
        return {"symbol": symbol.upper(), "lei": rec.lei, "name": rec.name, "basis": basis}

    def link_cik(self, symbol: str, cik: int, basis: str = "manual") -> dict[str, Any]:
        """Link a listing to an SEC filer. The filer's own SEC ticker row is created by a refresh if missing."""
        if not is_sec_cik(cik):
            raise ValueError(f"{cik} is not an SEC CIK")
        self.store.put_link(symbol, cik, basis, f"SEC CIK {cik}")
        return {"symbol": symbol.upper(), "cik": cik, "basis": basis}

    def find(self, symbol: str, name: str) -> dict[str, Any]:
        """Try to link ``symbol`` by name. Returns the link, or why not and the candidates to choose from."""
        recs = self.search(name)
        hits = name_matches(name, recs)
        out: dict[str, Any] = {"symbol": symbol.upper(), "name": name, "normalised": normalise(name),
                               "candidates": [r.as_dict() for r in recs[:10]]}  # fmt: skip
        if not hits:
            out["linked"] = False
            out["reason"] = "no active LEI record has exactly this name"
            return out
        with_reports = [r for r in hits if self.esef.filings(r.lei)[0]]
        if len(with_reports) != 1:
            out["linked"] = False
            names = ", ".join(f"{r.lei} ({r.name})" for r in hits)
            out["reason"] = (f"no report on filings.xbrl.org for {names}" if not with_reports else
                             f"{len(with_reports)} LEI records with this name have reports: {names}")  # fmt: skip
            return out
        rec = with_reports[0]
        cik = self._entity(rec)
        self.store.put_link(symbol, cik, "name-match",
                            f"LEI {rec.lei} ({rec.name}): GLEIF name equals listing name {name!r} after normalising")  # fmt: skip
        out |= {"linked": True, "lei": rec.lei, "lei_name": rec.name, "basis": "name-match"}
        return out

    def refresh(
        self, symbol: str, name: str | None = None, concepts: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """Fetch the accounts behind ``symbol``: linking it by name first if it has no link and a name is given."""
        link = self.store.link(symbol)
        out: dict[str, Any] = {"symbol": symbol.upper()}
        if link is None and name:
            out["find"] = self.find(symbol, name)
            link = self.store.link(symbol)
        if link is None:
            raise KeyError(
                f"{symbol}: not linked to a filer; run: python -m keers.data link {symbol} --lei <LEI>"
            )
        out["link"] = {k: link[k] for k in ("basis", "detail")}
        cik = int(link["cik"])
        if is_sec_cik(cik):
            out["filings"] = self.sec.refresh_cik(cik, concepts)
            return out
        ent = self.store.entity(cik)
        if ent is None or ent["scheme"] != "lei":
            raise KeyError(f"{symbol}: linked to unknown entity {cik}")
        out["filings"] = self.esef.refresh(ent["identifier"], cik, concepts)
        return out


def _json(resp: Response) -> Any:
    try:
        return json.loads(resp.body)
    except ValueError as e:
        raise ParseError(f"{resp.url}: not JSON") from e


__all__ = ["Filers", "FetchError", "LeiRecord", "name_matches", "normalise", "parse_lei_records"]
