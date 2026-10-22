"""Reading and writing the global company universe: ``Universe(store).search(...)`` and ``.get(...)``.

A listing is one share line on one exchange. ``key`` ("<MIC>:<local ticker>", e.g. "XLON:VOD") is the primary
key; ``symbol`` is the Yahoo-style symbol ("VOD.L"). Every listing says where it came from (``source``), when
(``as_of``), which raw response (``raw_sha256``), how its currency and Yahoo symbol were worked out
(``currency_basis``, ``symbol_basis``), and how deep its fundamentals go (``coverage``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

from ..data.store import Store, now_utc

# How deep a listing's fundamentals go. SEC filers get the full filing pipeline; everyone else has prices,
# charts and headlines only until a filings source for their market is added (see docs/universe.md).
COVERAGE_SEC = "sec"
COVERAGE_PRICES = "prices-only"

STALE_AFTER_DAYS = 14  # a listing not re-confirmed by a build for this long is flagged stale

COLUMNS = (
    "key", "symbol", "local_ticker", "name", "exchange", "mic", "country", "currency", "currency_basis", "cik",
    "isin", "figi", "composite_figi", "share_class_figi", "lei", "sector", "industry", "security_type",
    "coverage", "symbol_basis", "is_primary", "primary_key", "segment", "source", "as_of", "raw_sha256", "notes",
)  # fmt: skip


@dataclass(frozen=True)
class Listing:
    key: str
    symbol: str
    local_ticker: str
    name: str
    exchange: str
    mic: str
    country: str  # ISO-2 of the exchange (where it trades, not necessarily where the company is based)
    currency: str  # ISO-4217 trading currency; GBX/ZAC/ILA are pence/cents/agorot
    currency_basis: str  # "yahoo" (confirmed), "exchange-rule" (US: always USD), "exchange-default" (assumed)
    security_type: str
    coverage: str  # "sec" or "prices-only"
    symbol_basis: (
        str  # "derived" (reliable rule), "derived-unsure" (rule with known exceptions), "yahoo-verified"
    )
    segment: str
    source: str
    as_of: str  # date the source last listed it
    raw_sha256: str
    cik: int | None = None
    isin: str | None = None
    figi: str | None = None
    composite_figi: str | None = None
    share_class_figi: str | None = None
    lei: str | None = None
    sector: str | None = None
    industry: str | None = None
    is_primary: bool = True
    primary_key: str | None = None  # for a secondary listing, the key of the listing kept as primary
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=(), compare=False)  # added at read time (staleness, checks)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"], d["warnings"] = list(self.notes), list(self.warnings)
        return d

    def row(self) -> tuple[Any, ...]:
        d = asdict(self)
        d["notes"] = json.dumps(list(self.notes))
        d["is_primary"] = int(self.is_primary)
        return tuple(d[c] for c in COLUMNS)


def _from_row(r: Sequence[Any]) -> Listing:
    d = dict(zip(COLUMNS, r, strict=True))
    d["notes"] = tuple(json.loads(d["notes"]))
    d["is_primary"] = bool(d["is_primary"])
    return Listing(**d)


class Universe:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.db = store.db

    # writing ---------------------------------------------------------------------------------------------------
    def replace_segment(self, segment: str, listings: Iterable[Listing]) -> int:
        """Swap in a fresh copy of one source batch. Called only after the batch was fetched in full."""
        rows = [x.row() for x in listings]
        marks = ",".join("?" * len(COLUMNS))
        with self.store.tx() as db:
            db.execute("DELETE FROM listings WHERE segment=?", (segment,))
            db.executemany(f"INSERT OR REPLACE INTO listings VALUES ({marks})", rows)  # noqa: S608
        return len(rows)

    def mark_primaries(self) -> dict[str, int]:
        """Where one share class trades in several places, keep one listing as primary and point the rest at it.

        The primary is the listing on a non-hub exchange, then the earliest exchange in ``exchanges.NON_US`` (US
        first). Listings without a share-class FIGI are always primary.
        """
        from .exchanges import BY_CODE, RANK

        def rank(segment: str) -> tuple[int, int]:
            code = segment.split(":", 1)[1] if segment.startswith("openfigi:") else "US"
            ex = BY_CODE.get(code)
            return (1 if ex is not None and ex.hub else 0, RANK.get(code, 999))

        groups: dict[str, list[tuple[str, str]]] = {}
        q = "SELECT share_class_figi, key, segment FROM listings WHERE share_class_figi IS NOT NULL AND share_class_figi != ''"
        for sc, key, seg in self.db.execute(q):
            groups.setdefault(sc, []).append((key, seg))
        updates: list[tuple[int, str | None, str]] = []
        for members in groups.values():
            members.sort(key=lambda m: (*rank(m[1]), m[0]))
            head = members[0][0]
            updates.append((1, None, head))
            updates.extend((0, head, k) for k, _ in members[1:])
        with self.store.tx() as db:
            db.execute("UPDATE listings SET is_primary=1, primary_key=NULL")
            db.executemany("UPDATE listings SET is_primary=?, primary_key=? WHERE key=?", updates)
        return {"shareClasses": len(groups), "secondary": sum(1 for u in updates if u[0] == 0)}

    def put_check(self, key: str, status: str, yahoo_symbol: str, raw_sha256: str, meta: dict[str, Any],
                  detail: str = "") -> None:  # fmt: skip
        with self.store.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO listing_checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, now_utc(), status, yahoo_symbol, meta.get("currency"), meta.get("exchangeName"),
                 meta.get("instrumentType"), meta.get("longName") or meta.get("shortName"), raw_sha256, detail),
            )  # fmt: skip

    # reading ---------------------------------------------------------------------------------------------------
    def _resolve(self, x: Listing, today: date | None = None) -> Listing:
        """Apply any Yahoo check and add read-time warnings (staleness, unconfirmed currency or symbol)."""
        from .exchanges import MINOR_UNITS

        today = today or date.today()
        warnings: list[str] = []
        c = self.db.execute(
            "SELECT status, yahoo_symbol, currency, checked_at, detail FROM listing_checks WHERE key=?",
            (x.key,),
        ).fetchone()
        if c is not None:
            status, ysym, cur, checked, detail = c
            if status == "ok":
                cur = MINOR_UNITS.get(cur, cur) if cur else None
                if cur and cur != x.currency:
                    warnings.append(
                        f"currency corrected from the exchange default {x.currency} to {cur} by Yahoo"
                    )
                x = replace(x, symbol=ysym, currency=cur or x.currency,
                            currency_basis="yahoo" if cur else x.currency_basis,
                            symbol_basis="yahoo-verified")  # fmt: skip
            else:
                warnings.append(f"Yahoo check {status} on {checked[:10]}: {detail}")
        if x.currency_basis == "exchange-default":
            warnings.append(
                f"currency {x.currency} is the exchange's usual currency, not confirmed for this listing"
            )
        if x.symbol_basis == "derived-unsure":
            warnings.append(
                f"Yahoo symbol {x.symbol} was worked out from the exchange ticker and is unconfirmed"
            )
        age = (today - date.fromisoformat(x.as_of)).days
        if age > STALE_AFTER_DAYS:
            warnings.append(f"stale: last confirmed by its source {age} days ago ({x.as_of})")
        return replace(x, warnings=tuple(warnings))

    def get(self, key_or_symbol: str, today: date | None = None) -> Listing | None:
        """By key ("XLON:VOD") or Yahoo symbol ("VOD.L"). A symbol shared by two listings returns the primary."""
        q = f"SELECT {','.join(COLUMNS)} FROM listings WHERE key=?"  # noqa: S608
        r = self.db.execute(q, (key_or_symbol.upper(),)).fetchone()
        if r is None:
            q = f"SELECT {','.join(COLUMNS)} FROM listings WHERE symbol=? ORDER BY is_primary DESC, key LIMIT 1"  # noqa: S608
            r = self.db.execute(q, (key_or_symbol.upper(),)).fetchone()
        if r is None:
            # a Yahoo check may have moved the symbol (VOLVB.ST -> VOLV-B.ST)
            k = self.db.execute("SELECT key FROM listing_checks WHERE yahoo_symbol=? AND status='ok'",
                                (key_or_symbol.upper(),)).fetchone()  # fmt: skip
            return self.get(k[0], today) if k else None
        return self._resolve(_from_row(r), today)

    def search(self, query: str, limit: int = 20, primary_only: bool = True, country: str | None = None,
               today: date | None = None) -> list[Listing]:  # fmt: skip
        """Exact symbol or ticker first, then names starting with the query, then names containing it."""
        qn = query.strip().upper()
        if not qn:
            return []
        like = qn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = "(symbol=? OR local_ticker=? OR UPPER(name) LIKE ? ESCAPE '\\')"
        args: list[Any] = [qn, qn, f"%{like}%"]
        if primary_only:
            where += " AND is_primary=1"
        if country:
            where += " AND country=?"
            args.append(country.upper())
        q = (
            f"SELECT {','.join(COLUMNS)} FROM listings WHERE {where} ORDER BY "  # noqa: S608
            "CASE WHEN symbol=? OR local_ticker=? THEN 0 WHEN UPPER(name) LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END, "
            "CASE coverage WHEN 'sec' THEN 0 ELSE 1 END, LENGTH(name), key LIMIT ?"
        )
        args += [qn, qn, f"{like}%", limit]
        return [self._resolve(_from_row(r), today) for r in self.db.execute(q, args)]

    def all(self, primary_only: bool = False) -> list[Listing]:
        q = f"SELECT {','.join(COLUMNS)} FROM listings"  # noqa: S608
        if primary_only:
            q += " WHERE is_primary=1"
        return [_from_row(r) for r in self.db.execute(q + " ORDER BY key")]

    def stats(self, today: date | None = None) -> dict[str, Any]:
        """Counts by exchange, coverage and how each field was obtained: where the universe is thin, in numbers."""
        today = today or date.today()
        stale_before = date.fromordinal(today.toordinal() - STALE_AFTER_DAYS).isoformat()
        by_ex: dict[str, Any] = {}
        q = (
            "SELECT l.mic, l.country, l.segment, COUNT(*), SUM(l.is_primary), SUM(l.coverage='sec'), "
            "SUM(c.status='ok'), SUM(c.status IS NOT NULL AND c.status!='ok'), SUM(l.as_of < ?), MAX(l.as_of) "
            "FROM listings l LEFT JOIN listing_checks c ON c.key=l.key GROUP BY l.mic, l.country, l.segment"
        )
        for mic, country, seg, n, prim, sec, ok, bad, stale, as_of in self.db.execute(q, (stale_before,)):
            by_ex[f"{mic}"] = {"country": country, "segment": seg, "listings": n, "primary": prim,
                               "secFilings": sec, "yahooChecked": ok or 0, "yahooProblems": bad or 0,
                               "stale": stale, "asOf": as_of}  # fmt: skip
        tot = self.db.execute(
            "SELECT COUNT(*), SUM(is_primary), SUM(coverage='sec' AND is_primary), COUNT(DISTINCT country) "
            "FROM listings"
        ).fetchone()
        return {
            "listings": tot[0],
            "primaryListings": tot[1] or 0,
            "primaryWithSecFilings": tot[2] or 0,
            "countries": tot[3],
            "byExchange": dict(sorted(by_ex.items(), key=lambda kv: -int(kv[1]["primary"] or 0))),
        }
