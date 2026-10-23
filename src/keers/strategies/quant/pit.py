"""A read-only view of the store as of one date. Nothing dated after ``as_of`` can be read through it."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from keers.data import fundamentals as F
from keers.data import snapshot as S
from keers.data.models import Value, missing
from keers.data.snapshot import Snapshot
from keers.data.store import Store


class LookAheadError(RuntimeError):
    """A read returned data dated after the as-of date. Raised, never ignored."""


class PointInTime:
    def __init__(self, store: Store, as_of: date) -> None:
        self._store, self.as_of = store, as_of
        self._snaps: dict[str, Any] = {}

    def adjusted_closes(self, ticker: str, calendar_days: int) -> list[tuple[date, float]]:
        """(day, adjusted close) for the ``calendar_days`` up to and including ``as_of``; days without one dropped."""
        rows = self._store.adjusted_closes(ticker, self.as_of - timedelta(days=calendar_days), self.as_of)
        out = [(d, a) for d, a in rows if a is not None and a > 0]
        if any(d > self.as_of for d, _ in out):
            raise LookAheadError(f"{ticker}: price dated after {self.as_of}")
        return out

    def volumes(self, ticker: str, calendar_days: int) -> list[tuple[date, float, float]]:
        """(day, close, volume) from the best-ranked source, up to ``as_of``."""
        from keers.portfolio.market import daily_rows

        rows = daily_rows(self._store, ticker, self.as_of - timedelta(days=calendar_days), self.as_of)
        out = [(r[0], r[1], r[2]) for r in rows if r[2] is not None and r[2] > 0]
        if any(d > self.as_of for d, _, _ in out):
            raise LookAheadError(f"{ticker}: volume dated after {self.as_of}")
        return out


def _check_sources(ticker: str, as_of: date, sources: tuple[dict[str, Any], ...]) -> None:
    for s in sources:
        for key in ("filed", "day"):
            v = s.get(key)
            if isinstance(v, str) and len(v) >= 10 and date.fromisoformat(v[:10]) > as_of:
                raise LookAheadError(f"{ticker}: a figure {key} {v} is after {as_of}")


def snapshot(pit: PointInTime, ticker: str) -> tuple[Snapshot, dict[str, Any] | None, Value]:
    """(snapshot, company record, total assets at the accounts' period end), all as of ``pit.as_of``.

    Every cited filing date and price date is checked against ``as_of``; anything later raises.
    """
    key = ticker.upper()
    if key in pit._snaps:
        return pit._snaps[key]  # type: ignore[no-any-return]
    st, as_of = pit._store, pit.as_of
    snap = S.build(st, key, as_of)
    co = st.company(key)
    at = snap.values["revenue"].period_end if "revenue" in snap.values else None
    if co is not None and at is not None:
        assets = F.Fundamentals(st, co["cik"], as_of).balance(F.ASSETS, "total assets", at)
    else:
        assets = missing("m", "total assets: no accounts")
    for v in [*snap.values.values(), assets]:
        _check_sources(key, as_of, v.sources)
    out = (snap, co, assets)
    pit._snaps[key] = out
    return out
