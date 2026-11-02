"""ESEF annual reports from filings.xbrl.org, read as xBRL-JSON.

Every listed company in the EU and the UK publishes its annual financial report in ESEF: inline XBRL tagged with
the IFRS taxonomy (``ifrs-full``). filings.xbrl.org, run by XBRL International, collects these reports from the
national registers, validates them and publishes each one as xBRL-JSON. It is free and needs no key.

What this gives, and what it does not:

- Annual figures only. Half-year and quarterly reports are not ESEF, so the newest figures can be up to about
  16 months old (a 31 December year end is published by the end of April). Snapshots say so.
- Consolidated IFRS figures as tagged by the company. Only ``ifrs-full`` concepts without dimensions are read;
  a company's own extension concepts are left out, so a line it tags only with its own concept stays unknown.
- ``filed`` is the day filings.xbrl.org added the report, which can be days or weeks after the company published
  it. Point-in-time views therefore see a report a little late, never early.

Rules kept here: a report is used only if it is for the LEI asked about; a fact for another entity, with
dimensions, or with a value that is not a number is left out; two different values for the same line, period and
unit in one report are both dropped (the report contradicts itself) and the parse report says so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urljoin

from .http import Fetcher, FetchError, Response
from .models import Fact, Filing
from .sec import ParseError
from .store import Store

BASE = "https://filings.xbrl.org"
# Tried in order. The API is JSON:API; which filter form it accepts is checked by the live probe, and every
# result is checked against the LEI asked for, so a filter the server ignores cannot return another company.
FILINGS_BY_ENTITY = BASE + "/api/entities/{lei}/filings?page%5Bsize%5D=100"
FILINGS_FILTERED = BASE + "/api/filings?filter={flt}&include=entity&page%5Bsize%5D=100"
MAX_PAGES = 5
ENTITIES = BASE + "/api/entities?page%5Bsize%5D=200"
# The filer list is about 7,500 entities; this bounds the walk if the site's paging misbehaves.
MAX_ENTITY_PAGES = 200
FORM = "ESEF"

PLAIN_DIMENSIONS = {"concept", "entity", "period", "unit", "language", "noteId"}
LEI_RX = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def is_lei(s: str) -> bool:
    return bool(LEI_RX.match(s.strip().upper()))


@dataclass(frozen=True)
class EsefFiling:
    """One report on filings.xbrl.org."""

    fxo_id: str
    lei: str
    country: str
    period_end: date
    added: date
    json_url: str
    viewer_url: str
    errors: int | None = None

    def as_filing(self, entity_id: int) -> Filing:
        return Filing(
            entity_id, self.fxo_id, FORM, self.added, self.period_end, self.viewer_url or self.json_url
        )


def _abs(u: Any) -> str:
    return urljoin(BASE + "/", str(u)) if u else ""


def _day(s: Any) -> date:
    return date.fromisoformat(str(s)[:10])


def parse_filings(doc: Any, lei: str) -> list[EsefFiling]:
    """JSON:API list of filings -> the ones for ``lei``. Anything that is not clearly for that LEI is left out."""
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ParseError("filings.xbrl.org: not a JSON:API list")
    lei = lei.upper()
    entities = {
        (str(x.get("type")), str(x.get("id"))): (x.get("attributes") or {}).get("identifier")
        for x in doc.get("included") or []
        if isinstance(x, dict)
    }
    out = []
    for d in doc["data"]:
        if not isinstance(d, dict):
            continue
        a = d.get("attributes") or {}
        rel = (((d.get("relationships") or {}).get("entity") or {}).get("data")) or {}
        ident = entities.get((str(rel.get("type")), str(rel.get("id"))))
        fxo = str(a.get("fxo_id") or "")
        json_url = _abs(a.get("json_url"))
        # The LEI must be stated: by the included entity, or as the first part of the report id or its path.
        named = {str(ident or "").upper(), fxo.split("-", 1)[0].upper()}
        if lei not in named and f"/{lei}/" not in json_url.upper():
            continue
        if not json_url or not a.get("period_end") or not a.get("date_added"):
            continue
        errors = a.get("error_count")
        out.append(EsefFiling(
            fxo or json_url, lei, str(a.get("country") or ""), _day(a["period_end"]), _day(a["date_added"]),
            json_url, _abs(a.get("viewer_url") or a.get("report_url")),
            int(errors) if isinstance(errors, int | float) else None,
        ))  # fmt: skip
    return out


def latest_per_period(filings: Iterable[EsefFiling], periods: int) -> list[EsefFiling]:
    """The most recently added report for each of the last ``periods`` period ends, newest first."""
    best: dict[date, EsefFiling] = {}
    for f in filings:
        if f.period_end not in best or (f.added, f.fxo_id) > (
            best[f.period_end].added,
            best[f.period_end].fxo_id,
        ):
            best[f.period_end] = f
    return [best[k] for k in sorted(best, reverse=True)[:periods]]


# xBRL-JSON --------------------------------------------------------------------------------------------------------
def _end_date(s: str) -> date:
    """An xBRL-JSON period end. A datetime at midnight is the end of the day before (OIM convention)."""
    s = s.strip().rstrip("Z")
    if "T" not in s:
        return date.fromisoformat(s)
    dt = datetime.fromisoformat(s)
    return dt.date() - timedelta(days=1) if dt.time() == datetime.min.time() else dt.date()


def _start_date(s: str) -> date:
    return date.fromisoformat(s.strip().rstrip("Z")[:10])


def _unit(u: str | None) -> str | None:
    """'iso4217:EUR' -> 'EUR', 'xbrli:shares' -> 'shares', 'iso4217:EUR/xbrli:shares' -> 'EUR/shares' (per-share
    amounts, as SEC companyfacts writes them). Other ratios and products are not read (None)."""
    if not u or "*" in u:
        return None
    if "/" in u:
        num, _, den = u.partition("/")
        cur = _unit(num)
        return f"{cur}/shares" if cur and cur != "shares" and _unit(den) == "shares" else None
    local = u.split(":", 1)[-1]
    if local == "shares":
        return "shares"
    return local.upper() if re.fullmatch(r"[A-Za-z]{3}", local) else None


def _prefixes(doc: dict[str, Any]) -> dict[str, str]:
    """Prefix -> taxonomy name. Any prefix bound to the IFRS Foundation's ifrs-full namespace becomes 'ifrs-full'."""
    ns = (doc.get("documentInfo") or {}).get("namespaces") or {}
    out = {}
    for prefix, uri in ns.items():
        if re.search(r"xbrl\.ifrs\.org/taxonomy/[^/]+/ifrs-full$", str(uri)):
            out[prefix] = "ifrs-full"
        elif str(uri) == "http://standards.iso.org/iso/17442":
            out[prefix] = "lei"
    return out


