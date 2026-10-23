"""A read-only view of the store as of one date. Nothing dated after ``as_of`` can be read through it."""

from __future__ import annotations

from datetime import date, timedelta

from keers.data.store import Store


class LookAheadError(RuntimeError):
    """A read returned data dated after the as-of date. Raised, never ignored."""


class PointInTime:
    def __init__(self, store: Store, as_of: date) -> None:
        self._store, self.as_of = store, as_of

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
