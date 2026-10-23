"""The paper-trading ledger: one SQLite file of portfolios and their orders, kept apart from the market-data store.

It is append-only. Orders are never edited or deleted, including rejected ones, so the record shows every trade
that was tried, the price it used and the risk report that allowed or blocked it. Positions, cash and P&L are
rebuilt from the filled orders every time (``analytics``), so there is no running balance to drift.

Default file: ``data/paper.sqlite`` (``KEERS_PAPER_DB`` to change it). Deleting the market-data store does not
touch it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PAPER_DB = "data/paper.sqlite"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    base_currency TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    created_at TEXT NOT NULL,      -- UTC time the order was decided
    trade_date TEXT NOT NULL,      -- the business date it counts on
    status TEXT NOT NULL,          -- filled | rejected
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,        -- A | B | C
    sector TEXT,
    sector_basis TEXT NOT NULL,    -- entered | listing | sic | position
    quantity REAL NOT NULL,        -- signed shares: positive buys
    reference_price REAL,
    price_source TEXT NOT NULL,    -- e.g. "yahoo quote", "yahoo close"
    price_at TEXT,                 -- time or date of that price
    price_sha256 TEXT NOT NULL,    -- hash of the raw response it came from
    fill_price REAL,
    commission REAL,
    slippage_bps REAL,
    commission_bps REAL,
    cost_version TEXT NOT NULL,
    currency TEXT,
    limits_version TEXT NOT NULL,
    risk TEXT NOT NULL,            -- the full risk report, JSON
    reason TEXT NOT NULL,          -- why it was rejected; '' when filled
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_portfolio ON orders(portfolio_id, id);
"""


@dataclass(frozen=True)
class Portfolio:
    id: int
    name: str
    base_currency: str
    starting_cash: float
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "baseCurrency": self.base_currency,
                "startingCash": self.starting_cash, "createdAt": self.created_at}  # fmt: skip


@dataclass(frozen=True)
class Order:
    portfolio_id: int
    created_at: str
    trade_date: str
    status: str
    symbol: str
    strategy: str
    sector: str | None
    sector_basis: str
    quantity: float
    reference_price: float | None
    price_source: str
    price_at: str | None
    price_sha256: str
    fill_price: float | None
    commission: float | None
    slippage_bps: float | None
    commission_bps: float | None
    cost_version: str
    currency: str | None
    limits_version: str
    risk: dict[str, Any]
    reason: str = ""
    note: str = ""
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notional"] = self.quantity * self.fill_price if self.fill_price is not None else None
        return d


_COLS = ("portfolio_id", "created_at", "trade_date", "status", "symbol", "strategy", "sector", "sector_basis",
         "quantity", "reference_price", "price_source", "price_at", "price_sha256", "fill_price", "commission",
         "slippage_bps", "commission_bps", "cost_version", "currency", "limits_version", "risk", "reason", "note")  # fmt: skip


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Ledger:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        elif int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(f"paper ledger schema {row[0]} does not match code schema {SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # portfolios -------------------------------------------------------------------------------------------------
    def create_portfolio(self, name: str, starting_cash: float, base_currency: str = "USD") -> Portfolio:
        name = " ".join(str(name).split())[:60]
        if not name:
            raise ValueError("a portfolio needs a name")
        if not isinstance(starting_cash, int | float) or not 1_000 <= starting_cash <= 1e12:
            raise ValueError("starting cash must be between 1,000 and 1,000,000,000,000")
        if self.db.execute("SELECT 1 FROM portfolios WHERE name=?", (name,)).fetchone():
            raise ValueError(f"a portfolio called {name!r} already exists")
        with self.db:
            cur = self.db.execute(
                "INSERT INTO portfolios (name, base_currency, starting_cash, created_at) VALUES (?,?,?,?)",
                (name, base_currency, float(starting_cash), now_utc()),
            )
        p = self.portfolio(int(cur.lastrowid or 0))
        assert p is not None  # noqa: S101
        return p

    def portfolio(self, pid: int) -> Portfolio | None:
        r = self.db.execute(
            "SELECT id,name,base_currency,starting_cash,created_at FROM portfolios WHERE id=?", (pid,)
        ).fetchone()
        return Portfolio(*r) if r else None

    def portfolios(self) -> list[Portfolio]:
        rows = self.db.execute(
            "SELECT id,name,base_currency,starting_cash,created_at FROM portfolios ORDER BY id"
        )
        return [Portfolio(*r) for r in rows]

    # orders -----------------------------------------------------------------------------------------------------
    def append(self, o: Order) -> Order:
        if o.status not in ("filled", "rejected"):
            raise ValueError("order status must be filled or rejected")
        if o.status == "filled" and (
            o.fill_price is None or o.commission is None or not o.risk.get("approved")
        ):
            # The ledger itself refuses a fill without an approved risk report.
            raise ValueError("a filled order needs a fill price, a commission and an approved risk report")
        vals = asdict(o)
        vals["risk"] = json.dumps(o.risk, sort_keys=True, default=str)
        with self.db:
            cur = self.db.execute(
                f"INSERT INTO orders ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",  # noqa: S608
                [vals[c] for c in _COLS],
            )
        return Order(**{**asdict(o), "id": int(cur.lastrowid or 0)})

    def orders(self, pid: int, status: str | None = None) -> list[Order]:
        q = f"SELECT id,{','.join(_COLS)} FROM orders WHERE portfolio_id=?"  # noqa: S608
        args: list[Any] = [pid]
        if status:
            q += " AND status=?"
            args.append(status)
        out = []
        for r in self.db.execute(q + " ORDER BY id", args):
            d = dict(zip(("id", *_COLS), r, strict=True))
            d["risk"] = json.loads(d["risk"])
            out.append(Order(**d))
        return out
