"""Non-US financials: ESEF reports, IFRS line items, currencies, reporting cadence and listing links.

Every company, identifier and figure here is fictional (tests/data/foreign_fixtures.py).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from typing import Any

import pytest

from keers.data import esef, fx, snapshot
from keers.data.beta import _in_usd
from keers.data.filers import Filers, name_matches, normalise, parse_lei_records
from keers.data.fundamentals import Fundamentals
from keers.data.http import Fetcher
from keers.data.models import Fact, PricePoint, RatePoint
from keers.data.store import ENTITY_ID_BASE, Store
from keers.strategies import distress

from . import fixtures as us
from . import foreign_fixtures as ff

AS_OF = date(2026, 10, 23)  # a Friday
PX_DAY = "2026-10-22"


def esef_filing(year: int, added: str) -> esef.EsefFiling:
    return esef.parse_filings(ff.filings_list([ff.filing(year, added)]), ff.LEI)[0]


def load(store: Store | None = None, years: tuple[int, ...] = (2024, 2025), shares_m: float = 200.0) -> Store:
    """The fictional company's ESEF reports, a London price in pence and the FX rates, as a refresh stores them."""
    store = store or Store(":memory:")
    cik = store.entity_id("lei", ff.LEI, ff.LEGAL_NAME, "GB", "test")
    store.put_company(cik, ff.LEI, ff.LEGAL_NAME, None, None)
    store.put_link(ff.SYMBOL, cik, "manual", f"LEI {ff.LEI}")
    for y in years:
        f = esef_filing(y, f"{y + 1}-03-20")
        facts, rep = esef.parse_xbrl_json(ff.report(y, shares_m), ff.LEI, cik, f, f"sha-{y}")
        assert not rep.contradictions
        store.put_facts(facts)
        store.put_filings([f.as_filing(cik)])
    store.put_prices([PricePoint(ff.SYMBOL, date.fromisoformat(PX_DAY), 450.0, "yahoo", raw_sha256="px")])
    store.put_price_currency(ff.SYMBOL, "yahoo", "GBp", "px")
    store.put_rates([
        RatePoint("DEXUSUK", date.fromisoformat(PX_DAY), 1.30, "fred", "uk"),
        RatePoint("DEXUSEU", date.fromisoformat(PX_DAY), 1.10, "fred", "eu"),
        RatePoint("DGS10", date.fromisoformat(PX_DAY), 4.0, "fred", "t"),
    ])  # fmt: skip
    return store


# exchange rates ---------------------------------------------------------------------------------------------------
def rates_store() -> Store:
    st = Store(":memory:")
    d = date(2026, 10, 22)
    st.put_rates([
        RatePoint("DEXUSUK", d, 1.30, "fred", "a"), RatePoint("DEXUSEU", d, 1.10, "fred", "b"),
        RatePoint("DEXJPUS", d, 150.0, "fred", "c"), RatePoint("ECB:PLN", d, 4.40, "ecb", "d"),
        RatePoint("ECB:USD", d, 1.10, "ecb", "e"),
    ])  # fmt: skip
    return st


def test_fx_rates_from_fred_and_ecb() -> None:
    st, d = rates_store(), date(2026, 10, 23)
    assert fx.rate(st, "GBP", "USD", d).value == pytest.approx(1.30)
    assert fx.rate(st, "USD", "JPY", d).value == pytest.approx(150.0)  # FRED quotes yen per dollar
    assert fx.rate(st, "GBP", "EUR", d).value == pytest.approx(1.30 / 1.10)
    assert fx.rate(st, "GBX", "EUR", d).value == pytest.approx(1.30 / 1.10 / 100)  # pence
    assert fx.rate(st, "GBp", "GBP", d).value == pytest.approx(0.01)
    # Zloty: not on FRED; crossed through the euro on the ECB's own fixing.
    pln = fx.rate(st, "PLN", "USD", d)
    assert pln.value == pytest.approx(1.10 / 4.40)
    assert {s["series"] for s in pln.sources} == {"ECB:PLN", "ECB:USD"}


