"""A periodic filing that SEC company facts does not carry yet is read from the filing's own XBRL instance.

Seen with Visa: its 10-Q of 29 July 2026 was missing from company facts on 3 November 2026, so the latest accounts
ended in March and the snapshot was blocked as stale. The company and every number here are fictional.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from keers.data import fundamentals as F
from keers.data import sec, snapshot, xbrl
from keers.data.http import FetchError, Response
from keers.data.store import Store

from . import fixtures as fx
from .test_company_lines import FakeFetcher
from .test_data_layer import with_market

AS_OF = date(2025, 11, 7)
FOLDER = f"https://www.sec.gov/Archives/edgar/data/{fx.CIK}/{fx.Q3_25[0].replace('-', '')}"


def instance(rows: list[fx.Row]) -> bytes:
    """An instance document holding ``rows`` (all from one filing), shaped like EDGAR's ``*_htm.xml``."""
    ctx: dict[tuple[str | None, str], str] = {}
    body = []
    for tax, concept, unit, start, end, val, *_ in rows:
        cid = ctx.setdefault((start, end), f"c{len(ctx)}")
        uid = {"USD": "usd", "shares": "shares"}[unit]
        body.append(
            f'<{tax}:{concept} contextRef="{cid}" unitRef="{uid}" decimals="-6">{val:.0f}</{tax}:{concept}>'
        )
    contexts = []
    for (start, end), cid in ctx.items():
        period = (f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>" if start
                  else f"<xbrli:instant>{end}</xbrli:instant>")  # fmt: skip
        contexts.append(f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">{fx.CIK}'
                        f"</xbrli:identifier></xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>")  # fmt: skip
    # A dimensioned fact and a nil fact, neither of which company facts would carry.
    contexts.append(f'<xbrli:context id="seg"><xbrli:entity><xbrli:identifier scheme="x">{fx.CIK}</xbrli:identifier>'
                    '<xbrli:segment><xbrldi:explicitMember dimension="srt:SegmentsAxis">exw:GadgetsMember'
                    "</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>"
                    "2025-01-01</xbrli:startDate><xbrli:endDate>2025-09-30</xbrli:endDate></xbrli:period>"
                    "</xbrli:context>")  # fmt: skip
    body.append('<us-gaap:Revenues contextRef="seg" unitRef="usd" decimals="-6">123000000</us-gaap:Revenues>')
    body.append('<us-gaap:ShortTermBorrowings contextRef="c0" unitRef="usd" xsi:nil="true"/>')
    return (
        '<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:dei="http://xbrl.sec.gov/dei/2025" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
        '<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
        + "".join(contexts)
        + "".join(body)
        + "</xbrli:xbrl>"
    ).encode()


class Net(FakeFetcher):
    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        if url not in self.docs:
            raise FetchError(f"{url}: 404")
        return super().get(url, headers)


def network(lagging: bool, with_instance: bool = True) -> Net:
    rows = fx.base_rows()
    q3 = [r for r in rows if r[6] == fx.Q3_25[0]]
    facts_rows = [r for r in rows if r[6] != fx.Q3_25[0]] if lagging else rows
    docs = {
        sec.SUBMISSIONS_URL.format(cik=fx.CIK): json.dumps(fx.submissions()).encode(),
        sec.FACTS_URL.format(cik=fx.CIK): json.dumps(fx.company_facts(facts_rows)).encode(),
        f"{FOLDER}/index.json": json.dumps({"directory": {"item": [{"name": "exw-10q_htm.xml"}]}}).encode(),
    }
    if with_instance:
        docs[f"{FOLDER}/exw-10q_htm.xml"] = instance(q3)
    return Net(docs)


def refreshed(net: Net) -> tuple[Store, dict]:  # type: ignore[type-arg]
    store = Store(":memory:")
    out = sec.SEC(net, store).refresh_cik(fx.CIK, F.ALL_CONCEPTS, fx.TICKER)  # type: ignore[arg-type]
    return with_market(store), out


def test_a_filing_missing_from_company_facts_is_read_from_its_instance() -> None:
    store, out = refreshed(network(lagging=True))
    assert out["fromInstance"] == [{"filing": fx.Q3_25[0], "form": "10-Q", "filed": "2025-11-04",
                                    "facts": out["fromInstance"][0]["facts"]}]  # fmt: skip
    snap = snapshot.build(store, fx.TICKER, AS_OF)
    assert not any(b.startswith("stale") for b in snap.blocked_by)
    assert snap.values["revenue"].period_end == date(2025, 9, 30)
    # Same figures as when company facts carries the filing; the segment fact and the nil fact are left out.
    full, out_full = refreshed(network(lagging=False))
    assert "fromInstance" not in out_full
    ref = snapshot.build(full, fx.TICKER, AS_OF)
    for k in ("revenue", "ebit", "netIncome", "capex", "netDebt", "shares"):
        assert snap.values[k].value == pytest.approx(ref.values[k].value), k
    rev = store.facts(fx.CIK, "Revenues", unit="USD")
    assert all(f.value != 123e6 for f in rev)
    assert {f.filed for f in rev if f.accn == fx.Q3_25[0]} == {
        date(2025, 11, 4)
    }  # point in time: the filing date


def test_a_failed_instance_read_leaves_the_accounts_stale_not_guessed() -> None:
    store, out = refreshed(network(lagging=True, with_instance=False))
    assert "fromInstance" not in out and out["fromInstanceErrors"][0].startswith(f"10-Q {fx.Q3_25[0]}")
    snap = snapshot.build(store, fx.TICKER, date(2025, 11, 20))
    assert snap.values["revenue"].period_end == date(2025, 6, 30)


def test_only_filings_newer_than_every_stored_fact_are_fetched() -> None:
    store, _ = refreshed(network(lagging=False))
    _, filings = sec.parse_submissions(fx.submissions())
    assert sec.missing_filings(store, fx.CIK, filings) == []


def test_instance_units_are_spelt_as_company_facts_spells_them() -> None:
    assert sec._unit("iso4217:USD/xbrli:shares") == "USD/shares"
    assert sec._unit("shares") == "shares"
    assert xbrl.PERIODIC  # the instance reader is shared with the company-lines and own-lines readers
