"""Typed records for the data layer.

Every number that enters Keers & Co is stored with where it came from, when it was retrieved, the period it
covers, its unit and the raw document it was parsed from (by SHA-256). Values derived from several facts keep
the list of facts they were built from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """What sort of number this is. Reported facts and derived figures are never mixed up."""

    REPORTED = "reported"  # as filed by the company
    DERIVED = "derived"  # calculated in code from reported facts
    MARKET = "market"  # a market price or rate


@dataclass(frozen=True)
class Fact:
    """One XBRL fact from an SEC filing, as filed."""

    cik: int
    taxonomy: str  # "us-gaap" or "dei"
    concept: str  # e.g. "Revenues"
    unit: str  # e.g. "USD", "shares"
    start: date | None  # None for balance-sheet (instant) facts
    end: date
    value: float
    accn: str  # SEC accession number of the filing that reported it
    form: str  # "10-K", "10-Q", ...
    filed: date  # when the market could first have known it
    fy: int | None = None
    fp: str | None = None
    frame: str | None = None
    raw_sha256: str = ""

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days + 1

    def cite(self) -> dict[str, Any]:
        return {
            "concept": f"{self.taxonomy}:{self.concept}",
            "value": self.value,
            "unit": self.unit,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat(),
            "form": self.form,
            "accn": self.accn,
            "filed": self.filed.isoformat(),
        }


@dataclass(frozen=True)
class Filing:
    cik: int
    accn: str
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str | None = None


@dataclass(frozen=True)
class PricePoint:
    ticker: str
    day: date
    close: float
    source: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    raw_sha256: str = ""


@dataclass(frozen=True)
class RatePoint:
    series: str  # FRED series id, e.g. "DGS10"
    day: date
    value: float  # in percent, as published
    source: str
    raw_sha256: str = ""


@dataclass(frozen=True)
class Value:
    """A number in a company snapshot, with its provenance and any data-quality warnings."""

    value: float | None
    kind: Kind
    unit: str
    method: str  # how it was obtained, in words
    period_end: date | None = None
    sources: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "kind": self.kind.value,
            "unit": self.unit,
            "method": self.method,
            "periodEnd": self.period_end.isoformat() if self.period_end else None,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }


def missing(unit: str, why: str) -> Value:
    return Value(None, Kind.DERIVED, unit, why, warnings=(why,))


@dataclass
class RunRecord:
    kind: str
    started_at: str
    finished_at: str | None = None
    status: str = "running"
    detail: dict[str, Any] = field(default_factory=dict)