def test_fx_is_missing_when_stale_or_unsupported() -> None:
    st = rates_store()
    stale = fx.rate(st, "GBP", "USD", date(2026, 11, 20))
    assert not stale.known and "no rate between" in stale.method
    sar = fx.rate(st, "SAR", "USD", date(2026, 10, 23))
    assert not sar.known and "no free official exchange-rate source for SAR" in sar.method
    assert not fx.supported("CLP") and fx.supported("GBX") and fx.supported("PLN")


def test_fx_series_needed() -> None:
    assert fx.series_for(["GBX", "EUR", "USD"]) == (["DEXUSEU", "DEXUSUK"], [])
    assert fx.series_for(["PLN"]) == ([], ["PLN", "USD"])


def test_ecb_csv_parser() -> None:
    pts = fx.parse_ecb_csv(ff.ecb_csv("PLN", [("2026-10-21", 4.41), ("2026-10-22", 4.40)]), "PLN", "h")
    assert [(p.series, p.day.isoformat(), p.value) for p in pts] == [
        ("ECB:PLN", "2026-10-21", 4.41), ("ECB:PLN", "2026-10-22", 4.40)]  # fmt: skip
    with pytest.raises(Exception, match="not an SDMX CSV"):
        fx.parse_ecb_csv("<html>busy</html>", "PLN", "h")


# ESEF -------------------------------------------------------------------------------------------------------------
def test_xbrl_json_parser_rules() -> None:
    f = esef_filing(2025, "2026-03-20")
    facts, rep = esef.parse_xbrl_json(ff.report(2025), ff.LEI, ENTITY_ID_BASE, f, "sha")
    by = {(x.concept, x.start, x.end): x for x in facts}
    rev = by[
        ("Revenue", date(2025, 1, 1), date(2025, 12, 31))
    ]  # midnight 1 Jan 2026 is the end of 31 Dec 2025
    assert (
        rev.value == 1100 * ff.M
        and rev.unit == "EUR"
        and rev.form == "ESEF"
        and rev.filed == date(2026, 3, 20)
    )
    assert rev.accn == f"{ff.LEI}-2025-12-31-ESEF-GB-0"
    assert by[("Revenue", date(2024, 1, 1), date(2024, 12, 31))].value == 1000 * ff.M  # comparative
    cash = by[("CashAndCashEquivalents", None, date(2025, 12, 31))]
    assert cash.value == 120 * ff.M
    assert by[("NumberOfSharesOutstanding", None, date(2025, 12, 31))].unit == "shares"
    # Segment line, subsidiary, text and extension facts are all left out; the duplicate is kept once.
    assert sum(1 for x in facts if x.concept == "Revenue" and x.end == date(2025, 12, 31)) == 1
    assert rep.skipped["has dimensions"] == 1 and rep.skipped["other entity"] == 1
    assert rep.skipped["not an ifrs-full concept"] == 1 and rep.skipped["no usable unit"] == 1


def test_contradictory_facts_are_dropped() -> None:
    doc = ff.report(2025)
    doc["facts"]["clash"] = ff._fact("Revenue", ff.duration(2025), 1234 * ff.M, decimals=-6)
    facts, rep = esef.parse_xbrl_json(doc, ff.LEI, ENTITY_ID_BASE, esef_filing(2025, "2026-03-20"), "sha")
    assert not [x for x in facts if x.concept == "Revenue" and x.end == date(2025, 12, 31)]
    assert rep.contradictions and "ifrs-full:Revenue" in rep.contradictions[0]


def test_filings_list_keeps_only_the_lei_asked_for() -> None:
    other = ff.filing(2025, "2026-03-21")
    other["attributes"]["fxo_id"] = "KEERSTEST0SOMEONE001-2025-12-31-ESEF-FR-0"
    other["attributes"]["json_url"] = "/KEERSTEST0SOMEONE001/2025-12-31/ESEF/FR/0/x.json"
    other["relationships"]["entity"]["data"]["id"] = "99"
    doc = ff.filings_list([ff.filing(2024, "2025-03-20"), ff.filing(2025, "2026-03-20"), other])
    found = esef.parse_filings(doc, ff.LEI)
    assert [f.period_end.year for f in found] == [2024, 2025]
    assert found[0].json_url.startswith("https://filings.xbrl.org/") and found[0].country == "GB"
    # A refiled report for the same year replaces the earlier one.
    again = esef.parse_filings(ff.filings_list([ff.filing(2025, "2026-03-20"), ff.filing(2025, "2026-04-02", 1)]),
                               ff.LEI)  # fmt: skip
    assert [f.fxo_id for f in esef.latest_per_period(again, 3)] == [f"{ff.LEI}-2025-12-31-ESEF-GB-1"]


