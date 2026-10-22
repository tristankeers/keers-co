"""The company directory the workbench searches: who a company is and where its data can come from.

The global universe (``keers.universe``, built separately) is the intended backing. Until it is in the store, the
directory falls back to what is already known: companies in the store, the SEC ticker map (US filers) and, when
live sources are on, Yahoo's symbol search for listings anywhere. Every record says where it came from
(``source``) and how deep its data goes (``coverage``), so thin data is plain to see.

Coverage values match the universe: ``sec`` (files with the SEC: fundamentals from its filings) and
``prices-only`` (price, chart and news; no fundamentals source yet). The universe plans to split ``sec`` into
``sec-us-gaap`` and ``sec-ifrs``; both are understood here. The fallbacks use ``sec-filer``, which means the same
as ``sec``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from keers.data.store import Store

from . import sources

COVERAGE_TEXT = {
    "sec": "Files with the SEC. Fundamentals load on refresh; valuation and the distress screen run if the filings "
    "are complete (US GAAP only for now; IFRS filers will show as blocked).",
    "sec-us-gaap": "Fundamentals from SEC filings (US GAAP). Valuation and distress screen available.",
    "sec-ifrs": "Files with the SEC under IFRS. The fundamentals builder reads US GAAP only, so valuation and "
    "the distress screen will be blocked until IFRS concepts are mapped.",
    "sec-filer": "Files with the SEC. Fundamentals load on refresh; valuation runs if the filings are complete.",
    "prices-only": "No fundamentals source for this listing yet: price, chart and news only. Valuation and the "
    "distress screen need SEC filings for now.",
}


@dataclass(frozen=True)
class Listing:
    """One listed security. Field names match ``keers.universe.Listing``."""

    key: str
    symbol: str  # Yahoo style: AAPL, BRK-B, VOD.L, 7203.T
    name: str
    exchange: str = ""
    mic: str = ""
    country: str = ""  # ISO 3166-1 alpha-2
    currency: str = ""  # ISO 4217; GBX for pence
    cik: int | None = None
    isin: str | None = None
    figi: str | None = None
    lei: str | None = None
    sector: str | None = None
    industry: str | None = None
    source: str = ""
    as_of: str | None = None
    coverage: str = "prices-only"
    warnings: tuple[str, ...] = ()  # e.g. unconfirmed currency or symbol, stale record (from the universe)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["warnings"] = list(self.warnings)
        d["coverageText"] = COVERAGE_TEXT.get(self.coverage, "")
        return d


class Directory(Protocol):
    name: str

    def search(self, query: str, limit: int = 20) -> list[Listing]: ...

    def get(self, key: str) -> Listing | None: ...


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def score(query: str, symbol: str, name: str) -> int | None:
    """Rank a match: exact symbol, symbol prefix, name prefix, word prefix, then substring. None if no match."""
    q, sym, nm = norm(query), norm(symbol), name.upper()
    if not q:
        return None
    if sym == q:
        return 0
    if sym.startswith(q):
        return 1
    if nm.startswith(query.strip().upper()):
        return 2
    if any(w.startswith(query.strip().upper()) for w in re.split(r"[\s\-,./&]+", nm)):
        return 3
    if q in norm(nm):
        return 4
    return None


def rank(query: str, items: Iterable[Listing], limit: int) -> list[Listing]:
    scored = [
        (s, len(x.symbol), x.symbol, x) for x in items if (s := score(query, x.symbol, x.name)) is not None
    ]
    return [x for *_, x in sorted(scored, key=lambda t: t[:3])][:limit]


class StoreDirectory:
    """Companies whose filings are already in the store."""

    name = "store"

    def __init__(self, open_store: Callable[[], Store]) -> None:
        self.open_store = open_store

    def _all(self) -> list[Listing]:
        store = self.open_store()
        try:
            rows = sources.companies(store)
        finally:
            store.close()
        return [
            Listing(key=r["ticker"], symbol=sources.symbol(r["ticker"]), name=r["name"], country="US", currency="USD",
                    cik=r["cik"], source="store (SEC submissions)", as_of=r["updatedAt"][:10], coverage="sec-filer")
            for r in rows
        ]  # fmt: skip

    def search(self, query: str, limit: int = 20) -> list[Listing]:
        return rank(query, self._all(), limit)

    def get(self, key: str) -> Listing | None:
        k = sources.symbol(key)
        return next((x for x in self._all() if x.symbol == k), None)


class SecTickerDirectory:
    """SEC's company_tickers.json: every US filer with a ticker. Cached to a file; refreshed when live."""

    name = "sec"

    def __init__(self, cache: Path, fetch: Callable[[], tuple[bytes, str]] | None = None) -> None:
        self.cache = cache
        self.fetch = fetch
        self._rows: list[Listing] | None = None
        self._by_symbol: dict[str, Listing] = {}

    def _load(self) -> list[Listing]:
        if self._rows is not None:
            return self._rows
        doc: Any = None
        as_of = None
        if self.cache.exists():
            cached = json.loads(self.cache.read_text())
            doc, as_of = cached.get("doc"), cached.get("retrievedAt")
        if doc is None and self.fetch is not None:
            try:
                body, as_of = self.fetch()
                doc = json.loads(body)
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                self.cache.write_text(json.dumps({"retrievedAt": as_of, "doc": doc}))
            except Exception:  # noqa: BLE001 - an unreachable source leaves the directory to its other backings
                doc = None
        rows: list[Listing] = []
        for r in (doc or {}).values() if isinstance(doc, dict) else []:
            try:
                sym = sources.symbol(str(r["ticker"]))
                rows.append(Listing(key=sym, symbol=sym, name=str(r["title"]), country="US", currency="USD",
                                    cik=int(r["cik_str"]), source="SEC company_tickers.json",
                                    as_of=(as_of or "")[:10] or None, coverage="sec-filer"))  # fmt: skip
            except (KeyError, TypeError, ValueError):
                continue
        self._rows = rows
        self._by_symbol = {x.symbol: x for x in rows}
        return rows

    def search(self, query: str, limit: int = 20) -> list[Listing]:
        return rank(query, self._load(), limit)

    def get(self, key: str) -> Listing | None:
        self._load()
        return self._by_symbol.get(sources.symbol(key))


