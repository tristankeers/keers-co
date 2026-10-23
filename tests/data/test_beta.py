"""Beta from stored prices: exact on constructed series, missing (never guessed) when the inputs are not there."""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

import pytest

from keers.data.beta import BENCHMARK, MIN_WEEKS, beta
from keers.data.market import parse_tiingo_json
from keers.data.models import PricePoint
from keers.data.store import SCHEMA_VERSION, Store

AS_OF = date(2026, 10, 16)  # a Friday


def fridays(n: int, end: date = AS_OF) -> list[date]:
    return [end - timedelta(weeks=n - 1 - i) for i in range(n)]


def put_series(store: Store, ticker: str, days: list[date], returns: list[float], adj: bool = True) -> None:
    px, pts = 100.0, []
    for d, r in zip(days, [0.0, *returns], strict=True):
        px *= 1 + r
        pts.append(PricePoint(ticker, d, px * 3, "tiingo", raw_sha256="s", adj_close=px if adj else None))
    store.put_prices(pts)


def market(n: int, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.002, 0.02) for _ in range(n)]


def test_beta_recovers_a_known_slope_and_applies_blume() -> None:
    store, days = Store(":memory:"), fridays(105)
    m = market(104)
    put_series(store, BENCHMARK, days, m)
    put_series(store, "EXW", days, [1.5 * r + 0.001 for r in m])
    v = beta(store, "EXW", AS_OF)
    assert v.known and v.sources[0]["raw"] == pytest.approx(1.5)
    assert v.value == pytest.approx(0.67 * 1.5 + 0.33)
    assert v.sources[0]["r2"] == pytest.approx(1.0)


def test_beta_uses_adjusted_closes_not_raw_closes() -> None:
    # A 3-for-1 split shows in the raw close but not the adjusted one; beta must not see it.
    store, days = Store(":memory:"), fridays(105)
    m = market(104)
    put_series(store, BENCHMARK, days, m)
    put_series(store, "EXW", days, m)
    split = [PricePoint("EXW", d, 1.0, "tiingo", raw_sha256="s",
                        adj_close=store.adjusted_closes("EXW", d, d)[0][1]) for d in days[60:]]  # fmt: skip
    store.put_prices(split)
    assert beta(store, "EXW", AS_OF).sources[0]["raw"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("short", "weekly returns, needs"),
        ("no_benchmark", "no SPY prices"),
        ("stale_benchmark", "is stale"),
        ("unadjusted", "lack adjusted closes"),
    ],
)
def test_beta_is_missing_rather_than_guessed(case: str, reason: str) -> None:
    store = Store(":memory:")
    days = fridays(MIN_WEEKS if case == "short" else 105)
    m = market(len(days) - 1)
    if case != "no_benchmark":
        bdays = [d - timedelta(days=14) for d in days] if case == "stale_benchmark" else days
        put_series(store, BENCHMARK, bdays, m)
    put_series(store, "EXW", days, m, adj=case != "unadjusted")
    v = beta(store, "EXW", AS_OF)
    assert not v.known and v.value is None and reason in v.method


def test_tiingo_adjusted_close_is_parsed() -> None:
    t = parse_tiingo_json(
        [{"date": "2026-10-16T00:00:00.000Z", "close": 300.0, "adjClose": 100.0}], "exw", "s"
    )
    assert t[0].adj_close == 100.0
    assert parse_tiingo_json([{"date": "2026-10-16", "close": 3.0}], "exw", "s")[0].adj_close is None


def test_schema_1_store_is_migrated_in_place(tmp_path: object) -> None:
    path = f"{tmp_path}/old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE prices (ticker TEXT NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL, close REAL NOT NULL,"
        " open REAL, high REAL, low REAL, volume REAL, raw_sha256 TEXT NOT NULL, PRIMARY KEY (ticker, day, source));"
        "INSERT INTO prices VALUES ('EXW', '2026-10-16', 'tiingo', 40, NULL, NULL, NULL, NULL, 's');"
    )
    db.commit()
    db.close()
    store = Store(path)
    assert store.last_price("EXW") is not None
    assert store.adjusted_closes("EXW", AS_OF, AS_OF) == [(AS_OF, None)]  # old rows: adjusted close unknown
    store.put_prices([PricePoint("EXW", AS_OF, 40.0, "tiingo", raw_sha256="s", adj_close=39.5)])
    assert store.adjusted_closes("EXW", AS_OF, AS_OF) == [(AS_OF, 39.5)]
    version = store.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert int(version) == SCHEMA_VERSION == 6