# snapshot, valuation and distress on IFRS accounts in euros, priced in pence --------------------------------------
def test_ifrs_snapshot_in_reporting_currency() -> None:
    snap = snapshot.build(load(), ff.SYMBOL, AS_OF)
    v = snap.values
    assert not snap.blocked, snap.blocked_by
    assert (snap.currency, snap.price_currency, snap.cadence, snap.source) == ("EUR", "GBp", "annual", "esef")
    assert v["revenue"].value == pytest.approx(1100) and v["revenue"].unit == "EURm"
    assert v["revenue"].method.startswith("ifrs-full:Revenue: fiscal year as reported")
    assert v["revenueGrowth"].value == pytest.approx(0.10)
    assert v["ebitda"].value == pytest.approx(180)
    assert v["netIncome"].value == pytest.approx(80)
    assert v["interestExpense"].value == pytest.approx(24) and "FinanceCosts" in v["interestExpense"].method
    # IFRS 16: lease liabilities are in debt, and there is no separate operating-lease line.
    assert v["debt"].value == pytest.approx(280 + 60 + 90) and "lease" in v["debt"].method
    assert "operatingLeases" not in v
    assert v["netDebt"].value == pytest.approx(310)
    # 450 pence × (1.30 / 1.10) euros per pound × 200m shares
    assert v["marketCap"].value == pytest.approx(4.50 * 1.30 / 1.10 * 200)
    assert v["ev"].value == pytest.approx(4.50 * 1.30 / 1.10 * 200 + 310)
    assert v["shares"].value == pytest.approx(200)
    # Every filed figure cites its report on filings.xbrl.org.
    assert v["revenue"].sources[0]["url"].endswith("/reports/ixbrlviewer.html")
    assert any("annual accounts only" in w for w in snap.warnings)
    assert any("fundamentals are 296 days old" in w for w in snap.warnings)
    assert any("linked to this listing (manual)" in w for w in snap.warnings)


def test_ifrs_deal_is_translated_to_dollars_at_one_rate() -> None:
    snap = snapshot.build(load(), ff.SYMBOL, AS_OF)
    deal = snapshot.to_kci_deal(snap)
    assert deal["currency"] == "USD" and deal["reportingCurrency"] == "EUR"
    assert deal["translation"]["usdPerUnit"] == pytest.approx(1.10)
    assert deal["revenue"] == pytest.approx(1100 * 1.10)
    assert deal["netDebt"] == pytest.approx(310 * 1.10)
    assert deal["market"]["marketCap"] == pytest.approx(4.50 * 1.30 * 200)  # = pence → dollars directly
    assert deal["market"]["price"] == pytest.approx(4.50 * 1.30)
    # Ratios do not move with the translation.
    assert deal["ev"] / deal["ebitda"] == pytest.approx(snap.values["ev"].value / snap.values["ebitda"].value)
    out = snapshot.analyse(snap)
    assert out["ran"] and out["analysis"]["calcVersion"] == "kci-v3"


def test_annual_accounts_go_stale_after_the_annual_limit() -> None:
    late = snapshot.build(load(years=(2024,)), ff.SYMBOL, AS_OF)  # FY2024 only: 661 days old
    assert any(b.startswith("stale fundamentals") and "limit 500 days for annual accounts" in b
               for b in late.blocked_by)  # fmt: skip


def test_missing_dollar_rate_blocks_the_engines() -> None:
    st = load()
    st.db.execute("DELETE FROM rates WHERE series='DEXUSEU'")
    st.db.commit()
    snap = snapshot.build(st, ff.SYMBOL, AS_OF)
    assert any("no exchange rate to translate EUR accounts" in b for b in snap.blocked_by)
    assert not snapshot.analyse(snap)["ran"]


