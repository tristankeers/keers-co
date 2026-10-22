"""SQLite store for raw documents, filed facts, prices, rates and refresh runs.

Raw responses are kept by SHA-256 so any parsed number can be traced back to the exact bytes it came from.
Facts are never overwritten in place: a restated figure arrives with a later filing date and both are kept, so
point-in-time queries ("what was known on date X") stay honest.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .models import Fact, Filing, IntradayBar, NewsItem, PricePoint, Quote, RatePoint, RunRecord

SCHEMA_VERSION = 3

# When two sources have a close for the same day, the snapshot uses the first of these. The source is always
# cited with the price, so the choice is visible.
SOURCE_RANK = {"yahoo": 0, "tiingo": 1, "stooq": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_documents (
    sha256 TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    body BLOB
);
CREATE TABLE IF NOT EXISTS companies (
    cik INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    sic TEXT,
    fiscal_year_end TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS companies_ticker ON companies(ticker);
CREATE TABLE IF NOT EXISTS facts (
    cik INTEGER NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    unit TEXT NOT NULL,
    start TEXT NOT NULL,  -- '' for instant (balance-sheet) facts, so the key dedupes
    end TEXT NOT NULL,
    value REAL NOT NULL,
    accn TEXT NOT NULL,
    form TEXT NOT NULL,
    filed TEXT NOT NULL,
    fy INTEGER,
    fp TEXT,
    frame TEXT,
    raw_sha256 TEXT NOT NULL,
    PRIMARY KEY (cik, taxonomy, concept, unit, start, end, accn)
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts(cik, concept, end);
CREATE TABLE IF NOT EXISTS filings (
    cik INTEGER NOT NULL,
    accn TEXT NOT NULL,
    form TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    report_date TEXT,
    primary_document TEXT,
    PRIMARY KEY (cik, accn)
);
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    close REAL NOT NULL,
    open REAL, high REAL, low REAL, volume REAL,
    raw_sha256 TEXT NOT NULL,
    adj_close REAL,
    PRIMARY KEY (ticker, day, source)
);
CREATE TABLE IF NOT EXISTS rates (
    series TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    value REAL NOT NULL,
    raw_sha256 TEXT NOT NULL,
    PRIMARY KEY (series, day, source)
);
CREATE TABLE IF NOT EXISTS bars (
    ticker TEXT NOT NULL,
    interval TEXT NOT NULL,
    start TEXT NOT NULL,  -- UTC, ISO 8601
    source TEXT NOT NULL,
    close REAL NOT NULL,
    open REAL, high REAL, low REAL, volume REAL,
    raw_sha256 TEXT NOT NULL,
    PRIMARY KEY (ticker, interval, start, source)
);
CREATE TABLE IF NOT EXISTS quotes (
    ticker TEXT NOT NULL,
    at TEXT NOT NULL,  -- time of the last trade, UTC
    source TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    exchange TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    warnings TEXT NOT NULL,
    PRIMARY KEY (ticker, at, source)
);
-- Third-party headlines. Untrusted text: shown and cited, never treated as instructions.
CREATE TABLE IF NOT EXISTS news (
    uuid TEXT NOT NULL,
    source TEXT NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    link TEXT NOT NULL,
    published TEXT,
    related TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    PRIMARY KEY (uuid, source)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """A thin wrapper over one SQLite file. Use ``Store(":memory:")`` in tests."""

    def __init__(self, path: str | Path, keep_bodies: bool = True) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.keep_bodies = keep_bodies
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        else:
            version = int(row[0])
            if version == 1:
                # v1 -> v2: adjusted closes for return statistics.
                self.db.execute("ALTER TABLE prices ADD COLUMN adj_close REAL")
                version = 2
            if version == 2:
                # v2 -> v3: intraday bars, quotes and news. The tables are created by SCHEMA above.
                version = 3
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"Store schema {row[0]} does not match code schema {SCHEMA_VERSION}")
            if version != int(row[0]):
                self.db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
                self.db.commit()

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.db:
            yield self.db

    # raw documents ---------------------------------------------------------------------------------------------
    def put_raw(self, url: str, body: bytes, sha256: str, retrieved_at: str, content_type: str = "") -> None:
        with self.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO raw_documents VALUES (?,?,?,?,?,?)",
                (sha256, url, retrieved_at, content_type, len(body), body if self.keep_bodies else None),
            )

    def raw(self, sha256: str) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT sha256,url,retrieved_at,content_type,size FROM raw_documents WHERE sha256=?", (sha256,)
        ).fetchone()
        if r is None:
            return None
        return dict(zip(("sha256", "url", "retrievedAt", "contentType", "size"), r, strict=True))

    def latest_body(self, url: str) -> bytes | None:
        r = self.db.execute(
            "SELECT body FROM raw_documents WHERE url=? AND body IS NOT NULL ORDER BY retrieved_at DESC LIMIT 1",
            (url,),
        ).fetchone()
        return None if r is None else bytes(r[0])

    # companies -------------------------------------------------------------------------------------------------
    def put_company(self, cik: int, ticker: str, name: str, sic: str | None, fye: str | None) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?)",
                (cik, ticker.upper(), name, sic, fye, now_utc()),
            )

    def company(self, ticker: str) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT cik,ticker,name,sic,fiscal_year_end FROM companies WHERE ticker=?", (ticker.upper(),)
        ).fetchone()
        if r is None:
            return None
        return dict(zip(("cik", "ticker", "name", "sic", "fiscalYearEnd"), r, strict=True))

    # facts -----------------------------------------------------------------------------------------------------
    def put_facts(self, facts: Iterable[Fact]) -> int:
        rows = [
            (
                f.cik, f.taxonomy, f.concept, f.unit, _iso(f.start) or "", f.end.isoformat(), f.value, f.accn,
                f.form, f.filed.isoformat(), f.fy, f.fp, f.frame, f.raw_sha256,
            )
            for f in facts
        ]  # fmt: skip
        with self.tx() as db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            return db.total_changes - before

    def facts(
        self,
        cik: int,
        concept: str,
        unit: str | None = None,
        known_by: date | None = None,
        taxonomy: str = "us-gaap",
    ) -> list[Fact]:
        """All filed values of a concept, oldest period first. ``known_by`` keeps only facts filed on or before it."""
        q = "SELECT * FROM facts WHERE cik=? AND taxonomy=? AND concept=?"
        args: list[Any] = [cik, taxonomy, concept]
        if unit is not None:
            q += " AND unit=?"
            args.append(unit)
        if known_by is not None:
            q += " AND filed<=?"
            args.append(known_by.isoformat())
        q += " ORDER BY end, start, filed"
        return [
            Fact(
                cik=r[0], taxonomy=r[1], concept=r[2], unit=r[3], start=_d(r[4]), end=date.fromisoformat(r[5]),
                value=r[6], accn=r[7], form=r[8], filed=date.fromisoformat(r[9]), fy=r[10], fp=r[11],
                frame=r[12], raw_sha256=r[13],
            )
            for r in self.db.execute(q, args)
        ]  # fmt: skip

    # filings ---------------------------------------------------------------------------------------------------
    def put_filings(self, filings: Iterable[Filing]) -> None:
        rows = [
            (f.cik, f.accn, f.form, f.filing_date.isoformat(), _iso(f.report_date), f.primary_document)
            for f in filings
        ]
        with self.tx() as db:
            db.executemany("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?)", rows)

    def filings(self, cik: int, forms: tuple[str, ...] = ("10-K", "10-Q")) -> list[Filing]:
        marks = ",".join("?" * len(forms))
        q = f"SELECT * FROM filings WHERE cik=? AND form IN ({marks}) ORDER BY filing_date"  # noqa: S608
        return [
            Filing(r[0], r[1], r[2], date.fromisoformat(r[3]), _d(r[4]), r[5])
            for r in self.db.execute(q, (cik, *forms))
        ]

    # prices ----------------------------------------------------------------------------------------------------
    def put_prices(self, points: Iterable[PricePoint]) -> int:
        rows = [
            (
                p.ticker.upper(),
                p.day.isoformat(),
                p.source,
                p.close,
                p.open,
                p.high,
                p.low,
                p.volume,
                p.raw_sha256,
                p.adj_close,
            )
            for p in points
        ]
        with self.tx() as db:
            before = db.total_changes
            db.executemany(
                "INSERT OR REPLACE INTO prices (ticker,day,source,close,open,high,low,volume,raw_sha256,adj_close) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            return db.total_changes - before

    def last_price(self, ticker: str, on_or_before: date | None = None) -> PricePoint | None:
        q = "SELECT ticker,day,source,close,open,high,low,volume,raw_sha256 FROM prices WHERE ticker=?"
        args: list[Any] = [ticker.upper()]
        if on_or_before is not None:
            q += " AND day<=?"
            args.append(on_or_before.isoformat())
        rows = self.db.execute(q + " ORDER BY day DESC LIMIT 5", args).fetchall()
        if not rows:
            return None
        same_day = [x for x in rows if x[1] == rows[0][1]]
        r = min(same_day, key=lambda x: (SOURCE_RANK.get(x[2], 99), x[2]))
        return PricePoint(r[0], date.fromisoformat(r[1]), r[3], r[2], r[4], r[5], r[6], r[7], r[8])

    def adjusted_closes(self, ticker: str, start: date, end: date) -> list[tuple[date, float | None]]:
        """Daily (day, adjusted close) from ONE source; None on days that source has no adjusted close.

        Sources adjust for dividends in slightly different ways, so series are never stitched together. The source
        used is the one whose adjusted closes run latest, then the one with the most of them.
        """
        args = (ticker.upper(), start.isoformat(), end.isoformat())
        rng = "ticker=? AND day>=? AND day<=?"
        best = self.db.execute(
            f"SELECT source FROM prices WHERE {rng} AND adj_close IS NOT NULL "  # noqa: S608
            "GROUP BY source ORDER BY MAX(day) DESC, COUNT(*) DESC, source LIMIT 1",
            args,
        ).fetchone()
        if best is None:
            q = f"SELECT DISTINCT day FROM prices WHERE {rng} ORDER BY day"  # noqa: S608
            return [(date.fromisoformat(d), None) for (d,) in self.db.execute(q, args)]
        q = f"SELECT day, adj_close FROM prices WHERE {rng} AND source=? ORDER BY day"  # noqa: S608
        return [(date.fromisoformat(d), a) for d, a in self.db.execute(q, (*args, best[0]))]

    # intraday bars, quotes, news -------------------------------------------------------------------------------
    def put_bars(self, bars: Iterable[IntradayBar]) -> int:
        rows = [
            (b.ticker.upper(), b.interval, b.start.isoformat(), b.source, b.close, b.open, b.high, b.low, b.volume,
             b.raw_sha256)
            for b in bars
        ]  # fmt: skip
        with self.tx() as db:
            before = db.total_changes
            db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            return db.total_changes - before

    def bars(self, ticker: str, interval: str, since: datetime | None = None) -> list[IntradayBar]:
        q = "SELECT ticker,interval,start,source,close,open,high,low,volume,raw_sha256 FROM bars"
        q += " WHERE ticker=? AND interval=?"
        args: list[Any] = [ticker.upper(), interval]
        if since is not None:
            q += " AND start>=?"
            args.append(since.astimezone(UTC).isoformat())
        return [
            IntradayBar(r[0], datetime.fromisoformat(r[2]), r[1], r[4], r[3], r[5], r[6], r[7], r[8], r[9])
            for r in self.db.execute(q + " ORDER BY start", args)
        ]

    def put_quote(self, q: Quote) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?,?)",
                (q.ticker.upper(), q.at.isoformat(), q.source, q.price, q.currency, q.exchange, q.raw_sha256,
                 json.dumps(list(q.warnings))),
            )  # fmt: skip

    def last_quote(self, ticker: str) -> Quote | None:
        r = self.db.execute(
            "SELECT ticker,price,at,source,currency,exchange,raw_sha256,warnings FROM quotes WHERE ticker=? "
            "ORDER BY at DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        if r is None:
            return None
        return Quote(
            r[0], r[1], datetime.fromisoformat(r[2]), r[3], r[4], r[5], r[6], tuple(json.loads(r[7]))
        )

    def put_news(self, items: Iterable[NewsItem]) -> int:
        rows = [
            (n.uuid, n.source, n.ticker.upper(), n.title, n.publisher, n.link,
             n.published.isoformat() if n.published else None, json.dumps(list(n.related)), n.raw_sha256)
            for n in items
        ]  # fmt: skip
        with self.tx() as db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?)", rows)
            return db.total_changes - before

    def news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        """Stored headlines for a ticker, newest first. Untrusted third-party text."""
        q = (
            "SELECT uuid,source,ticker,title,publisher,link,published,related,raw_sha256 FROM news WHERE ticker=? "
            "ORDER BY published DESC LIMIT ?"
        )
        return [
            NewsItem(r[0], r[2], r[3], r[4], r[5], datetime.fromisoformat(r[6]) if r[6] else None,
                     tuple(json.loads(r[7])), r[1], r[8])
            for r in self.db.execute(q, (ticker.upper(), limit))
        ]  # fmt: skip

    # rates -----------------------------------------------------------------------------------------------------
    def put_rates(self, points: Iterable[RatePoint]) -> int:
        rows = [(p.series, p.day.isoformat(), p.source, p.value, p.raw_sha256) for p in points]
        with self.tx() as db:
            before = db.total_changes
            db.executemany("INSERT OR REPLACE INTO rates VALUES (?,?,?,?,?)", rows)
            return db.total_changes - before

    def last_rate(self, series: str, on_or_before: date | None = None) -> RatePoint | None:
        q = "SELECT series,day,source,value,raw_sha256 FROM rates WHERE series=?"
        args: list[Any] = [series]
        if on_or_before is not None:
            q += " AND day<=?"
            args.append(on_or_before.isoformat())
        r = self.db.execute(q + " ORDER BY day DESC LIMIT 1", args).fetchone()
        if r is None:
            return None
        return RatePoint(r[0], date.fromisoformat(r[1]), r[3], r[2], r[4])

    # runs ------------------------------------------------------------------------------------------------------
    def start_run(self, kind: str) -> int:
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO runs (kind, started_at, status, detail) VALUES (?,?,?,?)",
                (kind, now_utc(), "running", "{}"),
            )
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, detail: dict[str, Any]) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
                (now_utc(), status, json.dumps(detail, sort_keys=True), run_id),
            )

    def runs(self, limit: int = 10) -> list[RunRecord]:
        q = "SELECT kind, started_at, finished_at, status, detail FROM runs ORDER BY id DESC LIMIT ?"
        return [RunRecord(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in self.db.execute(q, (limit,))]
