"""Global company universe: parsers, build rules and Yahoo checks. Synthetic documents only; no network.

Every company here is FICTIONAL ("Example Widgets", "Northwind", ...). The tickers and identifiers are made up.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, timedelta
from email.message import Message
from typing import Any

import pytest

from keers.data.http import Fetcher
from keers.data.sec import ParseError
from keers.data.store import Store
from keers.universe import Universe
from keers.universe import build as ub
from keers.universe import sources as src
from keers.universe.exchanges import BY_CODE, class_dash_variant, local_ticker, yahoo_symbol

TODAY = date(2026, 10, 22)

SEC_DOC = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [9999999, "Example Widgets Inc", "EXW", "Nasdaq"],
        [9999998, "Example Holdings Corp", "EXH-B", "NYSE"],
        [9999997, "Example Index Trust", "EXIT", "NYSE"],  # an ETF in Nasdaq Trader
        [9999996, "Example Spac Corp", "EXSPW", "Nasdaq"],  # a warrant
        [9999995, "Example Unlisted Co", "EXUN", None],
        [9999994, "Example Pink Sheets Inc", "EXPK", "OTC"],
    ],
}
NASDAQ_LISTED = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    "EXW|Example Widgets Inc - Common Stock|Q|N|N|100|N|N\n"
    "EXSPW|Example Spac Corp - Warrants|G|N|N|100|N|N\n"
    "ZZZT|Test Issue|Q|Y|N|100|N|N\n"
    "File Creation Time: 1022202621:00|||||||\n"
)
OTHER_LISTED = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
    "EXH.B|Example Holdings Corp Class B|N|EXH.B|N|100|N|EXH.B\n"
    "EXIT|Example Index Trust|P|EXIT|Y|100|N|EXIT\n"
    "File Creation Time: 1022202621:00|||||||\n"
)


def figi(ticker: str, exch: str, name: str, share_class: str, stype: str = "Common Stock") -> dict[str, Any]:
    return {"figi": f"BBG{exch}{ticker}"[:12].ljust(12, "0"), "compositeFIGI": f"BBGC{ticker}"[:12],
            "shareClassFIGI": share_class, "ticker": ticker, "name": name, "exchCode": exch,
            "securityType": stype, "securityType2": "Common Stock", "marketSector": "Equity"}  # fmt: skip


FIGI_PAGES: dict[tuple[str, str, str | None], dict[str, Any]] = {
    ("US", "Common Stock", None): {
        "data": [
            figi("EXW", "US", "EXAMPLE WIDGETS INC", "BBGSC0000EXW"),
            figi("EXH/B", "US", "EXAMPLE HOLDINGS-B", "BBGSC0000EXH"),
        ],
        "total": 2,
    },
    ("US", "Depositary Receipt", None): {"data": [], "total": 0},
    # London, over two pages
    ("LN", "Common Stock", None): {
        "data": [
            figi("NWD", "LN", "NORTHWIND PLC", "BBGSC0000NWD"),
            figi("BT/A", "LN", "BRITANNIA TEL-A", "BBGSC000BTA0"),
        ],
        "next": "page2",
        "total": 3,
    },
    ("LN", "Common Stock", "page2"): {
        "data": [figi("RR/", "LN", "ROYAL ROWING PLC", "BBGSC0000RR0")],
        "total": 3,
    },
    # Xetra lists the US company too (same share class): that row is secondary
    ("GY", "Common Stock", None): {
        "data": [
            figi("EXW1", "GY", "EXAMPLE WIDGETS INC", "BBGSC0000EXW"),
            figi("GMB", "GY", "GRUNDWERK AG", "BBGSC0000GMB"),
            figi("FND", "GY", "SOME FUND", "BBGSC0000FND", "ETP"),
        ],
        "total": 3,
    },
    ("HK", "Common Stock", None): {
        "data": [figi("700", "HK", "HARBOUR TECH HOLDINGS", "BBGSC0000700")],
        "total": 1,
    },
    ("SS", "Common Stock", None): {
        "data": [figi("NORDB", "SS", "NORDIC TOOLS AB-B", "BBGSC00NORDB")],
        "total": 1,
    },
}


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", Message(), None)


class Net:
    """A fake network. ``fail`` holds exchange codes (or 'sec', 'nasdaq') whose requests error."""

    def __init__(self, pages: dict[tuple[str, str, str | None], dict[str, Any]] | None = None,
                 fail: set[str] | None = None, yahoo: dict[str, dict[str, Any]] | None = None) -> None:  # fmt: skip
        self.pages = FIGI_PAGES if pages is None else pages
        self.fail = fail or set()
        self.yahoo = yahoo or {}
        self.posts: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def opener(self, req: urllib.request.Request, timeout: float) -> bytes:
        url = req.full_url
        self.headers.append(dict(req.header_items()))
        if url == src.SEC_TICKERS_EXCHANGE_URL:
            if "sec" in self.fail:
                raise http_error(500)
            return json.dumps(SEC_DOC).encode()
        if url == src.NASDAQ_LISTED_URL:
            if "nasdaq" in self.fail:
                raise http_error(403)
            return NASDAQ_LISTED.encode()
        if url == src.OTHER_LISTED_URL:
            return OTHER_LISTED.encode()
        if url == src.OPENFIGI_EXCH_CODES_URL:
            return json.dumps({"values": sorted(BY_CODE)}).encode()
        if url == src.OPENFIGI_FILTER_URL:
            assert req.data is not None
            body = json.loads(req.data)
            self.posts.append(body)
            if body["exchCode"] in self.fail:
                raise http_error(500)
            page = self.pages.get((body["exchCode"], body["securityType2"], body.get("start")))
            return json.dumps(page if page is not None else {"data": [], "total": 0}).encode()
        if "finance.yahoo.com" in url:
            sym = urllib.request.unquote(url.split("/chart/")[1].split("?")[0])
            if sym not in self.yahoo:
                raise http_error(404)
            return json.dumps({"chart": {"result": [{"meta": self.yahoo[sym]}], "error": None}}).encode()
        raise AssertionError(f"unexpected URL {url}")

    def fetcher(self) -> Fetcher:
        return Fetcher("Test agent test@example.com", retries=0, opener=self.opener, sleep=lambda s: None)


def built(codes: list[str] | None = None, net: Net | None = None, store: Store | None = None,
          today: date = TODAY) -> tuple[Store, dict[str, Any]]:  # fmt: skip
    store = store or Store(":memory:")
    net = net or Net()
    report = ub.Builder(net.fetcher(), store, today=today).build(
        ["LN", "GY", "HK", "SS"] if codes is None else codes
    )
    return store, report


# parsers ------------------------------------------------------------------------------------------------------
def test_ticker_spellings() -> None:
    assert local_ticker("BRK/B") == local_ticker("BRK-B") == local_ticker("brk.b") == "BRK.B"
    assert local_ticker("RR/") == "RR" and local_ticker("WALMEX*") == "WALMEX"
    assert yahoo_symbol(BY_CODE["LN"], "BT/A") == "BT-A.L"
    assert yahoo_symbol(BY_CODE["HK"], "700") == "0700.HK"
    assert yahoo_symbol(BY_CODE["JT"], "7203") == "7203.T"
    assert class_dash_variant(BY_CODE["SS"], "VOLVB") == "VOLV-B.ST"
    assert class_dash_variant(BY_CODE["LN"], "VOLVB") is None


def test_sec_and_nasdaq_parsers_refuse_unexpected_documents() -> None:
    rows = src.parse_sec_tickers_exchange(SEC_DOC)
    assert rows[1].ticker == "EXH.B" and rows[4].exchange == ""
    with pytest.raises(ParseError):
        src.parse_sec_tickers_exchange({"data": []})
    with pytest.raises(ParseError, match="footer"):
        src.parse_nasdaq_listed(NASDAQ_LISTED.rsplit("File", 1)[0])  # cut short
    # An exchange letter this code does not know keeps the row (flags still apply) with no venue.
    odd = src.parse_other_listed(OTHER_LISTED.replace("|N|EXH.B|", "|F|EXH.B|"))
    assert odd[0].ticker == "EXH.B" and odd[0].mic == "" and odd[1].etf


def test_openfigi_error_is_raised_not_read_as_empty() -> None:
    with pytest.raises(ParseError, match="Too many"):
        src.parse_figi_page({"error": "Too many requests"}, "LN")


# build --------------------------------------------------------------------------------------------------------
def test_build_writes_listings_with_provenance() -> None:
    store, report = built()
    uni = Universe(store)
    assert report["status"] == "ok", report
    seg = report["segments"]
    assert seg["sec-us"]["excluded"] == {"etf": 1, "not ordinary shares (warrant, unit, right, preferred, note)": 1,
                                         "no exchange listing": 1}  # fmt: skip
    assert seg["openfigi:LN"]["pages"] == 2 and seg["openfigi:LN"]["rows"] == 3
    assert seg["openfigi:GY"]["excluded"] == {"OpenFIGI type ETP": 1}

    exw = uni.get("XNAS:EXW", TODAY)
    assert exw is not None and exw.coverage == "sec" and exw.cik == 9999999 and exw.figi
    assert exw.currency == "USD" and exw.currency_basis == "exchange-rule" and exw.warnings == ()
    assert exw.source == "sec+nasdaqtrader+openfigi" and exw.raw_sha256 and exw.as_of == "2026-10-22"
    brk = uni.get("EXH-B", TODAY)  # by Yahoo symbol
    assert brk is not None and brk.key == "XNYS:EXH.B" and brk.exchange == "NYSE"
    pink = uni.get("OTCM:EXPK", TODAY)
    assert pink is not None and pink.notes  # OTC is flagged

    nwd = uni.get("NWD.L", TODAY)
    assert nwd is not None and nwd.key == "XLON:NWD" and nwd.coverage == "prices-only"
    assert nwd.currency == "GBX" and nwd.currency_basis == "exchange-default"
    assert any("not confirmed" in w for w in nwd.warnings) and any("unconfirmed" in w for w in nwd.warnings)
    assert uni.get("BT-A.L", TODAY) is not None and uni.get("RR.L", TODAY) is not None
    hk = uni.get("XHKG:700", TODAY)
    assert hk is not None and hk.symbol == "0700.HK" and not any("unconfirmed" in w for w in hk.warnings)


def test_cross_listing_on_a_hub_exchange_points_at_the_home_listing() -> None:
    store, _ = built()
    uni = Universe(store)
    sec = uni.get("XETR:EXW1", TODAY)
    assert sec is not None and not sec.is_primary and sec.primary_key == "XNAS:EXW"
    assert [x.key for x in uni.search("example widgets", today=TODAY)] == ["XNAS:EXW"]
    assert {x.key for x in uni.search("example widgets", primary_only=False, today=TODAY)} == {
        "XNAS:EXW",
        "XETR:EXW1",
    }
    own = uni.get("XETR:GMB", TODAY)  # a German company listed only on Xetra stays primary
    assert own is not None and own.is_primary


def test_search_ranks_exact_ticker_first_and_filters_by_country() -> None:
    store, _ = built()
    uni = Universe(store)
    assert uni.search("nwd")[0].key == "XLON:NWD"
    assert [x.country for x in uni.search("plc", country="gb")] == ["GB", "GB"]
    assert uni.search("") == []
    assert uni.search("100%_") == []  # LIKE wildcards are matched literally


def test_failed_exchange_keeps_its_previous_rows_and_they_go_stale() -> None:
    store, _ = built()
    later = TODAY + timedelta(days=20)
    _, report = built(net=Net(fail={"LN"}), store=store, today=later)
    assert report["status"] == "partial"
    assert report["segments"]["openfigi:LN"]["status"] == "failed"
    assert report["segments"]["openfigi:LN"]["kept"] == 3
    nwd = Universe(store).get("XLON:NWD", later)
    assert nwd is not None and any(w.startswith("stale") for w in nwd.warnings)
    hk = Universe(store).get("XHKG:700", later)
    assert hk is not None and not any(w.startswith("stale") for w in hk.warnings)  # rebuilt this run


def test_paging_that_stops_short_of_the_reported_total_fails_the_exchange() -> None:
    pages = dict(FIGI_PAGES)
    pages[("LN", "Common Stock", "page2")] = {"data": [], "total": 3}
    _, report = built(["LN"], Net(pages=pages))
    assert report["segments"]["openfigi:LN"]["status"] == "failed"
    assert "paging stopped" in report["segments"]["openfigi:LN"]["error"]


def test_capped_test_build_says_so() -> None:
    store = Store(":memory:")
    report = ub.Builder(Net().fetcher(), store, today=TODAY).build(["LN"], max_pages=1, us=False)
    assert report["capped"] and report["segments"]["openfigi:LN"]["status"] == "capped"
    assert report["segments"]["openfigi:LN"]["rows"] == 2


def test_us_build_survives_a_missing_nasdaq_directory_but_says_so() -> None:
    _, report = built([], Net(fail={"nasdaq"}))
    us = report["segments"]["sec-us"]
    assert us["status"] == "ok" and "Nasdaq Trader directory unavailable" in us["warnings"][0]


def test_whole_us_segment_fails_without_sec() -> None:
    _, report = built([], Net(fail={"sec"}))
    assert report["segments"]["sec-us"]["status"] == "failed" and report["status"] == "failed"


def test_openfigi_key_is_sent_but_not_stored() -> None:
    net, store = Net(), Store(":memory:")
    ub.Builder(net.fetcher(), store, figi_key="secret-key", today=TODAY).build(["HK"], us=False)
    assert any(h.get("X-openfigi-apikey") == "secret-key" for h in net.headers)
    urls = [r[0] for r in store.db.execute("SELECT url FROM raw_documents")]
    assert any('"exchCode":"HK"' in u for u in urls) and not any("secret-key" in u for u in urls)


# Yahoo checks -------------------------------------------------------------------------------------------------
def meta(symbol: str, name: str, currency: str = "GBp", itype: str = "EQUITY") -> dict[str, Any]:
    return {"symbol": symbol, "longName": name, "currency": currency, "instrumentType": itype,
            "exchangeName": "LSE"}  # fmt: skip


def test_yahoo_check_confirms_currency_and_symbol() -> None:
    net = Net(yahoo={"NWD.L": meta("NWD.L", "Northwind plc"),
                     "BT-A.L": meta("BT-A.L", "Britannia Telecom plc", "USD"),
                     "RR.L": meta("RR.L", "Some Other Business Ltd"),
                     "NORD-B.ST": meta("NORD-B.ST", "Nordic Tools AB (publ)", "SEK")})  # fmt: skip
    store, _ = built(net=net)
    uni = Universe(store)
    keys = ["XLON:NWD", "XLON:BT.A", "XLON:RR", "XSTO:NORDB", "XHKG:700"]
    res = ub.check(net.fetcher(), store, [x for k in keys if (x := uni.get(k)) is not None])
    assert res["ok"] == 3
    assert [m["key"] for m in res["mismatch"]] == ["XLON:RR"]
    assert [m["key"] for m in res["not-found"]] == ["XHKG:700"]

    nwd = uni.get("XLON:NWD", TODAY)
    assert nwd is not None and nwd.currency == "GBX" and nwd.currency_basis == "yahoo"
    assert nwd.symbol_basis == "yahoo-verified" and nwd.warnings == ()
    bta = uni.get("XLON:BT.A", TODAY)
    assert bta is not None and bta.currency == "USD" and "corrected" in bta.warnings[0]
    nord = uni.get("NORD-B.ST", TODAY)  # found under the dashed share-class spelling
    assert nord is not None and nord.key == "XSTO:NORDB" and nord.symbol == "NORD-B.ST"
    rr = uni.get("XLON:RR", TODAY)
    assert rr is not None and any("different company" in w for w in rr.warnings)
    assert Universe(store).stats(TODAY)["byExchange"]["XLON"]["yahooProblems"] == 1


def test_yahoo_check_rejects_funds() -> None:
    store, _ = built(["LN"])
    x = Universe(store).get("XLON:NWD", TODAY)
    assert x is not None
    assert ub.judge(x, meta("NWD.L", "Northwind", itype="ETF"))[0] == "mismatch"


def test_post_json_records_the_request_in_the_url() -> None:
    seen: list[bytes | None] = []

    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        seen.append(req.data)  # type: ignore[arg-type]
        return b"{}"

    r = Fetcher("t", opener=opener, sleep=lambda s: None).post_json("https://api.example/x", {"b": 1, "a": 2})
    assert r.url == 'https://api.example/x#{"a":2,"b":1}' and seen == [b'{"a":2,"b":1}']


def test_yahoo_symbols_with_exchange_suffixes_are_left_alone() -> None:
    from keers.data import yahoo
    from keers.universe.exchanges import NON_US

    assert yahoo.symbol("brk.b") == "BRK-B"
    assert (
        yahoo.symbol("vod.l") == "VOD.L"
        and yahoo.symbol("BT-A.L") == "BT-A.L"
        and yahoo.symbol("7203.T") == "7203.T"
    )
    assert {e.yahoo_suffix for e in NON_US} | {
        v[2] for e in NON_US for v in e.venues
    } <= yahoo.EXCHANGE_SUFFIXES


def test_openfigi_venue_labels_placeholders_and_odd_lines() -> None:
    """OpenFIGI writes Taiwan's code as "TT (Taiwan Stock Exchange)" and mixes in TPEx listings (live probe)."""

    def row(t: str, exch: str, name: str) -> dict[str, Any]:
        return {**figi(t, "TT", name, f"BBGSC{t}"), "exchCode": exch}

    pages = {
        ("TT", "Common Stock", None): {"data": [row("2330", "TT (Taiwan Stock Exchange)", "HARBOUR SEMI"),
                                                row("6488", "TT (Taipei Stock Exchange)", "ISLAND WAFER"),
                                                row("1091Z", "TT (Taiwan Stock Exchange)", "OLD LINE")], "total": 3},
        ("TB", "Common Stock", None): {"data": [figi("AFC", "TB", "SIAM FIBRE", "BBGSC0000AFC"),
                                                figi("AFC-F", "TB", "SIAM FIBRE-FORGN", "BBGSC000AFCF")], "total": 2},
    }  # fmt: skip
    store, report = built(["TT", "TB"], Net(pages=pages))
    uni = Universe(store)
    tw = uni.get("XTAI:2330", TODAY)
    two = uni.get("6488.TWO", TODAY)
    assert tw is not None and tw.symbol == "2330.TW"
    assert two is not None and two.key == "ROCO:6488" and two.exchange == "Taipei Exchange (TPEx)"
    assert report["segments"]["openfigi:TT"]["excluded"] == {
        "placeholder ticker (delisted or not yet traded)": 1
    }
    assert report["segments"]["openfigi:TB"]["excluded"] == {
        "not an ordinary line (foreign board or NVDR)": 1
    }


def test_us_etfs_are_dropped_by_name_when_the_nasdaq_directory_is_down() -> None:
    doc = {**SEC_DOC, "data": [*SEC_DOC["data"], [9999990, "Example Physical Gold ETF", "EXGLD", "CBOE"]]}
    net = Net(fail={"nasdaq"})
    orig = net.opener

    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        return (
            json.dumps(doc).encode() if req.full_url == src.SEC_TICKERS_EXCHANGE_URL else orig(req, timeout)
        )

    net.opener = opener  # type: ignore[method-assign]
    _, report = built([], net)
    assert report["segments"]["sec-us"]["excluded"]["etf (by name)"] == 1