def test_unknown_price_currency_is_never_assumed() -> None:
    st = load()
    st.db.execute("DELETE FROM price_currency")
    st.db.commit()
    v = snapshot.build(st, ff.SYMBOL, AS_OF).values
    assert not v["marketCap"].known and "currency of EXM.L's price is not known" in v["marketCap"].method


def test_distress_screen_on_ifrs_accounts() -> None:
    res = distress.screen(load(), ff.SYMBOL, AS_OF)
    assert res["currency"] == "EUR" and res["accounts"] == {"source": "esef", "cadence": "annual"}
    checks = {c["name"]: c for c in res["checks"]}
    assert checks["interestCover"]["value"] == pytest.approx(130 / 24)
    assert checks["netLeverage"]["value"] == pytest.approx(310 / 180)
    assert checks["debtDue"]["value"] == pytest.approx(60 / 120)
    z = checks["altmanZ"]
    assert z["level"] != "unknown" and "industry not known" in z["thresholds"]
    x1, x2, x3, x4 = (600 - 400) / 2000, 300 / 2000, 130 / 2000, 750 / 1200
    assert z["value"] == pytest.approx(6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4)
    assert checks["drawdown"]["level"] == "unknown"  # one day of prices


def test_debt_rollforward_floor_uses_the_exchange_rate() -> None:
    st = load()
    cik = st.company(ff.SYMBOL)["cik"]  # type: ignore[index]
    fu = Fundamentals(st, cik, AS_OF)
    assert fu.currency == "EUR" and fu.taxonomy == "ifrs-full"
    assert fu.debt_rollforward(date(2025, 12, 31), date(2025, 1, 1)) is None  # no exchange rate: no finding
    fu.usd_per_unit = 1.10
    assert fu.debt_rollforward(date(2025, 12, 31), date(2025, 1, 1)) is None  # 340 + 30 − 30 = 340 found


# SEC foreign filers ------------------------------------------------------------------------------------------------
def sec_store(form: str) -> Store:
    st = Store(":memory:")
    rows = us.base_rows()
    st.put_facts(Fact(us.CIK, r[0], r[1], r[2], date.fromisoformat(r[3]) if r[3] else None, date.fromisoformat(r[4]),
                      r[5], r[6], form if r[7] == "10-K" else r[7], date.fromisoformat(r[8])) for r in rows)  # fmt: skip
    st.put_company(us.CIK, us.TICKER, us.NAME, "3990", "1231")
    from keers.data.models import Filing

    st.put_filings([Filing(us.CIK, "0000000001-25-000001", form, date(2025, 2, 14), date(2024, 12, 31))])
    st.put_prices([PricePoint(us.TICKER, date(2025, 11, 7), 40.0, "yahoo", raw_sha256="p")])
    return st


def test_us_listing_of_a_20f_filer_has_no_market_cap() -> None:
    v = snapshot.build(sec_store("20-F"), us.TICKER, date(2025, 11, 10)).values
    assert v["revenue"].known
    assert not v["marketCap"].known and "depositary share" in v["marketCap"].method
    v = snapshot.build(sec_store("40-F"), us.TICKER, date(2025, 11, 10)).values
    assert v["marketCap"].known  # 40-F filers list their own shares in New York


# linking ---------------------------------------------------------------------------------------------------------
def test_name_normalising() -> None:
    assert normalise("Exemplar Industrial Public Limited Company") == normalise("EXEMPLAR INDUSTRIAL PLC")
    assert normalise("Société Générale S.A.") == "SOCIETE GENERALE"
    assert normalise("Beispiel Aktiengesellschaft") == normalise("BEISPIEL AG")
    assert normalise("Exemplar Holdings plc") != normalise("Exemplar plc")  # "holdings" is kept


def test_name_match_needs_an_exact_normalised_name() -> None:
    recs = parse_lei_records(ff.gleif([(ff.LEI, "Exemplar Industrial Public Limited Company"),
                                       ("KEERSTEST0SUBSIDIA01", "Exemplar Industrial Services Limited")]))  # fmt: skip
    assert [r.lei for r in name_matches("EXEMPLAR INDUSTRIAL PLC", recs)] == [ff.LEI]
    assert name_matches("EXEMPLAR", recs) == []


