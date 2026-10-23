"""What the workbench shows, assembled from the data layer, the valuation engines and the distress screen.

Nothing here makes up a number. Every figure is either read from the store (with its provenance), produced by a
versioned engine, or derived in this module by a formula stated next to it. Each figure carries a ``label``:

- ``reported``: as filed with the SEC
- ``derived``: calculated in code from reported figures (the method says how)
- ``market``: a market price or rate
- ``assumption``: an input to a model, chosen by the calculation version's defaults or by the user
- ``estimate``: a model output

and ``stale: true`` when the data behind it is older than the freshness rules allow.

Live fetching is optional (``Live``). With it off, every view is built from the store and says so.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

from keers.data import snapshot as S
from keers.data import yahoo
from keers.data.beta import BENCHMARK
from keers.data.fundamentals import ALL_CONCEPTS
from keers.data.http import Fetcher, FetchError
from keers.data.market import Market
from keers.data.models import Value
from keers.data.refresh import PRICE_HISTORY_DAYS
from keers.data.sec import SEC, TICKERS_URL, ParseError
from keers.data.store import Store
from keers.portfolio import market as PM
from keers.portfolio.desk import Desk, TradeError, TradeRequest
from keers.portfolio.ledger import Ledger
from keers.risk.limits import Limits
from keers.strategies import distress
from keers.valuation import kci_v1_defaults as V1
from keers.valuation import kci_v2_defaults as V2

from . import sources
from .directory import (
    CompositeDirectory,
    Directory,
    Listing,
    SecTickerDirectory,
    StoreDirectory,
    UniverseDirectory,
    YahooSearchDirectory,
)

log = logging.getLogger("keers.web")

LIVE_TIMEOUT = 15  # seconds per network operation
LIVE_RETRIES = 1
LOCK_WAIT = 20  # seconds a page waits for its turn at the live sources before using stored data
SEARCH_LOCK_WAIT = 2  # search is typed live, so it never queues behind a refresh

LIVE_ERRORS = (FetchError, ParseError, KeyError, ValueError, OSError)
NEWS_MAX_AGE = 600  # seconds between live headline fetches for one company
QUOTE_MAX_AGE = 30
BARS_MAX_AGE = 60
REFRESH_MIN_GAP = 60
REFRESH_LOCK_WAIT = 45
CHART_DAYS = PRICE_HISTORY_DAYS

YAHOO_NOTICE = (
    "Prices, charts and headlines come from Yahoo Finance: an unofficial source, for personal research only. "
    "It must be replaced by a licensed feed before any commercial or regulated use."
)

# Profile figures, in display order: (key, title, group)
FIGURES: tuple[tuple[str, str, str], ...] = (
    ("price", "Share price (last close)", "Market"),
    ("shares", "Shares outstanding", "Market"),
    ("marketCap", "Market capitalisation", "Market"),
    ("netDebt", "Net debt", "Market"),
    ("ev", "Enterprise value", "Market"),
    ("revenue", "Revenue (TTM)", "Income statement"),
    ("revenueGrowth", "Revenue growth (TTM, year on year)", "Income statement"),
    ("ebitda", "EBITDA (TTM)", "Income statement"),
    ("ebit", "Operating income (TTM)", "Income statement"),
    ("da", "Depreciation and amortisation (TTM)", "Income statement"),
    ("netIncome", "Net income (TTM)", "Income statement"),
    ("sbc", "Share-based compensation (TTM)", "Income statement"),
    ("capex", "Capital expenditure (TTM)", "Cash flow"),
    ("interestExpense", "Interest expense (TTM)", "Cash flow"),
    ("debt", "Total borrowings (incl. finance leases)", "Balance sheet"),
    ("liquidAssets", "Cash and short-term investments", "Balance sheet"),
    ("operatingLeases", "Operating lease liabilities (not in net debt)", "Balance sheet"),
    ("beta", f"Beta (weekly vs {BENCHMARK}, Blume-adjusted)", "Risk and rates"),
    ("rate_DGS10", "10-year Treasury yield", "Risk and rates"),
    ("rate_BAA10Y", "Baa corporate spread over Treasuries", "Risk and rates"),
)
FUNDAMENTAL_KEYS = {k for k, _, g in FIGURES if g in ("Income statement", "Cash flow", "Balance sheet")}


# --------------------------------------------------------------------------------------------------------------
# Live sources
# --------------------------------------------------------------------------------------------------------------
@dataclass
class LiveConfig:
    sec_user_agent: str | None = None
    tiingo_token: str | None = None
    price_sources: tuple[str, ...] = ("yahoo", "tiingo")


@dataclass
class Live:
    """Live fetching, one request at a time (polite to every source, and SQLite has one writer).

    A page must never hang on a slow source. Each request has a short timeout and one retry, and a request that
    cannot get its turn within a few seconds gives up with a clear message, so the view falls back to stored data.
    Every live call is logged with how long it took.
    """

    config: LiveConfig
    fetcher: Fetcher | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    _last: dict[tuple[str, ...], float] = field(default_factory=dict)
    busy_with: str | None = None

    def __post_init__(self) -> None:
        if self.fetcher is None:
            self.fetcher = Fetcher(
                self.config.sec_user_agent or "keers-research", retries=LIVE_RETRIES, timeout=LIVE_TIMEOUT
            )

    @contextmanager
    def slot(self, what: str, wait: float = LOCK_WAIT) -> Iterator[None]:
        """Hold the live-source lock for one job, waiting at most ``wait`` seconds for it."""
        if not self.lock.acquire(timeout=wait):
            log.warning("live: %s skipped: still busy with %s after %.0fs", what, self.busy_with, wait)
            raise FetchError(
                f"live sources are busy ({self.busy_with or 'another request'}); showing stored data"
            )
        self.busy_with = what
        t = time.monotonic()
        try:
            yield
        except Exception as e:
            log.warning("live: %s failed after %.1fs: %s", what, time.monotonic() - t, str(e)[:300])
            raise
        else:
            log.info("live: %s done in %.1fs", what, time.monotonic() - t)
        finally:
            self.busy_with = None
            self.lock.release()

    @property
    def sec_enabled(self) -> bool:
        return bool(self.config.sec_user_agent)

    def market(self, store: Store) -> Market:
        assert self.fetcher is not None  # noqa: S101
        return Market(self.fetcher, store, self.config.tiingo_token, self.config.price_sources)

    def sec(self, store: Store) -> SEC:
        if not self.sec_enabled:
            raise FetchError(
                "SEC_USER_AGENT is not set; SEC requires a name and contact before it serves filings"
            )
        assert self.fetcher is not None  # noqa: S101
        return SEC(self.fetcher, store)

    def due(self, key: tuple[str, ...], seconds: float) -> bool:
        """True (and marks the time) when ``key`` was not fetched in the last ``seconds``."""
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < seconds:
            return False
        self._last[key] = now
        return True


# --------------------------------------------------------------------------------------------------------------
# Labels and provenance
# --------------------------------------------------------------------------------------------------------------
def sec_filing_url(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"


def source_link(cite: Mapping[str, Any], cik: int | None, ticker: str) -> str | None:
    if "accn" in cite and cik:
        return sec_filing_url(cik, str(cite["accn"]))
    src = cite.get("source")
    if src == "fred" and cite.get("series"):
        return f"https://fred.stlouisfed.org/series/{urlquote(str(cite['series']))}"
    if src == "yahoo":
        return f"https://finance.yahoo.com/quote/{urlquote(sources.symbol(ticker))}/history"
    if src == "tiingo":
        return f"https://www.tiingo.com/{urlquote(ticker.lower())}/overview"
    return None


def describe_source(cite: Mapping[str, Any]) -> str:
    if "accn" in cite:
        period = f"{cite['start']} to {cite['end']}" if cite.get("start") else f"at {cite['end']}"
        return f"{cite.get('form', '')} filed {cite.get('filed', '')}: {cite.get('concept', '')}, {period}"
    if cite.get("source") == "fred":
        return f"FRED {cite.get('series')} on {cite.get('day')}"
    if cite.get("source") == "prices":
        return (f"{cite.get('weeks')} weekly returns of {cite.get('ticker')} against {cite.get('benchmark')} "
                f"(raw beta {fmt(cite.get('raw'))}, R² {fmt(cite.get('r2'))})")  # fmt: skip
    if cite.get("source"):
        return f"{cite['source']} close on {cite.get('day')}"
    return ""


def fmt(x: Any) -> str:
    return f"{x:.3f}" if isinstance(x, float) else str(x)


def labelled(v: Value, cik: int | None, ticker: str, stale: bool = False) -> dict[str, Any]:
    d = v.as_dict()
    d["label"] = d.pop("kind")
    d["stale"] = stale
    d["sources"] = [
        {**s, "link": source_link(s, cik, ticker), "text": describe_source(s)} for s in d["sources"]
    ]
    return d


def figure(value: float | None, unit: str, label: str, method: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "unit": unit, "label": label, "method": method, "sources": [],
                           "warnings": [], "stale": False}  # fmt: skip
    out.update(extra)
    return out


def _known(v: Value | None) -> float | None:
    return v.value if v is not None and v.known else None


def ratio(a: Value | None, b: Value | None, method: str, positive_denominator: bool = True) -> dict[str, Any]:
    """a / b as a derived figure; missing (with the reason) when either side is unknown or b is not positive."""
    x, y = _known(a), _known(b)
    if x is None or y is None:
        return figure(None, "x", "derived", method, warnings=["an input is missing"])
    if y == 0 or (positive_denominator and y < 0):
        return figure(None, "x", "derived", method, warnings=["not meaningful: denominator is not positive"])
    return figure(x / y, "x", "derived", method)


def stale_flags(snap: S.Snapshot) -> tuple[bool, bool]:
    """(price is stale, fundamentals are stale), from the snapshot's own freshness rules."""
    return (any(b.startswith("stale price") for b in snap.blocked_by),
            any(b.startswith("stale fundamentals") for b in snap.blocked_by))  # fmt: skip


