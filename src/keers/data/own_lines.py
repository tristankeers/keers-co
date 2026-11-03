"""Company-specific lines that stand for a standard line, read for the companies in ``own_lines.toml`` only.

Some companies tag a line on their face statements with their own (extension) concept, or with a standard concept
other than the one the data layer reads. TotalEnergies tags capital expenditure with its own concept, and Shell
tags it as ``PurchaseOfOtherLongtermAssetsClassifiedAsInvestingActivities``. Without these the line reads as
unknown and the company cannot be valued.

Each line in the config was checked by a person against the filing named under ``reviewed``: the row on the face
statement, its label and its printed figures. A line is used only when the standard concepts give no value for
the period, and every figure taken from one carries a warning that names it. Nothing here is estimated.

Where the facts come from:

- ESEF reports (filings.xbrl.org): the listed concepts are kept when the report is parsed (``esef``).
- SEC 20-F and 40-F filers: standard concepts come from company facts like any other; the company's own concepts
  are not in company facts, so they are read from the latest annual filing's own XBRL instance (``refresh_sec``).
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from .http import Fetcher
from .models import Fact
from .store import Store, is_sec_cik
from .xbrl import SUBMISSIONS_URL, Instance, _json, fetch_instance, latest_periodic

# What a line can stand for, and how Fundamentals uses it.
STANDS_FOR = ("capex", "operating-income", "da", "interest")
STANDARD_PREFIXES = ("ifrs-full", "us-gaap")
ANNUAL_FORMS = ("20-F", "40-F", "20-F/A", "40-F/A", "10-K", "10-K/A")


@dataclass(frozen=True)
class OwnLine:
    concept: str  # "prefix:Name", as tagged in the filing
    stands_for: str
    label: str  # the row's label on the face statement
    sign: int = 1  # -1 when the company files the outflow as a negative number
    note: str = ""  # what the line includes, when it differs from the standard line

    @property
    def prefix(self) -> str:
        return self.concept.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.concept.split(":", 1)[1]

    @property
    def standard(self) -> bool:
        return self.prefix in STANDARD_PREFIXES


@dataclass(frozen=True)
class OwnLines:
    name: str
    lei: str | None
    cik: int | None
    reviewed: str
    lines: tuple[OwnLine, ...]

    def concepts(self) -> list[str]:
        return [ln.concept for ln in self.lines]

    def extension_concepts(self) -> set[str]:
        return {ln.concept for ln in self.lines if not ln.standard}


def parse_config(doc: dict[str, Any]) -> list[OwnLines]:
    out: list[OwnLines] = []
    for c in doc.get("company", []):
        lei, cik = c.get("lei"), c.get("cik")
        if (lei is None) == (cik is None):
            raise ValueError(f"own_lines: {c.get('name')!r} needs exactly one of lei or cik")
        if not str(c.get("reviewed", "")).strip():
            raise ValueError(f"own_lines: {c.get('name')!r} has no reviewed filing")
        lines = []
        for x in c.get("lines", []):
            sign = int(x.get("sign", 1))
            if (
                x.get("stands_for") not in STANDS_FOR
                or ":" not in str(x.get("concept", ""))
                or sign not in (1, -1)
            ):
                raise ValueError(f"own_lines: bad line {x!r}")
            lines.append(
                OwnLine(x["concept"], x["stands_for"], str(x.get("label", "")), sign, str(x.get("note", "")))
            )
        out.append(OwnLines(str(c["name"]), str(lei).upper() if lei else None, int(cik) if cik else None,
                            str(c["reviewed"]), tuple(lines)))  # fmt: skip
    return out


@cache
def config() -> tuple[OwnLines, ...]:
    text = resources.files("keers.data").joinpath("own_lines.toml").read_text()
    return tuple(parse_config(tomllib.loads(text)))


def for_lei(lei: str) -> OwnLines | None:
    return next((c for c in config() if c.lei == lei.upper()), None)


def for_cik(cik: int) -> OwnLines | None:
    return next((c for c in config() if c.cik == cik), None)


def for_entity(store: Store, entity_id: int) -> OwnLines | None:
    """The config entry for a store entity: an SEC CIK, or an ESEF entity found by its LEI."""
    if is_sec_cik(entity_id):
        return for_cik(entity_id)
    ent = store.entity(entity_id)
    if ent is None or ent.get("scheme") != "lei":
        return None
    return for_lei(str(ent["identifier"]))


def instance_facts(inst: Instance, cik: int, own: OwnLines) -> list[Fact]:
    """The configured extension concepts' values in a filing's own instance, without dimensions, in a currency."""
    want = own.extension_concepts()
    out = []
    for f in inst.facts:
        if f.dims or f"{f.prefix}:{f.concept}" not in want or math.isnan(f.value):
            continue
        if not (len(f.unit) == 3 and f.unit.isalpha() and f.unit.isupper()):
            continue
        out.append(Fact(cik, f.prefix, f.concept, f.unit, f.start, f.end, f.value, inst.accn, inst.form, inst.filed,
                        raw_sha256=inst.sha256))  # fmt: skip
    return out


def refresh_sec(fetcher: Fetcher, store: Store, cik: int) -> dict[str, Any] | None:
    """For a configured SEC filer with extension lines, store them from the latest annual filing's instance."""
    own = for_cik(cik)
    if own is None or not own.extension_concepts():
        return None
    subs = _json(fetcher.get(SUBMISSIONS_URL.format(cik=cik)).body, "submissions")
    accn, form, filed = latest_periodic(subs, ANNUAL_FORMS)
    inst = fetch_instance(fetcher, cik, accn, form, filed)
    facts = instance_facts(inst, cik, own)
    store.put_facts(facts)
    found = {f"{f.taxonomy}:{f.concept}" for f in facts}
    return {"filing": inst.accn, "form": inst.form, "facts": len(facts),
            "notFound": sorted(own.extension_concepts() - found)}  # fmt: skip