def network(routes: dict[str, Any]) -> tuple[Fetcher, list[str]]:
    seen: list[str] = []

    def opener(req: urllib.request.Request, timeout: float) -> bytes:
        url = req.full_url
        seen.append(url)
        for key, body in routes.items():
            if key in url:
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]

    return Fetcher("Test agent test@example.com", opener=opener, sleep=lambda s: None, min_interval={}), seen


def test_find_links_by_name_and_refresh_reads_the_reports() -> None:
    routes = {
        "api.gleif.org/api/v1/lei-records?filter": ff.gleif(
            [(ff.LEI, "Exemplar Industrial Public Limited Company")]
        ),
        "api.gleif.org/api/v1/lei-records/": ff.gleif(
            [(ff.LEI, "Exemplar Industrial Public Limited Company")]
        ),
        f"/api/entities/{ff.LEI}/filings": ff.filings_list(
            [ff.filing(2024, "2025-03-20"), ff.filing(2025, "2026-03-20")]
        ),
        "exm-2024-12-31.json": ff.report(2024),
        "exm-2025-12-31.json": ff.report(2025),
    }
    fetcher, seen = network(routes)
    st = Store(":memory:")
    filers = Filers(fetcher, st)
    out = filers.find(ff.SYMBOL, "EXEMPLAR INDUSTRIAL PLC")
    assert out["linked"] and out["basis"] == "name-match" and out["lei"] == ff.LEI
    res = filers.refresh(ff.SYMBOL)
    assert res["filings"]["query"] == "entity" and len(res["filings"]["read"]) == 2
    co = st.company(ff.SYMBOL)
    assert co is not None and co["cik"] >= ENTITY_ID_BASE and co["link"]["basis"] == "name-match"
    assert Fundamentals(st, co["cik"], AS_OF).flow(("ifrs-full:Revenue",), "revenue").value == pytest.approx(
        1100
    )


def test_find_does_not_link_an_ambiguous_or_unmatched_name() -> None:
    two = ff.gleif([(ff.LEI, "Exemplar Industrial plc"), ("KEERSTEST0EXEMPLAR43", "Exemplar Industrial PLC")])
    lei43 = "KEERSTEST0EXEMPLAR43"
    routes = {"lei-records?filter": two,
              f"/api/entities/{ff.LEI}/filings": ff.filings_list([ff.filing(2025, "2026-03-20")]),
              f"/api/entities/{lei43}/filings": ff.filings_list([ff.filing(2025, "2026-03-20")], lei=lei43)}  # fmt: skip
    fetcher, _ = network(routes)
    st = Store(":memory:")
    out = Filers(fetcher, st).find(ff.SYMBOL, "Exemplar Industrial plc")
    assert not out["linked"] and st.link(ff.SYMBOL) is None and "2 LEI records" in out["reason"]
    fetcher, _ = network({"lei-records?filter": ff.gleif([(ff.LEI, "Other Name plc")])})
    out = Filers(fetcher, st).find(ff.SYMBOL, "Exemplar Industrial plc")
    assert not out["linked"] and out["reason"].startswith("no active LEI record")
    with pytest.raises(KeyError, match="not linked"):
        Filers(fetcher, st).refresh(ff.SYMBOL)


def test_filings_query_falls_back_and_ignores_unfiltered_results() -> None:
    # The entity route is not there; the filter route answers, but with another company first (as an API that
    # ignored the filter would): only the LEI's own report is kept.
    other = ff.filing(2025, "2026-03-21")
    other["attributes"] |= {"fxo_id": "KEERSTEST0SOMEONE001-x", "json_url": "/KEERSTEST0SOMEONE001/x.json"}
    other["relationships"]["entity"]["data"]["id"] = "99"
    fetcher, seen = network({"/api/filings?filter=": ff.filings_list([other, ff.filing(2025, "2026-03-20")])})
    found, how = esef.Esef(fetcher, Store(":memory:")).filings(ff.LEI)
    assert how == "filter" and [f.period_end.year for f in found] == [2025]


