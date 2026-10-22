"""The global company universe: every listed company Keers & Co can search, price and (where filings allow) value.

Read it with ``Universe(store).search(query)`` and ``Universe(store).get(key_or_symbol)``. Build it with
``python -m keers.universe build``. See docs/universe.md for the sources, what each field means and where
coverage is thin.
"""

from .directory import COVERAGE_PRICES, COVERAGE_SEC, Listing, Universe

__all__ = ["COVERAGE_PRICES", "COVERAGE_SEC", "Listing", "Universe"]
