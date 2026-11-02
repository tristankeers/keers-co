"""Who was in the S&P 500 on a given day, rebuilt from Wikipedia's list of the index and its changes (free).

The page has two tables: today's constituents, and dated changes (ticker added, ticker removed). Walking the
changes backwards from today's list gives the membership on any past date, including companies that were later
taken over, went bust or were demoted. That is what removes most of the survivorship bias from a backtest.

Limits, reported with every result: Wikipedia's change log is maintained by volunteers and is incomplete in
places, so the rebuilt index does not always have exactly 500 names (the count on each date is reported); and a
ticker that two companies have used at different times is ambiguous, so such tickers are flagged.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


class ParseError(ValueError):
    """The page did not have the tables we expect. Raised rather than guessing."""


class _Tables(HTMLParser):
    """Collects the text of every cell of every table with an ``id``, expanding ``rowspan``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[tuple[str, str]]]] = {}
        self._stack: list[str | None] = []  # table ids (None for tables without one)
        self._row: list[tuple[str, str]] | None = None
        self._cell: list[str] | None = None
        self._kind = ""
        self._span = 1
        self._pending: dict[str, dict[int, tuple[int, tuple[str, str]]]] = {}  # col -> (rows left, cell)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            self._stack.append(a.get("id"))
            if a.get("id"):
                self.tables.setdefault(str(a["id"]), [])
        elif not self._stack or self._stack[-1] is None:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._kind = [], tag
            try:
                self._span = max(1, int(a.get("rowspan") or 1))
            except ValueError:
                self._span = 1
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._stack:
            self._stack.pop()
            return
        if not self._stack or self._stack[-1] is None:
            return
        tid = str(self._stack[-1])
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            pend = self._pending.setdefault(tid, {})
            while len(self._row) in pend:
                self._take(pend, len(self._row))
            text = " ".join("".join(self._cell).split())
            cell = (self._kind, text)
            if self._span > 1:
                pend[len(self._row)] = (self._span - 1, cell)
            self._row.append(cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            pend = self._pending.setdefault(tid, {})
            while len(self._row) in pend:
                self._take(pend, len(self._row))
            if self._row:
                self.tables[tid].append(self._row)
            self._row = None

    def _take(self, pend: dict[int, tuple[int, tuple[str, str]]], col: int) -> None:
        left, cell = pend.pop(col)
        assert self._row is not None  # noqa: S101
        self._row.append(cell)
        if left > 1:
            pend[col] = (left - 1, cell)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _ticker(s: str) -> str | None:
    t = s.strip().upper()
    return t if _TICKER.match(t) else None


def _date(s: str) -> date | None:
    s = re.sub(r"\[[^\]]*\]", "", s).strip()  # footnote marks
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class Membership:
    """Index membership over time. ``spells`` maps a ticker to its (start, end, name) periods in the index."""

    as_of: date
    current: dict[str, str]  # ticker -> name, today
    ciks: dict[str, int]  # ticker -> CIK, for today's members (from the constituents table)
    changes: list[dict[str, Any]]
    source_sha256: str = ""
    warnings: list[str] = field(default_factory=list)
    _days: list[date] = field(default_factory=list, repr=False)
    _sets: list[frozenset[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        state = set(self.current)
        days: list[date] = []
        sets: list[frozenset[str]] = []
        # Newest first: the set in force from each change date until the next one.
        for ch in sorted(self.changes, key=lambda c: c["date"], reverse=True):
            days.append(ch["date"])
            sets.append(frozenset(state))
            if ch.get("added"):
                state.discard(ch["added"])
            if ch.get("removed"):
                state.add(ch["removed"])
        self._before_all = frozenset(state)
        self._days = list(reversed(days))
        self._sets = list(reversed(sets))

    def members(self, d: date) -> frozenset[str]:
        i = bisect.bisect_right(self._days, d) - 1
        return self._sets[i] if i >= 0 else self._before_all

    def ever(self, start: date, end: date) -> set[str]:
        out = set(self.members(start))
        for d, s in zip(self._days, self._sets, strict=True):
            if start <= d <= end:
                out |= s
        return out

    def names(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for ch in self.changes:
            if ch.get("removed") and ch.get("removedName"):
                out.setdefault(ch["removed"], ch["removedName"])
        out.update(self.current)
        return out

    def reused(self) -> set[str]:
        """Tickers that left and came back under a different company name (ambiguous, so flagged)."""
        seen: dict[str, set[str]] = {}
        for ch in self.changes:
            for k in ("added", "removed"):
                if ch.get(k) and ch.get(k + "Name"):
                    seen.setdefault(ch[k], set()).add(_norm(ch[k + "Name"]))
        for t, n in self.current.items():
            seen.setdefault(t, set()).add(_norm(n))
        return {t for t, ns in seen.items() if len(ns) > 1}

    def as_dict(self) -> dict[str, Any]:
        return {"asOf": self.as_of.isoformat(), "current": self.current, "ciks": self.ciks,
                "changes": [{**c, "date": c["date"].isoformat()} for c in self.changes],
                "sourceSha256": self.source_sha256, "warnings": self.warnings}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Membership:
        return cls(date.fromisoformat(d["asOf"]), dict(d["current"]), {k: int(v) for k, v in d["ciks"].items()},
                   [{**c, "date": date.fromisoformat(c["date"])} for c in d["changes"]], d.get("sourceSha256", ""),
                   list(d.get("warnings", [])))  # fmt: skip


_SUFFIX = re.compile(
    r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LTD|LIMITED|PLC|HOLDINGS|HOLDING|THE|LLC|LP|NV|SA|AG|DEL|NEW)\b"
)


def _norm(name: str) -> str:
    """A company name reduced for matching: upper case, no punctuation, no legal suffixes."""
    n = name.upper().replace("&", " AND ")
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    n = _SUFFIX.sub(" ", n)
    return " ".join(n.split())


def parse(html: str, as_of: date, sha256: str = "") -> Membership:
    p = _Tables()
    p.feed(html)
    cons, chg = p.tables.get("constituents"), p.tables.get("changes")
    if not cons or not chg:
        raise ParseError(f"expected tables 'constituents' and 'changes'; found {sorted(p.tables)}")
    head = [c[1].lower() for c in cons[0]]
    try:
        i_sym, i_name = head.index("symbol"), head.index("security")
    except ValueError as e:
        raise ParseError(f"constituents header not recognised: {head}") from e
    i_cik = head.index("cik") if "cik" in head else None
    current: dict[str, str] = {}
    ciks: dict[str, int] = {}
    for row in cons[1:]:
        if len(row) <= max(i_sym, i_name):
            continue
        t = _ticker(row[i_sym][1])
        if t is None:
            continue
        current[t] = row[i_name][1]
        if i_cik is not None and i_cik < len(row) and row[i_cik][1].strip().isdigit():
            ciks[t] = int(row[i_cik][1].strip())
    changes: list[dict[str, Any]] = []
    skipped = 0
    for row in chg:
        cells = [c[1] for c in row]
        if any(k == "th" for k, _ in row) or len(cells) < 5:
            continue
        d = _date(cells[0])
        if d is None:
            skipped += 1
            continue
        added, removed = _ticker(cells[1]), _ticker(cells[3])
        if not added and not removed:
            skipped += 1
            continue
        changes.append({"date": d, "added": added, "addedName": cells[2] if added else None,
                        "removed": removed, "removedName": cells[4] if removed else None})  # fmt: skip
    if len(current) < 450 or len(changes) < 100:
        raise ParseError(f"too few rows: {len(current)} constituents, {len(changes)} changes")
    warn = [f"{skipped} change rows could not be read and were left out"] if skipped else []
    return Membership(as_of, current, ciks, changes, sha256, warn)
