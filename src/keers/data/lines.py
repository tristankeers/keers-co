"""Company-specific balance-sheet lines and blank face-statement lines, read from a filing's own XBRL instance.

Two gaps in the SEC company-facts API are closed here, for the companies listed in ``company_lines.toml`` only:

1. **Company-specific concepts.** Some companies file a debt or lease line under their own concept (Plug Power's
   ``plug:LongTermDebtNonCurrentExcludingConvertibleNotes``). Company facts omits those, so debt looks smaller
   than it is. Each such line is listed in the config with its role, after a person has read the filing.
2. **Lines shown without a value.** When a debt line is on the face balance sheet with a value at the comparative
   date and none at the current one, the statement shows a dash: the balance is nil. Company facts simply has no
   value, which reads as unknown. A *blank marker* records that the line was presented and empty, so it reads as
   zero, with a warning naming the filing.

Facts are stored with the filing's accession number and filing date, so point-in-time reads still apply.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib import resources
from typing import Any

from .http import Fetcher
from .models import Fact
from .store import Store
from .xbrl import Instance, fetch_latest_instance, fetch_linkbase, parse_statements, statement_roles

ROLES = ("debt-current", "debt-noncurrent", "lease-current", "lease-noncurrent")
BLANK = "keers-blank"  # taxonomy of blank markers; the concept is "prefix:Name" of the empty line


@dataclass(frozen=True)
class Line:
    concept: str  # "prefix:Name"
    role: str
    label: str

    @property
    def prefix(self) -> str:
        return self.concept.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.concept.split(":", 1)[1]


@dataclass(frozen=True)
class CompanyLines:
    ticker: str
    cik: int
    reviewed: str
    lines: tuple[Line, ...]


def parse_config(doc: dict[str, Any]) -> dict[int, CompanyLines]:
    out: dict[int, CompanyLines] = {}
    for c in doc.get("company", []):
        lines = []
        for x in c.get("lines", []):
            if x["role"] not in ROLES or ":" not in x["concept"]:
                raise ValueError(f"company_lines: bad line {x!r}")
            lines.append(Line(x["concept"], x["role"], x.get("label", "")))
        out[int(c["cik"])] = CompanyLines(
            str(c["ticker"]).upper(), int(c["cik"]), str(c["reviewed"]), tuple(lines)
        )
    return out


@cache
def config() -> dict[int, CompanyLines]:
    text = resources.files("keers.data").joinpath("company_lines.toml").read_text()
    return parse_config(tomllib.loads(text))


def balance_dates(inst: Instance) -> list[date]:
    """Balance-sheet dates in the instance: every date with total assets filed without dimensions."""
    return sorted(
        {f.end for f in inst.facts if f.prefix == "us-gaap" and f.concept == "Assets" and not f.dims}
    )


def facts_to_store(
    inst: Instance, cik: int, cfg: CompanyLines, face: set[str], debt_concepts: set[str]
) -> list[Fact]:
    """The configured lines' values, and blank markers for debt lines on the face balance sheet."""
    dates = balance_dates(inst)
    out: list[Fact] = []

    def fact(taxonomy: str, concept: str, end: date, value: float) -> Fact:
        return Fact(cik, taxonomy, concept, "USD", None, end, value, inst.accn, inst.form, inst.filed,
                    raw_sha256=inst.sha256)  # fmt: skip

    by_key: dict[tuple[str, str, date], float] = {}
    for f in inst.facts:
        if not f.dims and f.start is None and f.unit == "USD":
            by_key[(f.prefix, f.concept, f.end)] = f.value
    for line in cfg.lines:
        for d in dates:
            v = by_key.get((line.prefix, line.name, d))
            if v is not None and not math.isnan(v):
                out.append(fact(line.prefix, line.name, d, v))
    watched = {ln.concept for ln in cfg.lines} | debt_concepts
    for concept in sorted(watched & face):
        prefix, name = concept.split(":", 1)
        values = {d: by_key.get((prefix, name, d)) for d in dates}
        if not any(v is not None and not math.isnan(v) for v in values.values()):
            continue  # presented in the template but empty at every date: nothing to say
        for d, v in values.items():
            if v is None or math.isnan(v):
                out.append(fact(BLANK, concept, d, 0.0))
    return out


def refresh_lines(fetcher: Fetcher, store: Store, cik: int, debt_concepts: set[str]) -> dict[str, Any] | None:
    """For a configured company, read the latest periodic filing's instance and store its lines and blanks."""
    cfg = config().get(cik)
    if cfg is None:
        return None
    inst = fetch_latest_instance(fetcher, cik)
    roles = parse_statements(fetch_linkbase(fetcher, cik, inst.accn, "_pre.xml"))
    face = {c for r in statement_roles(roles, "balance") for c in roles[r]}
    if not face:
        raise ValueError(f"{inst.accn}: no face balance sheet found in the presentation linkbase")
    facts = facts_to_store(inst, cik, cfg, face, debt_concepts)
    store.put_facts(facts)
    return {
        "filing": inst.accn,
        "form": inst.form,
        "lines": sum(1 for f in facts if f.taxonomy != BLANK),
        "blanks": sorted({f"{f.concept} at {f.end}" for f in facts if f.taxonomy == BLANK}),
    }
