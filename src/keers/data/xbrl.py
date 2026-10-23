"""A filing's own XBRL instance: the facts the SEC company-facts API leaves out.

Company facts (``data.sec.gov/api/xbrl/companyfacts``) carry only standard concepts (us-gaap, dei, srt) and only
facts without dimensions. A company that files its debt under its own concept (for example ``dbx:TermLoan...``)
or only per debt instrument (a dimension) therefore looks as if it had less debt than it does. EDGAR publishes
the instance document of every inline-XBRL filing (``<primary document>_htm.xml``); this module reads it.

Only the standard library is used. Values are taken exactly as filed: XBRL instance values are already in units
(no scaling) and already carry their sign.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET  # noqa: S405 (SEC filings; expat does not fetch external entities)
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .http import Fetcher

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/index.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{name}"
PERIODIC = ("10-Q", "10-K", "10-Q/A", "10-K/A")

# Namespaces of standard taxonomies and of XBRL itself. Anything else that carries facts is the company's own.
STANDARD_NS = {
    "fasb.org/us-gaap": "us-gaap",
    "xbrl.sec.gov/dei": "dei",
    "fasb.org/srt": "srt",
}
INFRA_HOSTS = ("xbrl.org", "w3.org", "xbrl.sec.gov", "fasb.org")


class InstanceError(ValueError):
    """The filing has no readable XBRL instance. Raised rather than returning an empty list of facts."""


@dataclass(frozen=True)
class InstanceFact:
    prefix: str  # "us-gaap", "dei", "srt" or the company's own prefix (e.g. "dbx")
    concept: str
    unit: str  # "USD", "shares", or the raw measure
    start: date | None
    end: date
    value: float
    dims: tuple[tuple[str, str], ...]  # (axis, member) pairs; empty for the face statements' totals
    context: str


@dataclass
class Instance:
    url: str
    accn: str
    form: str
    filed: date
    sha256: str
    facts: list[InstanceFact] = field(default_factory=list)
    extension_prefixes: set[str] = field(default_factory=set)


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _standard(ns: str) -> str | None:
    for key, prefix in STANDARD_NS.items():
        if key in ns:
            return prefix
    return None


def parse_instance(body: bytes) -> tuple[list[InstanceFact], set[str]]:
    """Instance XML → (numeric facts, the company's own prefixes). Text and nil facts are skipped."""
    prefixes: dict[str, str] = {}
    try:
        events = ET.iterparse(io.BytesIO(body), events=("start-ns",))  # noqa: S314
        for _, (p, uri) in events:
            prefixes.setdefault(uri, p)
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError as e:
        raise InstanceError(f"instance is not XML: {e}") from e
    contexts: dict[str, tuple[date | None, date, tuple[tuple[str, str], ...]]] = {}
    units: dict[str, str] = {}
    for el in root:
        ns, name = _local(el.tag)
        if name == "context":
            start = end = None
            dims: list[tuple[str, str]] = []
            for sub in el.iter():
                _, n = _local(sub.tag)
                text = (sub.text or "").strip()
                if n == "instant":
                    end = date.fromisoformat(text)
                elif n == "startDate":
                    start = date.fromisoformat(text)
                elif n == "endDate":
                    end = date.fromisoformat(text)
                elif n in ("explicitMember", "typedMember"):
                    member = text or "".join((c.text or "").strip() for c in sub)
                    dims.append((sub.get("dimension", ""), member))
            if end is not None:
                contexts[el.get("id", "")] = (start, end, tuple(sorted(dims)))
        elif name == "unit":
            measures = [(m.text or "").strip() for m in el.iter() if _local(m.tag)[1] == "measure"]
            if len(measures) == 1:
                m = measures[0].split(":")[-1]
                units[el.get("id", "")] = "USD" if m == "USD" else m
            else:
                units[el.get("id", "")] = "/".join(measures)
    out: dict[tuple[str, str, str, str], InstanceFact] = {}
    ext: set[str] = set()
    for el in root:
        ctx = el.get("contextRef")
        if ctx is None or ctx not in contexts:
            continue
        ns, name = _local(el.tag)
        if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
            continue
        try:
            value = float((el.text or "").strip())
        except ValueError:
            continue
        prefix = _standard(ns)
        if prefix is None:
            if any(h in ns for h in INFRA_HOSTS):
                continue
            prefix = prefixes.get(ns) or ns
            ext.add(prefix)
        start, end, dims = contexts[ctx]
        unit = units.get(el.get("unitRef", ""), "")
        key = (prefix, name, ctx, unit)
        out.setdefault(key, InstanceFact(prefix, name, unit, start, end, value, dims, ctx))
    return list(out.values()), ext


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise InstanceError(f"{what}: not JSON ({e})") from e


def instance_name(index_doc: Any) -> str:
    """The instance file in a filing folder's ``index.json``: EDGAR names it ``<primary>_htm.xml``."""
    try:
        items = [str(i["name"]) for i in index_doc["directory"]["item"]]
    except (KeyError, TypeError) as e:
        raise InstanceError(f"filing index: unexpected shape ({e})") from e
    hits = [n for n in items if n.endswith("_htm.xml")]
    if len(hits) != 1:
        raise InstanceError(f"filing index: expected one *_htm.xml instance, found {hits or 'none'}")
    return hits[0]


def latest_periodic(submissions: Any, forms: tuple[str, ...] = PERIODIC) -> tuple[str, str, date]:
    """(accession, form, filing date) of the newest 10-Q or 10-K in a submissions document."""
    try:
        recent = submissions["filings"]["recent"]
        rows = zip(recent["accessionNumber"], recent["form"], recent["filingDate"], strict=True)
        best = max(((a, f, date.fromisoformat(d)) for a, f, d in rows if f in forms), key=lambda r: r[2])
    except (KeyError, TypeError, ValueError) as e:
        raise InstanceError(f"submissions: no periodic filing found ({e})") from e
    return best


def fetch_instance(fetcher: Fetcher, cik: int, accn: str, form: str, filed: date) -> Instance:
    folder = accn.replace("-", "")
    index = _json(fetcher.get(INDEX_URL.format(cik=cik, folder=folder)).body, "filing index")
    url = DOC_URL.format(cik=cik, folder=folder, name=instance_name(index))
    resp = fetcher.get(url)
    facts, ext = parse_instance(resp.body)
    return Instance(url, accn, form, filed, resp.sha256, facts, ext)


def fetch_latest_instance(fetcher: Fetcher, cik: int) -> Instance:
    subs = _json(fetcher.get(SUBMISSIONS_URL.format(cik=cik)).body, "submissions")
    accn, form, filed = latest_periodic(subs)
    return fetch_instance(fetcher, cik, accn, form, filed)