def test_store_links_and_entity_ids() -> None:
    st = Store(":memory:")
    a = st.entity_id("lei", ff.LEI.lower(), "A", "GB", "t")
    assert a == ENTITY_ID_BASE and st.entity_id("lei", ff.LEI, "A2", "GB", "t") == a
    assert st.entity_id("lei", "KEERSTEST0EXEMPLAR43", "B", None, "t") == a + 1
    st.put_company(a, ff.LEI, "A2", None, None)
    st.put_link("exm.l", a, "manual", "x")
    co = st.company("EXM.L")
    assert co is not None and co["ticker"] == "EXM.L" and co["cik"] == a and co["link"]["basis"] == "manual"
    st.drop_link("EXM.L")
    assert st.company("EXM.L") is None


def test_beta_prices_are_converted_to_dollars_day_by_day() -> None:
    d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 20)
    out = _in_usd([(d1, 100.0), (d2, 101.0), (d3, 102.0)], {d1: 1.3, d2: 1.31})
    assert out == [
        (d1, pytest.approx(130.0)),
        (d2, pytest.approx(132.31)),
        (d3, None),
    ]  # d3: rate 14 days old


def test_refresh_a_home_listing_end_to_end() -> None:
    """refresh(): name from the universe table, GLEIF link, ESEF reports, Yahoo prices in pence, FRED FX."""
    from keers.data.refresh import refresh

    # Thu 22 and Fri 23 Oct 2026, 08:00 London; the session that followed has closed.
    rows = [(1792652400, 448.0), (1792738800, 450.0)]

    def chart(sym: str) -> dict[str, Any]:
        doc = us.yahoo_chart(sym, rows, 1792769400, (1792738800, 1792769400))
        if sym != "SPY":
            doc["chart"]["result"][0]["meta"]["currency"] = "GBp"
        return doc

    routes: dict[str, Any] = {
        "api.gleif.org/api/v1/lei-records?filter": ff.gleif(
            [(ff.LEI, "Exemplar Industrial Public Limited Company")]
        ),
        f"/api/entities/{ff.LEI}/filings": ff.filings_list(
            [ff.filing(2024, "2025-03-20"), ff.filing(2025, "2026-03-20")]
        ),
        "exm-2024-12-31.json": ff.report(2024),
        "exm-2025-12-31.json": ff.report(2025),
        "chart/SPY": chart("SPY"),
        "chart/EXM.L": chart("EXM.L"),
        "id=DEXUSUK": us.fred_csv("DEXUSUK", [("2026-10-22", "1.30")]).encode(),
        "id=DEXUSEU": us.fred_csv("DEXUSEU", [("2026-10-22", "1.10")]).encode(),
    }
    fetcher, seen = network(routes)
    st = Store(":memory:")
    st.db.execute(
        "INSERT INTO listings (key, symbol, local_ticker, name, exchange, mic, country, currency, currency_basis, "
        "security_type, coverage, symbol_basis, is_primary, segment, source, as_of, raw_sha256, notes) VALUES "
        "('XLON:EXM', 'EXM.L', 'EXM', 'EXEMPLAR INDUSTRIAL PLC', 'LN', 'XLON', 'GB', 'GBX', 'exchange-default', "
        "'Common Stock', 'prices-only', 'derived', 1, 'openfigi:LN', 'openfigi', '2026-10-20', 'x', '')"
    )
    res = refresh(st, [ff.SYMBOL], fetcher, today=AS_OF, rates=False, sources=("yahoo",))
    assert res["status"] == "ok", res["failed"]
    done = res["done"][ff.SYMBOL]
    assert done["find"]["linked"] and set(done["fx"]) == {"DEXUSEU", "DEXUSUK"}
    assert st.price_currency(ff.SYMBOL, "yahoo") == "GBp"
    snap = snapshot.build(st, ff.SYMBOL, AS_OF)
    assert snap.values["marketCap"].value == pytest.approx(4.50 * 1.30 / 1.10 * 200)
    assert any("name-match" in w for w in snap.warnings)


