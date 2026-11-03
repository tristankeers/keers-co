"""The paper-trading ledger: one SQLite file of portfolios and their orders, kept apart from the market-data store.

It is append-only. Orders are never edited or deleted, including rejected ones, so the record shows every trade
that was tried, the price it used and the risk report that allowed or blocked it. Each order row carries the
SHA-256 of its own contents and of the row before it (schema 2), so an edited, deleted or reordered row breaks the
chain; ``verify`` finds it and the desk then refuses to trade. Kill-switch changes are kept the same way in
``controls``. Positions, cash and P&L are
rebuilt from the filled orders every time (``analytics``), so there is no running balance to drift.

Default file: ``data/paper.sqlite`` (``KEERS_PAPER_DB`` to change it). Deleting the market-data store does not
touch it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PAPER_DB = "data/paper.sqlite"
SCHEMA_VERSION = 2
GENESIS = "0" * 64

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
    note TEXT NOT NULL,
    prev_sha256 TEXT,              -- row_sha256 of the order before it (GENESIS for the first)
    row_sha256 TEXT                -- SHA-256 of this row's contents and prev_sha256
);
CREATE INDEX IF NOT EXISTS orders_portfolio ON orders(portfolio_id, id);
CREATE TABLE IF NOT EXISTS controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,          -- halt | resume
    reason TEXT NOT NULL,
    prev_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL
);
"""
_CONTROL_COLS = ("portfolio_id", "created_at", "action", "reason")


def _number(v: Any) -> Any:
    # SQLite gives back a whole number stored in a REAL column as a float (2 -> 2.0) and one stored in an
    # INTEGER column as an int, so every number is hashed as a float: written and read rows then hash alike.
    return float(v) if isinstance(v, int) and not isinstance(v, bool) else v


def row_hash(prev: str, values: dict[str, Any]) -> str:
    norm = {k: _number(v) for k, v in values.items()}
    raw = json.dumps({"prev": prev, **norm}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


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
        elif int(row[0]) == 1:
            self._migrate_from_1()
        elif int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(f"paper ledger schema {row[0]} does not match code schema {SCHEMA_VERSION}")

    def _migrate_from_1(self) -> None:
        """Schema 1 had no hash chain: add the columns and chain the existing orders in the order they were written.
        The chain can only vouch for those rows from this point on."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(orders)")}
        with self.db:
            for c in ("prev_sha256", "row_sha256"):
                if c not in cols:
                    self.db.execute(f"ALTER TABLE orders ADD COLUMN {c} TEXT")
            prev = GENESIS
            for r in self.db.execute(f"SELECT id,{','.join(_COLS)} FROM orders ORDER BY id").fetchall():  # noqa: S608
                values = dict(zip(_COLS, r[1:], strict=True))
                h = row_hash(prev, values)
                self.db.execute("UPDATE orders SET prev_sha256=?, row_sha256=? WHERE id=?", (prev, h, r[0]))
                prev = h
            self.db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_from_1', ?)", (now_utc(),))

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
        values = {c: vals[c] for c in _COLS}
        with self.db:
            last = self.db.execute("SELECT row_sha256 FROM orders ORDER BY id DESC LIMIT 1").fetchone()
            prev = last[0] if last else GENESIS
            cur = self.db.execute(
                f"INSERT INTO orders ({','.join(_COLS)},prev_sha256,row_sha256) "  # noqa: S608
                f"VALUES ({','.join('?' * (len(_COLS) + 2))})",
                [*values.values(), prev, row_hash(prev, values)],
            )
        return Order(**{**asdict(o), "id": int(cur.lastrowid or 0)})

    # integrity and the kill switch ----------------------------------------------------------------------------------
    def verify(self) -> dict[str, Any]:
        """Re-check both hash chains from the first row. Any edited, deleted or reordered row is a problem."""
        problems: list[str] = []
        prev, n = GENESIS, 0
        q_orders = f"SELECT id,{','.join(_COLS)},prev_sha256,row_sha256 FROM orders ORDER BY id"  # noqa: S608
        for r in self.db.execute(q_orders):
            n += 1
            values = dict(zip(_COLS, r[1:-2], strict=True))
            if r[-2] != prev:
                problems.append(f"order {r[0]}: does not follow the order before it")
            if r[-1] != row_hash(r[-2] or "", values):
                problems.append(f"order {r[0]}: contents do not match its hash")
            prev = r[-1] or ""
        prev_c, m = GENESIS, 0
        q_controls = f"SELECT id,{','.join(_CONTROL_COLS)},prev_sha256,row_sha256 FROM controls ORDER BY id"  # noqa: S608
        for r in self.db.execute(q_controls):
            m += 1
            values = dict(zip(_CONTROL_COLS, r[1:-2], strict=True))
            if r[-2] != prev_c or r[-1] != row_hash(r[-2], values):
                problems.append(f"kill-switch record {r[0]}: broken chain")
            prev_c = r[-1]
        return {"ok": not problems, "orders": n, "controls": m, "problems": problems}

    def set_control(self, pid: int, action: str, reason: str) -> None:
        if action not in ("halt", "resume"):
            raise ValueError("action must be halt or resume")
        values = {"portfolio_id": pid, "created_at": now_utc(), "action": action, "reason": reason}
        with self.db:
            last = self.db.execute("SELECT row_sha256 FROM controls ORDER BY id DESC LIMIT 1").fetchone()
            prev = last[0] if last else GENESIS
            self.db.execute(
                "INSERT INTO controls (portfolio_id,created_at,action,reason,prev_sha256,row_sha256) VALUES (?,?,?,?,?,?)",
                (*values.values(), prev, row_hash(prev, values)),
            )

    def halted(self, pid: int) -> dict[str, str] | None:
        """The kill switch's state: the halt in force (when and why), or None when trading is allowed."""
        r = self.db.execute(
            "SELECT action, created_at, reason FROM controls WHERE portfolio_id=? ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        return {"at": r[1], "reason": r[2]} if r and r[0] == "halt" else None

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