@dataclass
class ParseReport:
    facts: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    contradictions: list[str] = field(default_factory=list)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {"facts": self.facts, "skipped": dict(sorted(self.skipped.items())),
                "contradictions": self.contradictions[:20]}  # fmt: skip


def _decimals(d: Any) -> float:
    if d is None or str(d).upper() == "INF":
        return float("inf")
    return float(d)


def _consistent(values: list[tuple[Decimal, float]]) -> Decimal | None:
    """The most precise value if every other one rounds to the same figure at its own precision, else None."""
    values = sorted(values, key=lambda x: -x[1])
    best = values[0][0]
    for v, d in values[1:]:
        if d == float("inf"):
            if v != best:
                return None
            continue
        q = Decimal(1).scaleb(-int(d))
        if best.quantize(q) != v.quantize(q):
            return None
    return best


def parse_xbrl_json(
    doc: Any,
    lei: str,
    entity_id: int,
    filing: EsefFiling,
    raw_sha256: str,
    concepts: Iterable[str] | None = None,
) -> tuple[list[Fact], ParseReport]:
    """xBRL-JSON report -> facts for the entity, plus a report of what was left out and why."""
    if not isinstance(doc, dict) or not isinstance(doc.get("facts"), dict):
        raise ParseError(f"{filing.json_url}: not an xBRL-JSON report")
    keep = set(concepts) if concepts is not None else None
    prefixes = _prefixes(doc)
    rep = ParseReport()
    groups: dict[tuple[str, str, str, date | None, date], list[tuple[Decimal, float]]] = {}
    for body in doc["facts"].values():
        dims = (body or {}).get("dimensions") or {}
        concept = str(dims.get("concept") or "")
        if ":" not in concept:
            rep.skip("no concept")
            continue
        prefix, name = concept.split(":", 1)
        tax = prefixes.get(prefix)
        if tax != "ifrs-full":
            rep.skip("not an ifrs-full concept")
            continue
        if keep is not None and f"{tax}:{name}" not in keep:
            rep.skip("concept not used")
            continue
        if set(dims) - PLAIN_DIMENSIONS:
            rep.skip("has dimensions")
            continue
        ent = str(dims.get("entity") or "")
        ep, _, ident = ent.partition(":")
        if prefixes.get(ep) != "lei" or ident.upper() != lei.upper():
            rep.skip("other entity")
            continue
        unit = _unit(dims.get("unit"))
        if unit is None:
            rep.skip("no usable unit")
            continue
        try:
            val = Decimal(str(body.get("value")))
        except (InvalidOperation, ValueError):
            rep.skip("not a number")
            continue
        if not val.is_finite():
            rep.skip("not a number")
            continue
        period = str(dims.get("period") or "")
        try:
            if "/" in period:
                a, b = period.split("/", 1)
                start: date | None = _start_date(a)
                end = _end_date(b)
            else:
                start, end = None, _end_date(period)
        except ValueError:
            rep.skip("bad period")
            continue
        groups.setdefault((tax, name, unit, start, end), []).append((val, _decimals(body.get("decimals"))))
    facts = []
    for (tax, name, unit, start, end), vals in groups.items():
        v = _consistent(vals)
        if v is None:
            rep.contradictions.append(
                f"{tax}:{name} {start or ''}..{end} {unit}: {sorted({str(x) for x, _ in vals})}"
            )
            continue
        facts.append(Fact(entity_id, tax, name, unit, start, end, float(v), filing.fxo_id, FORM, filing.added,
                          raw_sha256=raw_sha256))  # fmt: skip
    rep.facts = len(facts)
    return facts, rep