# --------------------------------------------------------------------------------------------------------------
# Valuation inputs the user may change, with the bounds a value must sit within
# --------------------------------------------------------------------------------------------------------------
Bounds = tuple[float, float]
DCF_EDITABLE: dict[str, Bounds] = {
    "beta": (0.0, 5.0), "riskFree": (-0.02, 0.2), "erp": (0.0, 0.2), "sizePremium": (0.0, 0.1),
    "preTaxCostOfDebt": (0.0, 0.3), "debtWeight": (0.0, 0.95), "taxRate": (0.0, 0.6), "terminalGrowth": (-0.05, 0.06),
    "exitMultiple": (0.0, 100.0), "nwcPct": (-1.0, 1.0), "daPct": (0.0, 1.0),
    "revenueGrowth": (-0.9, 3.0), "ebitdaMargin": (-2.0, 1.0), "capexPct": (0.0, 1.0),
}  # fmt: skip
LBO_EDITABLE: dict[str, Bounds] = {
    "entryEVOverride": (0.0, 1e9), "exitMultiple": (0.0, 100.0), "transactionFeePct": (0.0, 0.2),
    "financingFeePct": (0.0, 0.2), "cashSweepPct": (0.0, 1.0), "interestOnCashRate": (0.0, 0.2),
    "nwcPct": (-1.0, 1.0), "taxRate": (0.0, 0.6), "exitFeePct": (0.0, 0.2), "daPct": (0.0, 1.0),
    "revenueGrowth": (-0.9, 3.0), "ebitdaMargin": (-2.0, 1.0), "capexPct": (0.0, 1.0),
    "tranches.0.leverage": (0.0, 15.0), "tranches.0.rate": (0.0, 0.4), "tranches.1.leverage": (0.0, 15.0),
    "tranches.1.rate": (0.0, 0.4),
}  # fmt: skip


