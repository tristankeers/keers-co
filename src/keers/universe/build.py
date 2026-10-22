"""Build the global company universe from live sources, and check listings against Yahoo.

Rules:
- A source batch (a "segment": US, or one non-US exchange) is written only when it was fetched in full. A batch
  that fails keeps its previous rows, which then age into "stale" warnings. Nothing is ever replaced by an
  empty or half-read list, unless the caller asked for a capped test build (``max_pages``), which the report
  says plainly.
- Every listing records its source, the date its source last listed it and the hash of the raw response.
- Currencies and Yahoo symbols that are worked out rather than given are marked as such until checked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

from ..data.http import Fetcher, FetchError, Response
from ..data.sec import ParseError
from ..data.store import Store
from . import sources as src
from .directory import COVERAGE_PRICES, COVERAGE_SEC, Listing, Universe
from .exchanges import (
    BY_CODE,
    NON_US,
    SEC_EXCHANGE_MIC,
    US,
    US_MIC_NAME,
    Exchange,
    class_dash_variant,
    local_ticker,
    yahoo_symbol,
)

# Not companies: funds, and instruments other than ordinary or depositary shares.
EXCLUDE_TYPES = {
    "ETP",
    "Open-End Fund",
    "Closed-End Fund",
    "Mutual Fund",
    "Unit",
    "Right",
    "Warrant",
    "Fund of Funds",
}
# Bloomberg placeholder lines for delisted or not-yet-traded shares (1091Z, 0924555D).
PLACEHOLDER_TICKER = re.compile(r"^\d{4,}[A-Z]$")
ETF_NAME = re.compile(r"\b(ETF|ETN|ETP)s?\b", re.I)
# Nasdaq Trader turns away scripted requests that do not look like a browser.
NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; keers-research/0.1)", "Accept": "text/plain,*/*"}
NOT_SHARES = re.compile(
    r"\b(warrants?|units?|rights?|preferred|preference|depositary shares? (each )?representing|notes? due|"
    r"debentures?|subordinated|% ?(senior|fixed))\b",
    re.I,
)


def _json(resp: Response, what: str) -> Any:
    try:
        return json.loads(resp.body)
    except json.JSONDecodeError as e:
        raise ParseError(f"{what}: not JSON ({e})") from e


class Builder:
    def __init__(
        self, fetcher: Fetcher, store: Store, figi_key: str | None = None, today: date | None = None
    ):
        self.fetcher = fetcher
        self.store = store
        self.universe = Universe(store)
        self.figi_headers = {"X-OPENFIGI-APIKEY": figi_key} if figi_key else {}
        if figi_key:  # a free key allows 20 filter requests a minute instead of 5
            self.fetcher.min_interval["api.openfigi.com"] = 3.1
        self.today = today or date.today()

    def _get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        resp = self.fetcher.get(url, headers)
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        return resp

    def _post(self, url: str, body: dict[str, Any]) -> Response:
        resp = self.fetcher.post_json(url, body, self.figi_headers)
        self.store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        return resp

    # OpenFIGI --------------------------------------------------------------------------------------------------
    def figi_rows(
        self, ex: Exchange, max_pages: int | None
    ) -> tuple[list[tuple[src.FigiRow, str]], dict[str, Any]]:
        """All rows for one exchange, each with the hash of the page it came from, and a fetch summary."""
        out: list[tuple[src.FigiRow, str]] = []
        info: dict[str, Any] = {"pages": 0, "total": 0, "truncated": False}
        for st in ex.security_types:
            start: str | None = None
            total: int | None = None
            got = 0
            while True:
                if max_pages is not None and info["pages"] >= max_pages:
                    info["truncated"] = True
                    break
                resp = self._post(src.OPENFIGI_FILTER_URL, src.figi_filter_body(ex.code, st, start))
                rows, start, total = src.parse_figi_page(_json(resp, f"openfigi {ex.code}"), ex.code)
                info["pages"] += 1
                got += len(rows)
                out.extend((r, resp.sha256) for r in rows)
                if not start or not rows:
                    break
            if total is not None:
                info["total"] += total
                if got < total and not info["truncated"]:
                    raise ParseError(
                        f"openfigi {ex.code} {st}: server reported {total} matches but paging stopped at {got}"
                    )
        return out, info

    def valid_exch_codes(self) -> set[str] | None:
        try:
            return src.parse_exch_codes(_json(self._get(src.OPENFIGI_EXCH_CODES_URL), "openfigi exchCode"))
        except (FetchError, ParseError):
            return None

    # segments --------------------------------------------------------------------------------------------------
    def us_listings(self, max_pages: int | None, figi: bool = True) -> tuple[list[Listing], dict[str, Any]]:
        info: dict[str, Any] = {"warnings": [], "excluded": {}}
        sec_resp = self._get(src.SEC_TICKERS_EXCHANGE_URL)
        sec_rows = src.parse_sec_tickers_exchange(_json(sec_resp, "company_tickers_exchange.json"))
        venues: dict[str, src.UsVenue] = {}
        try:
            for url, parse in ((src.NASDAQ_LISTED_URL, src.parse_nasdaq_listed),
                               (src.OTHER_LISTED_URL, src.parse_other_listed)):  # fmt: skip
                for venue in parse(self._get(url, NASDAQ_HEADERS).body.decode("utf-8", "replace")):
                    venues.setdefault(venue.ticker, venue)
        except (FetchError, ParseError) as e:
            venues = {}
            info["warnings"].append(f"Nasdaq Trader directory unavailable, so venues come from SEC's labels and ETFs are not filtered: {e}")  # fmt: skip
        figis: dict[str, src.FigiRow] = {}
        if figi:
            try:
                rows, finfo = self.figi_rows(US, max_pages)
                info["figiPages"] = finfo["pages"]
                for fr, _ in rows:
                    figis.setdefault(local_ticker(fr.ticker), fr)
            except (FetchError, ParseError) as e:
                info["warnings"].append(
                    f"OpenFIGI US unavailable, so US listings have no FIGIs this run: {e}"
                )

        def skip(why: str) -> None:
            info["excluded"][why] = info["excluded"].get(why, 0) + 1

        out: dict[str, Listing] = {}
        for r in sec_rows:
            v = venues.get(r.ticker)
            if v is not None and (v.etf or v.test_issue):
                skip("etf" if v.etf else "test issue")
                continue
            if v is None and ETF_NAME.search(r.name):
                skip("etf (by name)")
                continue
            if v is not None and NOT_SHARES.search(v.name):
                skip("not ordinary shares (warrant, unit, right, preferred, note)")
                continue
            mic = v.mic if v is not None else SEC_EXCHANGE_MIC.get(r.exchange.lower(), "")
            if not mic:
                skip("no exchange listing")
                continue
            f = figis.get(r.ticker)
            if f is not None and f.security_type in EXCLUDE_TYPES:
                skip(f"OpenFIGI type {f.security_type}")
                continue
            key = f"{mic}:{r.ticker}"
            if key in out:
                continue
            sources = ["sec"] + (["nasdaqtrader"] if v else []) + (["openfigi"] if f else [])
            out[key] = Listing(
                key=key, symbol=r.ticker.replace(".", "-"), local_ticker=r.ticker, name=r.name,
                exchange=US_MIC_NAME.get(mic, mic), mic=mic, country="US", currency="USD",
                currency_basis="exchange-rule", security_type=f.security_type if f else "unknown",
                coverage=COVERAGE_SEC, symbol_basis="derived", segment="sec-us", source="+".join(sources),
                as_of=self.today.isoformat(), raw_sha256=sec_resp.sha256, cik=r.cik,
                figi=f.figi if f else None, composite_figi=f.composite_figi if f else None,
                share_class_figi=(f.share_class_figi or None) if f else None,
                notes=("OTC: thinly traded, quotes may be stale",) if mic == "OTCM" else (),
            )  # fmt: skip
        info["rows"] = len(out)
        return list(out.values()), info

    def exchange_listings(self, ex: Exchange, max_pages: int | None) -> tuple[list[Listing], dict[str, Any]]:
        rows, info = self.figi_rows(ex, max_pages)
        info["excluded"] = {}
        out: dict[str, Listing] = {}
        basis = "derived" if ex.symbol_confidence == "high" else "derived-unsure"
        for r, sha in rows:
            why = None
            code, _, label = r.exch_code.partition(" ")
            label = label.strip().strip("()")
            venue = next((v for v in ex.venues if v[0] == label), None)
            lex = replace(ex, mic=venue[1], yahoo_suffix=venue[2], name=venue[3]) if venue else ex
            t = local_ticker(r.ticker)
            if code and code != ex.code:
                why = f"answer for another exchange ({r.exch_code})"
            elif r.security_type in EXCLUDE_TYPES:
                why = f"OpenFIGI type {r.security_type}"
            elif not t:
                why = "no ticker"
            elif PLACEHOLDER_TICKER.match(t):
                why = "placeholder ticker (delisted or not yet traded)"
            elif any(t.endswith(sfx) for sfx in ex.drop_suffixes):
                why = "not an ordinary line (foreign board or NVDR)"
            if why:
                info["excluded"][why] = info["excluded"].get(why, 0) + 1
                continue
            key = f"{lex.mic}:{t}"
            if key in out:
                info["excluded"]["duplicate ticker"] = info["excluded"].get("duplicate ticker", 0) + 1
                continue
            notes = []
            if ex.symbol_confidence == "low":
                notes.append(
                    f"Yahoo codes on {ex.name} differ from the exchange ticker; the symbol is a guess"
                )
            out[key] = Listing(
                key=key, symbol=yahoo_symbol(lex, r.ticker), local_ticker=t, name=r.name, exchange=lex.name,
                mic=lex.mic, country=ex.country, currency=ex.currency, currency_basis="exchange-default",
                security_type=r.security_type or r.security_type2, coverage=COVERAGE_PRICES, symbol_basis=basis,
                segment=f"openfigi:{ex.code}", source="openfigi", as_of=self.today.isoformat(), raw_sha256=sha,
                figi=r.figi, composite_figi=r.composite_figi or None, share_class_figi=r.share_class_figi or None,
                notes=tuple(notes),
            )  # fmt: skip
        info["rows"] = len(out)
        return list(out.values()), info

    def build(self, codes: Sequence[str] | None = None, max_pages: int | None = None, us: bool = True,
              progress: Callable[[str], None] | None = None) -> dict[str, Any]:  # fmt: skip
        """Fetch and write each segment. ``codes`` limits the non-US exchanges (Bloomberg codes, e.g. ["LN"])."""
        say = progress or (lambda _m: None)
        run = self.store.start_run("universe-build")
        exchanges = (
            list(NON_US) if codes is None else [BY_CODE[c.upper()] for c in codes if c.upper() != "US"]
        )
        report: dict[str, Any] = {"segments": {}, "capped": max_pages is not None}
        valid = self.valid_exch_codes() if exchanges else None
        if exchanges and valid is None:
            report["warnings"] = [
                "could not fetch OpenFIGI's list of exchange codes, so codes were not pre-checked"
            ]
        jobs: list[tuple[str, Callable[[], tuple[list[Listing], dict[str, Any]]]]] = []
        if us:
            jobs.append(("sec-us", lambda: self.us_listings(max_pages)))
        for ex in exchanges:
            if valid is not None and ex.code not in valid:
                report["segments"][f"openfigi:{ex.code}"] = {"status": "failed", "error": f"OpenFIGI does not know exchange code {ex.code}"}  # fmt: skip
                continue
            jobs.append((f"openfigi:{ex.code}", lambda ex=ex: self.exchange_listings(ex, max_pages)))  # type: ignore[misc]
        for seg, job in jobs:
            say(f"{seg} ...")
            try:
                listings, info = job()
            except (FetchError, ParseError) as e:
                report["segments"][seg] = {"status": "failed", "error": str(e), "kept": self._count(seg)}
                say(f"{seg} failed, kept the previous {self._count(seg)} rows: {e}")
                continue
            if not listings:
                report["segments"][seg] = {"status": "failed", "error": "source returned no listings", **info,
                                           "kept": self._count(seg)}  # fmt: skip
                continue
            self.universe.replace_segment(seg, listings)
            status = "capped" if info.get("truncated") else "ok"
            report["segments"][seg] = {"status": status, **info}
            say(f"{seg} {status}: {len(listings)} listings")
        report["primaries"] = self.universe.mark_primaries()
        report["stats"] = {k: v for k, v in self.universe.stats(self.today).items() if k != "byExchange"}
        statuses = [s["status"] for s in report["segments"].values()]
        report["status"] = "ok" if all(s == "ok" for s in statuses) else (
            "failed" if all(s == "failed" for s in statuses) else "partial")  # fmt: skip
        self.store.finish_run(run, report["status"], report)
        return report

    def _count(self, segment: str) -> int:
        return int(
            self.store.db.execute("SELECT COUNT(*) FROM listings WHERE segment=?", (segment,)).fetchone()[0]
        )


# Yahoo checks -------------------------------------------------------------------------------------------------
CHECK_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; keers-research/0.1)", "Accept": "application/json"}
_STOP = {"THE", "INC", "CORP", "CORPORATION", "LTD", "LIMITED", "PLC", "CO", "COMPANY", "GROUP", "HOLDINGS",
         "HOLDING", "SA", "AG", "NV", "SE", "SPA", "AB", "ASA", "OYJ", "BHD", "TBK", "PT", "PCL", "AND", "DE"}  # fmt: skip


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[A-Z0-9]+", name.upper()) if len(w) >= 3 and w not in _STOP}


def judge(x: Listing, meta: dict[str, Any]) -> tuple[str, str]:
    """(status, detail) for a Yahoo chart ``meta`` block against a listing."""
    itype = str(meta.get("instrumentType") or "")
    if itype and itype not in ("EQUITY",):
        return "mismatch", f"Yahoo lists {meta.get('symbol')} as {itype}, not a share"
    yname = str(meta.get("longName") or meta.get("shortName") or "")
    if yname and _words(x.name) and not (_words(x.name) & _words(yname)):
        return (
            "mismatch",
            f"Yahoo's name {yname!r} shares no word with {x.name!r}: probably a different company",
        )
    if not meta.get("currency"):
        return "mismatch", "Yahoo gave no currency"
    return "ok", ""


def check_listing(fetcher: Fetcher, store: Store, x: Listing) -> tuple[str, str]:
    """Look a listing up on Yahoo and record the result. Tries the dashed share-class spelling on Nordic tickers."""
    uni = Universe(store)
    ex = BY_CODE.get(x.segment.split(":", 1)[1]) if x.segment.startswith("openfigi:") else None
    candidates = [x.symbol] + ([v] if ex and (v := class_dash_variant(ex, x.local_ticker)) else [])
    last = ""
    for sym in candidates:
        url = CHECK_URL.format(symbol=quote(sym), query=urlencode({"range": "5d", "interval": "1d"}))
        try:
            resp = fetcher.get(url, YAHOO_HEADERS)
        except FetchError as e:
            last = str(e)
            if "HTTP 404" in last:
                continue
            raise
        store.put_raw(resp.url, resp.body, resp.sha256, resp.retrieved_at, resp.content_type)
        try:
            doc = json.loads(resp.body)
            meta = doc["chart"]["result"][0]["meta"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            last = "no chart data"
            continue
        status, detail = judge(x, meta)
        uni.put_check(x.key, status, str(meta.get("symbol") or sym).upper(), resp.sha256, meta, detail)
        return status, detail
    uni.put_check(
        x.key, "not-found", x.symbol, "", {}, f"Yahoo has no chart for {' or '.join(candidates)} ({last})"
    )
    return "not-found", last


def check(fetcher: Fetcher, store: Store, listings: Sequence[Listing]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": 0, "mismatch": [], "not-found": [], "errors": []}
    for x in listings:
        try:
            status, detail = check_listing(fetcher, store, x)
        except FetchError as e:
            out["errors"].append({"key": x.key, "error": str(e)})
            continue
        if status == "ok":
            out["ok"] += 1
        else:
            out[status].append({"key": x.key, "symbol": x.symbol, "name": x.name, "detail": detail})
    return out