class YahooSearchDirectory:
    """Live symbol search across world exchanges (Yahoo; unofficial, personal use only)."""

    name = "yahoo"

    def __init__(self, search: Callable[[str, int], list[dict[str, str]]]) -> None:
        self._search = search

    def _listing(self, q: dict[str, str]) -> Listing:
        sym = sources.symbol(q["symbol"])
        today = datetime.now(UTC).date().isoformat()
        return Listing(key=sym, symbol=sym, name=q.get("name") or sym, exchange=q.get("exchange", ""),
                       sector=q.get("sector") or None, industry=q.get("industry") or None,
                       source="Yahoo Finance search (unofficial)", as_of=today, coverage="prices-only")  # fmt: skip

    def search(self, query: str, limit: int = 20) -> list[Listing]:
        try:
            return [self._listing(q) for q in self._search(query, limit)]
        except Exception:  # noqa: BLE001 - search is best-effort; the caller reports the source as unavailable
            return []

    def get(self, key: str) -> Listing | None:
        sym = sources.symbol(key)
        return next((x for x in self.search(sym, 5) if x.symbol == sym), None)


class UniverseDirectory:
    """Adapter for ``keers.universe.Universe`` once that package and its ``listings`` table exist."""

    name = "universe"

    def __init__(self, open_store: Callable[[], Store]) -> None:
        self.open_store = open_store

    @staticmethod
    def available() -> bool:
        try:
            import keers.universe  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
        except ImportError:
            return False
        return True

    def _call(self, method: str, *args: Any) -> Any:
        import keers.universe as U  # type: ignore[import-not-found,unused-ignore]

        store = self.open_store()
        try:
            return getattr(U.Universe(store), method)(*args)
        finally:
            store.close()

    @staticmethod
    def _convert(x: Any) -> Listing:
        d = x.as_dict() if hasattr(x, "as_dict") else dict(x)
        fields = Listing.__dataclass_fields__
        out: dict[str, Any] = {k: d.get(k) for k in fields if k in d and d.get(k) is not None}
        out["warnings"] = tuple(d.get("warnings") or ())
        out.setdefault("source", "universe")
        return Listing(**out)

    def search(self, query: str, limit: int = 20) -> list[Listing]:
        try:
            return [self._convert(x) for x in self._call("search", query, limit)]
        except Exception:  # noqa: BLE001 - e.g. the listings table not built yet
            return []

    def get(self, key: str) -> Listing | None:
        try:
            x = self._call("get", key)
        except Exception:  # noqa: BLE001
            return None
        return None if x is None else self._convert(x)


class CompositeDirectory:
    """Asks each backing in order. The first record for a symbol wins; later ones fill in a missing CIK."""

    def __init__(self, backings: Sequence[Directory]) -> None:
        self.backings = list(backings)

    @property
    def sources(self) -> list[str]:
        return [b.name for b in self.backings]

    def search(self, query: str, limit: int = 20) -> list[Listing]:
        seen: dict[str, Listing] = {}
        order: list[str] = []
        for b in self.backings:
            for x in b.search(query, limit):
                if x.symbol not in seen:
                    seen[x.symbol] = x
                    order.append(x.symbol)
                elif seen[x.symbol].cik is None and x.cik is not None:
                    seen[x.symbol] = _with(seen[x.symbol], cik=x.cik, coverage="sec-filer")
        return rank(query, (seen[s] for s in order), limit) or [seen[s] for s in order][:limit]

    def get(self, key: str) -> Listing | None:
        found: Listing | None = None
        for b in self.backings:
            x = b.get(key)
            if x is None:
                continue
            if found is None:
                found = x
                if found.cik is not None:
                    return found
            elif x.cik is not None:
                return _with(
                    found,
                    cik=x.cik,
                    coverage=found.coverage if found.coverage != "prices-only" else "sec-filer",
                )
        return found


def _with(x: Listing, **changes: Any) -> Listing:
    return Listing(**{**asdict(x), **changes})