class InputError(ValueError):
    """An edited valuation input was refused. The message says which and why."""


def _check_number(path: str, v: Any, bounds: Bounds) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise InputError(f"{path}: must be a number")
    lo, hi = bounds
    if not lo <= v <= hi:
        raise InputError(f"{path}: {v} is outside the allowed range {lo} to {hi}")
    return float(v)


def apply_overrides(
    base: Mapping[str, Any], edits: Mapping[str, Any], allowed: Mapping[str, Bounds]
) -> dict[str, Any]:
    """A copy of ``base`` with ``edits`` applied. Only listed keys; lists keep their length; numbers stay in bounds."""
    out = copy.deepcopy(dict(base))
    for path, new in edits.items():
        if path not in allowed:
            raise InputError(f"{path}: not an editable input")
        parts = path.split(".")
        holder: Any = out
        for p in parts[:-1]:
            holder = holder[int(p)] if isinstance(holder, list) else holder[p]
        key: Any = int(parts[-1]) if isinstance(holder, list) else parts[-1]
        old = holder[key]
        if isinstance(old, list) or isinstance(new, list):
            n = len(old) if isinstance(old, list) else 1
            if not isinstance(new, list) or len(new) != n:
                raise InputError(f"{path}: give {n} yearly value(s)")
            holder[key] = [_check_number(f"{path}[{i}]", x, allowed[path]) for i, x in enumerate(new)]
        else:
            holder[key] = _check_number(path, new, allowed[path])
    return out