# follow-up to the first live check ---------------------------------------------------------------------------------
def test_names_with_split_legal_forms_and_leading_ab() -> None:
    assert normalise("NOVO NORDISK A/S-B") == normalise("Novo Nordisk A/S") == "NOVO NORDISK"
    assert normalise("VOLVO AB-B") == normalise("AB Volvo") == normalise("Aktiebolaget Volvo") == "VOLVO"


def test_find_links_through_the_filings_xbrl_org_filer_list_first() -> None:
    entities = {"data": [{"type": "entity", "id": "1", "attributes": {"identifier": ff.LEI, "name": ff.LEGAL_NAME}},
                         {"type": "entity", "id": "2", "attributes": {"identifier": "KEERSTEST0SUBSIDIA01",
                                                                      "name": "Exemplar Industrial Services Ltd"}}],
                "links": {"next": None}}  # fmt: skip
    fetcher, seen = network({"/api/entities?page": entities})
    st = Store(":memory:")
    out = Filers(fetcher, st).find(ff.SYMBOL, "EXEMPLAR INDUSTRIAL PLC (FICTIONAL)")
    assert out["linked"] and out["lei"] == ff.LEI and out["via"] == "filings.xbrl.org filer list"
    assert not any("gleif" in u for u in seen)  # no GLEIF search needed
    link = st.link(ff.SYMBOL)
    assert link is not None and link["basis"] == "name-match"
    # The list is cached: a second find does not fetch it again.
    n = len(seen)
    Filers(fetcher, st).find("EXM2.L", "Exemplar Industrial plc (fictional)")
    assert len(seen) == n


def _load_renamed(renames: dict[str, str | None]) -> Store:
    """``load()`` with some concepts renamed (or dropped, with None) in every report."""
    st = Store(":memory:")
    cik = st.entity_id("lei", ff.LEI, ff.LEGAL_NAME, "GB", "test")
    st.put_company(cik, ff.LEI, ff.LEGAL_NAME, None, None)
    st.put_link(ff.SYMBOL, cik, "manual", f"LEI {ff.LEI}")
    for y in (2024, 2025):
        doc = ff.report(y)
        for k, fact in list(doc["facts"].items()):
            name = fact["dimensions"]["concept"].split(":", 1)[1]
            if name in renames:
                if renames[name] is None:
                    del doc["facts"][k]
                else:
                    fact["dimensions"]["concept"] = f"ifrs-full:{renames[name]}"
        f = esef_filing(y, f"{y + 1}-03-20")
        st.put_facts(esef.parse_xbrl_json(doc, ff.LEI, cik, f, f"sha-{y}")[0])
    return st


def test_da_from_parts_then_from_the_line_with_impairment() -> None:
    at = date(2025, 12, 31)
    st = _load_renamed({"DepreciationAndAmortisationExpense": "DepreciationPropertyPlantAndEquipment"})
    cik = st.company(ff.SYMBOL)["cik"]  # type: ignore[index]
    da = Fundamentals(st, cik, AS_OF).depreciation_amortisation(at)
    assert da.value == pytest.approx(50) and "DepreciationRightofuseAssets" in da.warnings[-1]
    st = _load_renamed({"DepreciationAndAmortisationExpense":
                        "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"})  # fmt: skip
    da = Fundamentals(st, st.company(ff.SYMBOL)["cik"], AS_OF).depreciation_amortisation(at)  # type: ignore[index]
    assert da.value == pytest.approx(50) and "includes impairment" in da.warnings[-1]
    st = _load_renamed({"DepreciationAndAmortisationExpense": None})
    assert not Fundamentals(st, st.company(ff.SYMBOL)["cik"], AS_OF).depreciation_amortisation(at).known  # type: ignore[index]


def test_finance_costs_get_a_wider_implied_rate_limit() -> None:
    from keers.data.models import Kind, Value

    st = load()
    fu = Fundamentals(st, st.company(ff.SYMBOL)["cik"], AS_OF)  # type: ignore[index]
    at = date(2025, 12, 31)

    def debt(m: float) -> Value:
        return Value(m, Kind.REPORTED, "EURm", "test", at, (), ())

    assert fu.debt_check(debt(120), at) is None  # 24 / 120 = 20%: fine for finance costs
    msg = fu.debt_check(debt(80), at)  # 30%
    assert msg is not None and "limit 25%" in msg