def parse_entities(doc: Any) -> list[tuple[str, str]]:
    """JSON:API list of entities -> ``(lei, name)``. Entries without a valid LEI or a name are left out."""
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ParseError("filings.xbrl.org: not a JSON:API list")
    out = []
    for d in doc["data"]:
        a = (d or {}).get("attributes") or {} if isinstance(d, dict) else {}
        lei, name = str(a.get("identifier") or "").upper(), str(a.get("name") or "").strip()
        if is_lei(lei) and name:
            out.append((lei, name))
    return out


class Esef:
    """Finds an LEI's reports on filings.xbrl.org and stores their facts. Raw responses are stored by hash."""

    def __init__(self, fetcher: Fetcher, store: Store) -> None:
        self.fetcher, self.store = fetcher, store

    def _get(self, url: str) -> Response:
        resp = self.fetcher.get(url, headers={"Accept": "application/vnd.api+json, application/json"})
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        return resp

    def _pages(self, url: str, lei: str) -> list[EsefFiling]:
        out: list[EsefFiling] = []
        for _ in range(MAX_PAGES):
            doc = _json(self._get(url))
            out += parse_filings(doc, lei)
            nxt = (doc.get("links") or {}).get("next")
            if not nxt or not doc.get("data"):
                break
            url = _abs(nxt)
        return out

    def entities(self) -> list[tuple[str, str]]:
        """Every filer on filings.xbrl.org as ``(lei, name)``, walking the pages."""
        out: list[tuple[str, str]] = []
        url = ENTITIES
        for _ in range(MAX_ENTITY_PAGES):
            doc = _json(self._get(url))
            out += parse_entities(doc)
            nxt = (doc.get("links") or {}).get("next")
            if not nxt or not doc.get("data"):
                break
            url = _abs(nxt)
        return out

    def filings(self, lei: str) -> tuple[list[EsefFiling], str]:
        """Every report for ``lei``, and which query found them. An empty list means none were found."""
        lei = lei.upper()
        flt = json.dumps([{"name": "entity", "op": "has", "val": {"name": "identifier", "op": "eq", "val": lei}}],
                         separators=(",", ":"))  # fmt: skip
        errors = []
        for how, url in (
            ("entity", FILINGS_BY_ENTITY.format(lei=lei)),
            ("filter", FILINGS_FILTERED.format(flt=quote(flt))),
        ):
            try:
                found = self._pages(url, lei)
            except (FetchError, ParseError) as e:
                errors.append(f"{how}: {e}")
                continue
            if found:
                return found, how
        if errors:
            raise FetchError(f"filings.xbrl.org {lei}: " + "; ".join(errors))
        return [], "none"

    def refresh(
        self, lei: str, entity_id: int, concepts: Iterable[str] | None = None, periods: int = 3
    ) -> dict[str, Any]:
        """Store the facts of the latest report for each of the last ``periods`` years."""
        found, how = self.filings(lei)
        chosen = latest_per_period(found, periods)
        self.store.put_filings(f.as_filing(entity_id) for f in found)
        reports = []
        added = 0
        concepts = list(concepts) if concepts is not None else None
        for f in chosen:
            resp = self._get(f.json_url)
            facts, rep = parse_xbrl_json(_json(resp), lei, entity_id, f, resp.sha256, concepts)
            added += self.store.put_facts(facts)
            reports.append({"fxoId": f.fxo_id, "periodEnd": f.period_end.isoformat(), "added": f.added.isoformat(),
                            "country": f.country, "validationErrors": f.errors, **rep.as_dict()})  # fmt: skip
        return {"lei": lei, "query": how, "filings": len(found), "read": reports, "newFacts": added}


def _json(resp: Response) -> Any:
    try:
        return json.loads(resp.body)
    except ValueError as e:
        raise ParseError(f"{resp.url}: not JSON") from e