# --------------------------------------------------------------------------------------------------------------
# The workbench
# --------------------------------------------------------------------------------------------------------------
class Workbench:
    def __init__(
        self,
        db_path: str,
        live: Live | None = None,
        data_dir: Path | None = None,
        today: Callable[[], date] = date.today,
        extra_directories: list[Directory] | None = None,
        paper_db: str | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.db_path = db_path
        self.paper_db = paper_db or str(Path(db_path).resolve().parent / "paper.sqlite")
        self.limits = limits or Limits()
        self.paper_lock = threading.Lock()  # one paper trade at a time, so each sees the last one's cash
        self.live = live
        self.today = today
        data_dir = data_dir or Path(db_path).resolve().parent
        backings: list[Directory] = []
        if UniverseDirectory.available():
            backings.append(UniverseDirectory(self.open_store))
        backings += extra_directories or []
        backings.append(StoreDirectory(self.open_store))
        backings.append(SecTickerDirectory(data_dir / "sec_company_tickers.json",
                                           self._fetch_sec_tickers if live and live.sec_enabled else None))  # fmt: skip
        # Local backings answer from this machine in milliseconds, so suggestions can appear as you type.
        # Yahoo's search is live and slow; it is asked separately (search(remote=True)) and only adds what is missing.
        self.local_directory = CompositeDirectory(backings)
        self.remote_directory = YahooSearchDirectory(self._yahoo_search) if live is not None else None
        self.directory = CompositeDirectory(
            backings + ([self.remote_directory] if self.remote_directory else [])
        )

    # plumbing ---------------------------------------------------------------------------------------------------
    def open_store(self) -> Store:
        return Store(self.db_path, keep_bodies=False)

    @contextmanager
    def store(self) -> Iterator[Store]:
        s = self.open_store()
        try:
            yield s
        finally:
            s.close()

    def _fetch_sec_tickers(self) -> tuple[bytes, str]:
        assert self.live is not None and self.live.fetcher is not None  # noqa: S101
        with self.live.slot("SEC ticker map"):
            r = self.live.fetcher.get(TICKERS_URL)
        return r.body, r.retrieved_at

    def _yahoo_search(self, query: str, count: int) -> list[dict[str, str]]:
        assert self.live is not None  # noqa: S101
        assert self.live.fetcher is not None  # noqa: S101
        if len(query.strip()) < 2:
            return []
        with self.live.slot(f"Yahoo search {query!r}", SEARCH_LOCK_WAIT):
            return sources.yahoo_search(self.live.fetcher, query, count)

    # status -----------------------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.store() as st:
            companies = sources.companies(st)
            runs = [r.__dict__ for r in st.runs(5)]
            rates = {
                s: (r.value, r.day.isoformat()) if (r := st.last_rate(s)) else None
                for s in ("DGS10", "BAA10Y")
            }
        return {
            "today": self.today().isoformat(),
            "live": self.live is not None,
            "secEnabled": bool(self.live and self.live.sec_enabled),
            "priceSources": list(self.live.config.price_sources) if self.live else [],
            "tiingo": bool(self.live and self.live.config.tiingo_token),
            "directory": self.directory.sources,
            "universe": UniverseDirectory.available(),
            "companies": companies,
            "runs": runs,
            "rates": rates,
            "calcVersions": sorted(S.ENGINES),
            "defaultCalcVersion": S.DEFAULT_CALC_VERSION,
            "screenVersion": distress.SCREEN_VERSION,
            "notice": YAHOO_NOTICE,
        }

    def search(self, query: str, limit: int = 20, remote: bool = False) -> dict[str, Any]:
        """Local directories only (fast, for suggestions as you type), or with ``remote`` only Yahoo's extra hits."""
        q = query.strip()[:60]
        if not remote:
            hits = self.local_directory.search(q, limit) if q else []
            return {"query": q, "remote": False, "results": [h.as_dict() for h in hits],
                    "sources": self.local_directory.sources}  # fmt: skip
        if self.remote_directory is None or not q:
            return {"query": q, "remote": True, "results": [], "sources": []}
        known = {x.symbol for x in self.local_directory.search(q, limit)}
        extra = [x for x in self.remote_directory.search(q, limit) if x.symbol not in known]
        return {"query": q, "remote": True, "results": [x.as_dict() for x in extra],
                "sources": [self.remote_directory.name]}  # fmt: skip

    def listing(self, symbol: str) -> Listing:
        found = self.local_directory.get(symbol)
        if found is None and self.remote_directory is not None:
            found = self.remote_directory.get(symbol)
        if found is not None:
            return found
        sym = sources.symbol(symbol)
        return Listing(key=sym, symbol=sym, name="", source="not in the directory", coverage="prices-only")

    # refresh ----------------------------------------------------------------------------------------------------
    def refresh(self, symbol: str) -> dict[str, Any]:
        """Fetch filings (SEC filers), prices, rates and the beta benchmark now. Every failure is reported."""
        if self.live is None:
            return {"ok": False, "errors": {"live": "live sources are off (KEERS_OFFLINE)"}, "done": {}}
        lst = self.listing(symbol)
        sym = lst.symbol
        if not self.live.due(("refresh", sym), REFRESH_MIN_GAP):
            return {"ok": False, "symbol": sym, "done": {},
                    "errors": {"refresh": f"refreshed less than {REFRESH_MIN_GAP} seconds ago; try again shortly"}}  # fmt: skip
        today = self.today()
        start = today - timedelta(days=PRICE_HISTORY_DAYS)
        done: dict[str, Any] = {}
        errors: dict[str, str] = {}
        try:
            with self.live.slot(f"refresh {sym}", REFRESH_LOCK_WAIT), self.store() as st:
                self._refresh_locked(st, lst, sym, today, start, done, errors)
        except FetchError as e:  # could not get a turn at the live sources
            errors["live"] = str(e)
        return {"ok": not errors, "symbol": sym, "done": done, "errors": errors}

    def _refresh_locked(self, st: Store, lst: Listing, sym: str, today: date, start: date,
                        done: dict[str, Any], errors: dict[str, str]) -> None:  # fmt: skip
        assert self.live is not None  # noqa: S101
        market = self.live.market(st)
        if lst.cik is not None or (self.live.sec_enabled and not sources.is_foreign(sym)):
            try:
                done["filings"] = self.live.sec(st).refresh_company(sym, ALL_CONCEPTS)
            except KeyError:
                done["filings"] = {"note": f"{sym} is not in the SEC ticker map; prices only"}
            except LIVE_ERRORS as e:
                errors["filings"] = str(e)
        try:
            done["prices"] = market.refresh_prices(sym, start)
        except LIVE_ERRORS as e:
            errors["prices"] = str(e)
        bench = st.last_price(BENCHMARK)
        if bench is None or S.business_days_between(bench.day, today) > 1:
            try:
                done["benchmark"] = market.refresh_prices(BENCHMARK, start)
            except LIVE_ERRORS as e:
                errors["benchmark"] = str(e)
        rate = st.last_rate("DGS10")
        if rate is None or S.business_days_between(rate.day, today) > 2:
            try:
                done["rates"] = market.refresh_rates()
            except LIVE_ERRORS as e:
                errors["rates"] = str(e)
        run = st.start_run("workbench-refresh")
        st.finish_run(run, "ok" if not errors else ("partial" if done else "failed"),
                      {"symbol": sym, "ok": sorted(done), "failed": errors})  # fmt: skip

    # company profile --------------------------------------------------------------------------------------------
    def company(self, symbol: str) -> dict[str, Any]:
        lst = self.listing(symbol)
        sym = lst.symbol
        as_of = self.today()
        with self.store() as st:
            co = st.company(sym)
            snap = S.build(st, sym, as_of) if co else None
            filings = (
                st.filings(co["cik"], ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F")) if co else []
            )
            q = st.last_quote(sym)
            px = st.last_price(sym)
        listing = lst.as_dict()
        if co and listing.get("cik") is None:
            listing["cik"] = co["cik"]
        out: dict[str, Any] = {
            "symbol": sym,
            "listing": listing,
            "loaded": co is not None or px is not None,
            "sec": None,
            "filings": [
                {
                    "form": f.form,
                    "filed": f.filing_date.isoformat(),
                    "period": f.report_date.isoformat() if f.report_date else None,
                    "link": sec_filing_url(f.cik, f.accn),
                    "accn": f.accn,
                }
                for f in reversed(filings[-10:])
            ],  # fmt: skip
            "quote": self._quote_payload(q) if q else None,
            "lastClose": None,
            "snapshot": None,
            "figures": [],
            "ratios": [],
            "notice": YAHOO_NOTICE,
        }
        if px is not None:
            age = S.business_days_between(px.day, as_of)
            out["lastClose"] = figure(px.close, lst.currency or "", "market", f"{px.source} close on {px.day}",
                                      periodEnd=px.day.isoformat(), stale=age > S.PRICE_MAX_BUSINESS_DAYS,
                                      sources=[{"source": px.source, "day": px.day.isoformat(),
                                                "link": source_link({"source": px.source}, None, sym),
                                                "text": f"{px.source} close on {px.day}"}])  # fmt: skip
        if co and snap is not None:
            out["sec"] = {"cik": co["cik"], "name": co["name"], "sic": co["sic"], "fiscalYearEnd": co["fiscalYearEnd"],
                          "link": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={co['cik']}"}  # fmt: skip
            price_stale, fund_stale = stale_flags(snap)
            v = snap.values
            out["snapshot"] = {"asOf": snap.as_of.isoformat(), "blocked": snap.blocked, "blockedBy": snap.blocked_by,
                               "warnings": snap.warnings}  # fmt: skip
            for key, title, group in FIGURES:
                if key not in v:
                    continue
                stale = (key == "price" and price_stale) or (key in FUNDAMENTAL_KEYS and fund_stale)
                out["figures"].append({"key": key, "title": title, "group": group,
                                       **labelled(v[key], co["cik"], sym, stale)})  # fmt: skip
            out["ratios"] = [
                {"key": "evEbitda", "title": "EV / EBITDA", **ratio(v.get("ev"), v.get("ebitda"), "enterprise value ÷ EBITDA (TTM)")},
                {"key": "evRevenue", "title": "EV / revenue", **ratio(v.get("ev"), v.get("revenue"), "enterprise value ÷ revenue (TTM)")},
                {"key": "pe", "title": "Price / earnings", **ratio(v.get("marketCap"), v.get("netIncome"), "market cap ÷ net income (TTM)")},
                {"key": "leverage", "title": "Net debt / EBITDA", **ratio(v.get("netDebt"), v.get("ebitda"), "net debt ÷ EBITDA (TTM)")},
            ]  # fmt: skip
        return out

    def _quote_payload(self, q: Any) -> dict[str, Any]:
        d: dict[str, Any] = q.as_dict()
        stale = any(w.startswith("stale") for w in q.warnings)
        d.update(label="market", stale=stale, delayed=any("delayed" in w for w in q.warnings))
        return d

    def quote(self, symbol: str) -> dict[str, Any]:
        sym = sources.symbol(symbol)
        error = None
        if self.live is not None and self.live.due(("quote", sym), QUOTE_MAX_AGE):
            try:
                with self.live.slot(f"Yahoo quote {sym}"), self.store() as st:
                    self.live.market(st).quote(sym)
            except LIVE_ERRORS as e:
                error = str(e)
        with self.store() as st:
            q = st.last_quote(sym)
        return {"symbol": sym, "quote": self._quote_payload(q) if q else None, "error": error,
                "fetchedFrom": "store" if error or self.live is None else "yahoo"}  # fmt: skip

    # charts -----------------------------------------------------------------------------------------------------
    def _refresh_daily(self, sym: str, notes: list[str]) -> str | None:
        """Fetch daily prices when the stored ones are more than a business day old. Returns the error, if any."""
        today = self.today()
        with self.store() as st:
            last = st.last_price(sym)
        if self.live is None or (last is not None and S.business_days_between(last.day, today) <= 1) \
                or not self.live.due(("daily", sym), BARS_MAX_AGE):  # fmt: skip
            return None
        try:
            with self.live.slot(f"daily prices {sym}"), self.store() as st:
                res = self.live.market(st).refresh_prices(sym, today - timedelta(days=CHART_DAYS))
        except LIVE_ERRORS as e:
            return str(e)
        notes += res.get("notes", [])
        if res.get("fallbackFrom"):
            notes.append(
                f"fell back to {res['source']}: "
                + "; ".join(f"{k}: {v}" for k, v in res["fallbackFrom"].items())
            )
        return None

    def bars(self, symbol: str, interval: str = "1d") -> dict[str, Any]:
        sym = sources.symbol(symbol)
        today = self.today()
        error = None
        notes: list[str] = []
        if interval == "1d":
            error = self._refresh_daily(sym, notes)
            with self.store() as st:
                pts = sources.price_history(st, sym, today - timedelta(days=CHART_DAYS), today)
            rows = []
            filled = 0
            for p in pts:
                if p.open is None or p.high is None or p.low is None:
                    filled += 1
                rows.append({"time": p.day.isoformat(), "open": p.open if p.open is not None else p.close,
                             "high": p.high if p.high is not None else p.close,
                             "low": p.low if p.low is not None else p.close, "close": p.close, "volume": p.volume})  # fmt: skip
            if filled:
                notes.append(f"{filled} day(s) have only a close; drawn as flat bars")
            last_day = pts[-1].day if pts else None
            age = S.business_days_between(last_day, today) if last_day else None
            return {"symbol": sym, "interval": "1d", "source": pts[0].source if pts else None, "bars": rows,
                    "last": last_day.isoformat() if last_day else None,
                    "stale": age is None or age > S.PRICE_MAX_BUSINESS_DAYS, "notes": notes, "error": error,
                    "adjusted": False, "timezone": "exchange trading date"}  # fmt: skip
        if interval not in yahoo.INTERVALS:
            raise InputError(f"interval must be 1d or one of {', '.join(yahoo.INTERVALS)}")
        if self.live is not None and self.live.due(("bars", sym, interval), BARS_MAX_AGE):
            try:
                with self.live.slot(f"{interval} bars {sym}"), self.store() as st:
                    res = self.live.market(st).refresh_bars(sym, interval)
                notes += res.get("notes", [])
            except LIVE_ERRORS as e:
                error = str(e)
        span = {"1m": 5, "2m": 20, "5m": 30, "15m": 60, "30m": 60, "60m": 365, "90m": 60}[interval]
        now = min(datetime.now(UTC), datetime.combine(today + timedelta(days=1), dtime(), UTC))
        with self.store() as st:
            bars = st.bars(sym, interval, now - timedelta(days=span))
        rows = [{"time": int(b.start.timestamp()), "open": b.open if b.open is not None else b.close,
                 "high": b.high if b.high is not None else b.close, "low": b.low if b.low is not None else b.close,
                 "close": b.close, "volume": b.volume} for b in bars]  # fmt: skip
        last_bar = bars[-1].start if bars else None
        stale = last_bar is None or (now - last_bar) > timedelta(days=4)
        return {"symbol": sym, "interval": interval, "source": bars[0].source if bars else None, "bars": rows,
                "last": last_bar.isoformat() if last_bar else None, "stale": stale, "notes": notes, "error": error,
                "adjusted": False, "timezone": "UTC"}  # fmt: skip

    # news -------------------------------------------------------------------------------------------------------
    def news(self, symbol: str, count: int = 15) -> dict[str, Any]:
        sym = sources.symbol(symbol)
        error = None
        if self.live is not None and self.live.due(("news", sym), NEWS_MAX_AGE):
            try:
                with self.live.slot(f"Yahoo headlines {sym}"), self.store() as st:
                    self.live.market(st).refresh_news(sym, count)
            except LIVE_ERRORS as e:
                error = str(e)
        with self.store() as st:
            items = [n.as_dict() for n in st.news(sym, count)]
        return {"symbol": sym, "trust": "untrusted-third-party-text", "items": items, "error": error,
                "note": "Headlines are third-party text, shown as published. Nothing here is checked or endorsed."}  # fmt: skip

    # distress ---------------------------------------------------------------------------------------------------
    def distress(self, symbol: str) -> dict[str, Any]:
        sym = sources.symbol(symbol)
        as_of = self.today()
        with self.store() as st:
            co = st.company(sym)
            if co is None:
                return {"symbol": sym, "status": "not available", "checks": [], "screenVersion": distress.SCREEN_VERSION,
                        "blockedBy": ["no SEC filings in the store for this listing; the screen reads filed accounts"],
                        "credit": distress.credit_backdrop(st, as_of)}  # fmt: skip
            res = distress.screen(st, sym, as_of)
            res["credit"] = distress.credit_backdrop(st, as_of)
        for c in res.get("checks", []):
            for k, inp in (c.get("inputs") or {}).items():
                if isinstance(inp, dict) and "kind" in inp:
                    inp["label"] = inp.pop("kind")
                    inp["sources"] = [{**s, "link": source_link(s, co["cik"], sym), "text": describe_source(s)}
                                      for s in inp.get("sources", [])]  # fmt: skip
                    c["inputs"][k] = inp
        res["thresholds"] = {"levels": list(distress.LEVELS)}
        return res

    # valuation --------------------------------------------------------------------------------------------------
    def valuation(self, symbol: str, edits: Mapping[str, Any] | None = None) -> dict[str, Any]:
        sym = sources.symbol(symbol)
        with self.store() as st:
            co = st.company(sym)
            if co is None:
                return {"symbol": sym, "ran": False, "calcVersion": V2.CALC_VERSION,
                        "blockedBy": ["no SEC filings in the store for this listing; the engine needs filed accounts"]}  # fmt: skip
            snap = S.build(st, sym, self.today())
        if snap.blocked:
            return {"symbol": sym, "ran": False, "calcVersion": V2.CALC_VERSION, "blockedBy": snap.blocked_by,
                    "asOf": snap.as_of.isoformat()}  # fmt: skip
        deal = S.to_kci_deal(snap)
        edits = edits or {}
        lbo_default, dcf_default = V2.default_lbo(deal), V2.default_dcf(deal)
        custom: dict[str, Any] = {}
        v2_deal = dict(deal)
        if edits.get("lbo"):
            v2_deal["lbo"] = apply_overrides(lbo_default, edits["lbo"], LBO_EDITABLE)
            custom["lbo"] = sorted(edits["lbo"])
        if edits.get("dcf"):
            v2_deal["dcf"] = apply_overrides(dcf_default, edits["dcf"], DCF_EDITABLE)
            custom["dcf"] = sorted(edits["dcf"])
        v2 = V2.analyse(v2_deal)
        v1 = V1.analyse(deal)
        shares = _known(snap.values.get("shares"))
        price = _known(snap.values.get("price"))
        return {
            "symbol": sym,
            "ran": True,
            "asOf": snap.as_of.isoformat(),
            "calcVersion": v2["calcVersion"],
            "engineVersion": v2["engineVersion"],
            "custom": custom,
            "editable": {"lbo": LBO_EDITABLE, "dcf": DCF_EDITABLE},
            "defaults": {"lbo": lbo_default, "dcf": dcf_default},
            "changes": v2["changes"],
            "inputs": self._deal_inputs(snap),
            "v2": _trim(v2),
            "perShare": per_share(v2, shares, price),
            "comparison": compare(v1, v2),
            "dataStatus": deal["dataStatus"],
            "warnings": snap.warnings,
        }

    def _deal_inputs(self, snap: S.Snapshot) -> list[dict[str, Any]]:
        keys = ("revenue", "ebitda", "ebit", "netIncome", "capex", "netDebt", "ev", "marketCap", "revenueGrowth",
                "beta", "rate_DGS10", "price", "shares")  # fmt: skip
        titles = dict((k, t) for k, t, _ in FIGURES)
        price_stale, fund_stale = stale_flags(snap)
        return [{"key": k, "title": titles.get(k, k),
                 **labelled(snap.values[k], snap.cik, snap.ticker, (k == "price" and price_stale) or (k in FUNDAMENTAL_KEYS and fund_stale))}
                for k in keys if k in snap.values]  # fmt: skip

    # paper portfolios ------------------------------------------------------------------------------------------
    def paper_mark(self, symbol: str) -> PM.MarketMark:
        """The latest Yahoo price for a paper fill or mark (fetched first when live), with ADV and a sector hint."""
        sym = sources.symbol(symbol)
        if self.live is not None:
            self.quote(sym)  # refreshes the stored quote at most every QUOTE_MAX_AGE seconds
            self._refresh_daily(sym, [])
        found = self.local_directory.get(sym)
        with self.store() as st:
            m = PM.mark(st, sym, self.today(), (found.currency if found else None) or None, self.limits.adv_days,
                        self.limits.min_adv_days)  # fmt: skip
            co = st.company(sym)
        if found is not None and found.sector:
            return replace(m, sector=found.sector, sector_basis="listing")
        sic_sector = PM.sector_from_sic(co.get("sic") if co else None)
        if sic_sector:
            return replace(m, sector=sic_sector, sector_basis="sic")
        return m

    def _closes(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
        with self.store() as st:
            return PM.closes(st, symbol, start, end)

    @contextmanager
    def desk(self) -> Iterator[Desk]:
        ledger = Ledger(self.paper_db)
        try:
            yield Desk(ledger, self.paper_mark, self._closes, self.today, self.limits)
        finally:
            ledger.close()

    def portfolios(self) -> dict[str, Any]:
        with self.desk() as d:
            ps = d.ledger.portfolios()
        return {
            "portfolios": [p.as_dict() for p in ps],
            "limits": self.limits.as_dict(),
            "notice": PAPER_NOTICE,
        }

    def create_portfolio(self, name: str, starting_cash: Any) -> dict[str, Any]:
        try:
            cash = float(starting_cash)
        except (TypeError, ValueError) as e:
            raise InputError("starting cash must be a number") from e
        with self.paper_lock, self.desk() as d:
            try:
                p = d.ledger.create_portfolio(name, cash, self.limits.base_currency)
            except ValueError as e:
                raise InputError(str(e)) from e
        return p.as_dict()

    def portfolio(self, pid: int) -> dict[str, Any]:
        with self.desk() as d:
            try:
                out = d.view(pid)
            except TradeError as e:
                raise InputError(str(e)) from e
        out["notice"] = PAPER_NOTICE
        return out

    def paper_trade(self, pid: int, body: Mapping[str, Any], execute: bool) -> dict[str, Any]:
        with self.paper_lock, self.desk() as d:
            try:
                req = TradeRequest.from_dict(dict(body))
                return d.execute(pid, req) if execute else d.preview(pid, req)
            except TradeError as e:
                raise InputError(str(e)) from e

    # research agents --------------------------------------------------------------------------------------------
    def research(
        self, symbol: str, strategy: str, portfolio_id: Any = None, execute: bool = False
    ) -> dict[str, Any]:
        """Run the analyst, bear reviewer and memo writer (``keers.agents``). A proposed trade goes through the
        paper desk and the risk limits like any other; the agents cannot place one any other way."""
        from keers.agents import committee
        from keers.agents.llm import from_env

        pid = None
        if portfolio_id not in (None, ""):
            try:
                pid = int(portfolio_id)
            except (TypeError, ValueError) as e:
                raise InputError("portfolio must be a number") from e
        llm, why = from_env()
        try:
            r = committee.run(self, symbol, strategy, llm, why, pid, bool(execute),
                              out_dir=Path(self.db_path).resolve().parent / "agent-runs")  # fmt: skip
        except committee.RunError as e:
            raise InputError(str(e)) from e
        return r.as_dict()


PAPER_NOTICE = (
    "Paper trading only: no broker is connected and no real order is ever sent. Fills are simulated at the latest "
    "Yahoo price with a slippage and commission margin."
)


def _trim(a: Mapping[str, Any]) -> dict[str, Any]:
    """The analysis without the per-scenario model copies (the inputs that matter are in ``scIn``)."""
    out = dict(a)
    out["scenarios"] = {k: {"inputs": s["inputs"], "result": s["result"]} for k, s in a["scenarios"].items()}
    return out


def per_share(v2: Mapping[str, Any], shares: float | None, price: float | None) -> dict[str, Any]:
    """DCF equity value per share (equity ÷ filed shares) and its gap to the last close. Derived, not engine output."""
    dcf = v2.get("dcf") or {}
    eq = (dcf.get("primary") or {}).get("equity") if dcf.get("ok") else None
    if eq is None or not shares:
        return {"value": None, "label": "estimate", "method": "DCF equity ÷ shares outstanding",
                "warnings": ["DCF did not run or shares are unknown"]}  # fmt: skip
    ps = eq / shares
    out = {
        "value": ps,
        "unit": "USD",
        "label": "estimate",
        "method": "DCF equity value ÷ shares outstanding (filed)",
    }
    if price:
        out["vsPrice"] = {
            "value": ps / price - 1,
            "label": "derived",
            "method": "DCF value per share ÷ last close − 1",
        }
    return out


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, Mapping):
            return None
        d = d.get(p)
    return d


COMPARE_ROWS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Buyout entry EV", "USDm", ("lbo", "entry", "entryEV")),
    ("Buyout entry multiple", "x", ("lbo", "entry", "entryMultiple")),
    ("Exit multiple", "x", ("lboIn", "exitMultiple")),
    ("Base case IRR", "pct", ("scenarios", "base", "result", "returns", "irr")),
    ("Base case MOIC", "x", ("scenarios", "base", "result", "returns", "moic")),
    ("Downside MOIC", "x", ("scenarios", "downside", "result", "returns", "moic")),
    ("Beta used", "x", ("dcfIn", "beta")),
    ("Risk-free rate", "pct", ("dcfIn", "riskFree")),
    ("Size premium", "pct", ("dcfIn", "sizePremium")),
    ("WACC", "pct", ("wacc", "value")),
    ("DCF enterprise value", "USDm", ("dcf", "primary", "ev")),
    ("DCF equity value", "USDm", ("dcf", "primary", "equity")),
    ("Terminal value share of EV", "pct", ("dcf", "primary", "tvShareOfEV")),
    ("Screening score", "score", ("score", "total")),
    ("Decision", "text", ("rec", "decision")),
)


def compare(v1: Mapping[str, Any], v2: Mapping[str, Any]) -> list[dict[str, Any]]:
    """kci-v1 against kci-v2 on the same live inputs. The difference is v2 − v1 where both are numbers."""
    rows = []
    for title, unit, path in COMPARE_ROWS:
        a, b = _get(v1, *path), _get(v2, *path)
        if path[0] == "dcf" and not _get(v1, "dcf", "ok"):
            a = None
        if path[0] == "dcf" and not _get(v2, "dcf", "ok"):
            b = None
        num = isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool)
        rows.append({"title": title, "unit": unit, "v1": a, "v2": b, "change": (b - a) if num else None})
    return rows