def test_cadence_of_sec_filers_comes_from_their_forms() -> None:
    from keers.data.models import Filing

    st = Store(":memory:")
    st.put_filings([Filing(1306965, "a1", "20-F", date(2026, 3, 12), date(2025, 12, 31)),
                    Filing(1306965, "a2", "6-K", date(2026, 7, 30), date(2026, 6, 30)),
                    Filing(320193, "b1", "10-K", date(2025, 11, 1), date(2025, 9, 27)),
                    Filing(320193, "b2", "10-Q", date(2026, 8, 1), date(2026, 6, 28))])  # fmt: skip
    assert Fundamentals(st, 1306965, AS_OF).cadence() == "annual"
    assert Fundamentals(st, 320193, AS_OF).cadence() == "quarterly"


def test_bonds_issued_are_read_only_when_no_borrowings_line_is_filed() -> None:
    st = _load_renamed({"NoncurrentPortionOfNoncurrentBorrowings": "NoncurrentPortionOfNoncurrentBondsIssued",
                        "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings":
                            "CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued"})  # fmt: skip
    debt = snapshot.build(st, ff.SYMBOL, AS_OF).values["debt"]
    assert debt.value == pytest.approx(280 + 60 + 90)
    assert any("bonds issued only" in w for w in debt.warnings)


def test_shares_derived_from_basic_eps_when_no_count_is_filed() -> None:
    st = _load_renamed({"NumberOfSharesOutstanding": None})
    cik = st.company(ff.SYMBOL)["cik"]  # type: ignore[index]
    fu = Fundamentals(st, cik, AS_OF)
    assert not fu.shares(None).known
    f = esef_filing(2025, "2026-03-20")
    doc = {"documentInfo": ff.report(2025)["documentInfo"],
           "facts": {"eps": ff._fact("BasicEarningsLossPerShare", ff.duration(2025), 0.385,
                                     "iso4217:EUR/xbrli:shares", 3),
                     "eps24": ff._fact("BasicEarningsLossPerShare", ff.duration(2024), 0.4, "iso4217:EUR/xbrli:shares",
                                       2)}}  # fmt: skip
    facts, _ = esef.parse_xbrl_json(doc, ff.LEI, cik, f, "sha-eps")
    assert {x.unit for x in facts} == {"EUR/shares"}
    st.put_facts(facts)
    fu = Fundamentals(st, cik, AS_OF)
    fu.shares_max_age = 500  # as the snapshot sets it for annual accounts
    v = fu.shares(None)
    assert v.value == pytest.approx(80 / 0.385) and v.kind.name == "DERIVED"
    assert "BasicEarningsLossPerShare" in v.method and "derived" in v.warnings[0]
    # Too coarse: 0.4 (one decimal place shown) could be anything from 0.35 to 0.45, so nothing is derived.
    st2 = _load_renamed({"NumberOfSharesOutstanding": None})
    st2.put_facts([x for x in facts if x.end.year == 2024])
    fu2 = Fundamentals(st2, cik, AS_OF)
    fu2.shares_max_age = 800
    assert not fu2.shares(None).known


def test_shares_from_eps_use_total_profit_when_no_owners_split_is_filed() -> None:
    st = _load_renamed(
        {"NumberOfSharesOutstanding": None, "ProfitLossAttributableToOwnersOfParent": "ProfitLoss"}
    )
    cik = st.company(ff.SYMBOL)["cik"]  # type: ignore[index]
    doc = {"documentInfo": ff.report(2025)["documentInfo"],
           "facts": {"eps": ff._fact("BasicEarningsLossPerShare", ff.duration(2025), 0.385,
                                     "iso4217:EUR/xbrli:shares", 3)}}  # fmt: skip
    st.put_facts(esef.parse_xbrl_json(doc, ff.LEI, cik, esef_filing(2025, "2026-03-20"), "sha-eps")[0])
    fu = Fundamentals(st, cik, AS_OF)
    fu.shares_max_age = 500
    v = fu.shares(None)
    assert v.value == pytest.approx(80 / 0.385) and "ifrs-full:ProfitLoss /" in v.method
