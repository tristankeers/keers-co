"""Debt filed in ways the SEC company-facts API does not carry: credit-facility lines, company-specific concepts
and lines shown without a value. The company and every number are fictional."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from keers.data import fundamentals as F
from keers.data import lines as L
from keers.data import snapshot, xbrl
from keers.data.http import Response
from keers.data.store import Store
from keers.strategies import distress

from . import fixtures as fx
from .test_data_gaps import AS_OF, AT, ENDS, rows_without
from .test_data_layer import load, with_market

# A fictional 10-Q instance and schema, shaped like EDGAR's ``*_htm.xml`` and an ``.xsd`` with embedded linkbases.
INSTANCE = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2025"
  xmlns:exw="http://example.com/20250930" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
  xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <xbrli:context id="now"><xbrli:entity><xbrli:identifier scheme="x">9999999</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2025-09-30</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="ye"><xbrli:entity><xbrli:identifier scheme="x">9999999</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="vie"><xbrli:entity><xbrli:identifier scheme="x">9999999</xbrli:identifier><xbrli:segment>
  <xbrldi:explicitMember dimension="srt:ConsolidatedEntitiesAxis">us-gaap:VariableInterestEntityPrimaryBeneficiaryMember</xbrldi:explicitMember>
  </xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2025-09-30</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="q3"><xbrli:entity><xbrli:identifier scheme="x">9999999</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-09-30</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
 <us-gaap:Assets contextRef="now" unitRef="usd" decimals="-6">2000000000</us-gaap:Assets>
 <us-gaap:Assets contextRef="ye" unitRef="usd" decimals="-6">1900000000</us-gaap:Assets>
 <exw:TermLoanNoncurrent contextRef="now" unitRef="usd" decimals="-6">150000000</exw:TermLoanNoncurrent>
 <exw:TermLoanNoncurrent contextRef="ye" unitRef="usd" decimals="-6">150000000</exw:TermLoanNoncurrent>
 <exw:TermLoanNoncurrent contextRef="vie" unitRef="usd" decimals="-6">99000000</exw:TermLoanNoncurrent>
 <us-gaap:LinesOfCreditCurrent contextRef="ye" unitRef="usd" decimals="-6">25000000</us-gaap:LinesOfCreditCurrent>
 <us-gaap:LinesOfCreditCurrent contextRef="now" unitRef="usd" xsi:nil="true"/>
 <us-gaap:InterestExpense contextRef="q3" unitRef="usd" decimals="-6">30000000</us-gaap:InterestExpense>
</xbrli:xbrl>"""

SCHEMA = b"""<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:link="http://www.xbrl.org/2003/linkbase"
  xmlns:xlink="http://www.w3.org/1999/xlink">
 <xs:annotation><xs:appinfo><link:linkbase>
  <link:presentationLink xlink:type="extended" xlink:role="http://example.com/role/CONDENSEDCONSOLIDATEDBALANCESHEETS">
   <link:loc xlink:type="locator" xlink:href="https://xbrl.fasb.org/us-gaap.xsd#us-gaap_Assets" xlink:label="a"/>
   <link:loc xlink:type="locator" xlink:href="exw-20250930.xsd#exw_TermLoanNoncurrent" xlink:label="b"/>
   <link:loc xlink:type="locator" xlink:href="https://xbrl.fasb.org/us-gaap.xsd#us-gaap_LinesOfCreditCurrent" xlink:label="c"/>
  </link:presentationLink>
  <link:presentationLink xlink:type="extended" xlink:role="http://example.com/role/DebtDetails">
   <link:loc xlink:type="locator" xlink:href="https://xbrl.fasb.org/us-gaap.xsd#us-gaap_LongTermDebtNoncurrent" xlink:label="d"/>
  </link:presentationLink>
  <link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">
   <link:loc xlink:type="locator" xlink:href="exw-20250930.xsd#exw_TermLoanNoncurrent" xlink:label="loc_b"/>
   <link:label xlink:type="resource" xlink:label="lab_b" xlink:role="http://www.xbrl.org/2003/role/label">Term loan, non-current</link:label>
   <link:labelArc xlink:type="arc" xlink:from="loc_b" xlink:to="lab_b"/>
  </link:labelLink>
 </link:linkbase></xs:appinfo></xs:annotation>
</xs:schema>"""

CFG = L.CompanyLines(fx.TICKER, fx.CIK, "fictional review", (
    L.Line("exw:TermLoanNoncurrent", "debt-noncurrent", "Term loan, non-current"),
))  # fmt: skip


class FakeFetcher:
    def __init__(self, docs: dict[str, bytes]) -> None:
        self.docs, self.urls = docs, []  # type: ignore[var-annotated]

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        self.urls.append(url)
        return Response(url, self.docs[url], "sha-" + url.rsplit("/", 1)[-1], "2025-11-10T00:00:00+00:00", "")


def fetcher() -> FakeFetcher:
    folder = f"https://www.sec.gov/Archives/edgar/data/{fx.CIK}/{fx.Q3_25[0].replace('-', '')}"
    index = {"directory": {"item": [{"name": "exw-20250930.htm"}, {"name": "exw-20250930_htm.xml"},
                                    {"name": "exw-20250930.xsd"}]}}  # fmt: skip
    return FakeFetcher({
        xbrl.SUBMISSIONS_URL.format(cik=fx.CIK): json.dumps(fx.submissions()).encode(),
        f"{folder}/index.json": json.dumps(index).encode(),
        f"{folder}/exw-20250930_htm.xml": INSTANCE,
        f"{folder}/exw-20250930.xsd": SCHEMA,
    })  # fmt: skip


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "config", lambda: {fx.CIK: CFG})


# the instance reader ------------------------------------------------------------------------------------------
def test_parse_instance_keeps_dimensions_and_nil() -> None:
    facts, ext = xbrl.parse_instance(INSTANCE)
    assert ext == {"exw"}
    term = [f for f in facts if f.concept == "TermLoanNoncurrent"]
    assert {(f.end, f.value, bool(f.dims)) for f in term} == {
        (date(2025, 9, 30), 150e6, False),
        (date(2024, 12, 31), 150e6, False),
        (date(2025, 9, 30), 99e6, True),
    }
    nil = next(f for f in facts if f.concept == "LinesOfCreditCurrent" and f.end == date(2025, 9, 30))
    assert nil.value != nil.value  # NaN: tagged with no value


def test_linkbases_embedded_in_the_schema() -> None:
    roles = xbrl.parse_statements(SCHEMA)
    face = xbrl.statement_roles(roles, "balance")
    assert face == ["http://example.com/role/CONDENSEDCONSOLIDATEDBALANCESHEETS"]  # not the debt details note
    assert roles[face[0]] == ["us-gaap:Assets", "exw:TermLoanNoncurrent", "us-gaap:LinesOfCreditCurrent"]
    assert xbrl.parse_labels(SCHEMA) == {"exw:TermLoanNoncurrent": "Term loan, non-current"}


def test_instance_index_needs_exactly_one_instance() -> None:
    with pytest.raises(xbrl.InstanceError):
        xbrl.instance_name({"directory": {"item": [{"name": "a.htm"}]}})


# credit-facility lines (standard concepts) ----------------------------------------------------------------------
def credit_line_rows() -> list[fx.Row]:
    rows = rows_without("LongTermDebt")
    for e, f in ENDS:
        rows += [fx._inst("LongTermLineOfCredit", e, 300, f), fx._inst("LinesOfCreditCurrent", e, 40, f),
                 fx._inst("ConvertibleDebtCurrent", e, 60, f)]  # fmt: skip
    return rows


def test_credit_facility_borrowings_count_as_debt() -> None:  # Dropbox, Opendoor
    store = with_market(load(credit_line_rows()))
    snap = snapshot.build(store, "EXW", AS_OF)
    assert snap.values["debt"].value == pytest.approx(300 + 40 + 60)
    due = distress._debt_due(F.Fundamentals(store, fx.CIK, AS_OF), AT)
    assert due.value == pytest.approx(40 + 60)


def test_current_credit_line_is_not_added_beside_current_debt() -> None:
    rows = credit_line_rows() + [fx._inst("DebtCurrent", e, 100, f) for e, f in ENDS]
    d = snapshot.build(with_market(load(rows)), "EXW", AS_OF).values["debt"]
    assert d.value == pytest.approx(300 + 60 + 100)
    assert any("inside current debt" in w for w in d.warnings)


def test_noncurrent_credit_line_beside_long_term_debt_is_not_double_counted() -> None:
    rows = fx.base_rows() + [fx._inst("LongTermLineOfCredit", e, 300, f) for e, f in ENDS]
    d = snapshot.build(with_market(load(rows)), "EXW", AS_OF).values["debt"]
    assert d.value == pytest.approx(400)  # 380 non-current + 20 current, as without the credit line
    assert any("taken as already inside long-term debt" in w for w in d.warnings)


# company-specific lines and blanks ------------------------------------------------------------------------------
def test_refresh_reads_configured_lines_and_blanks(configured: None) -> None:
    store = load()
    info = L.refresh_lines(fetcher(), store, fx.CIK, set(F.DEBT_CONCEPTS))
    assert info is not None and info["lines"] == 2  # both dates, the dimensional fact left out
    assert info["blanks"] == ["us-gaap:LinesOfCreditCurrent at 2025-09-30"]
    stored = store.facts(fx.CIK, "TermLoanNoncurrent", "USD", taxonomy="exw")
    assert {(f.end, f.value, f.accn, f.filed) for f in stored} == {
        (date(2025, 9, 30), 150e6, fx.Q3_25[0], date(2025, 11, 4)),
        (date(2024, 12, 31), 150e6, fx.Q3_25[0], date(2025, 11, 4)),
    }


def test_configured_line_is_added_to_debt_and_blank_reads_as_zero(configured: None) -> None:
    store = with_market(load())
    L.refresh_lines(fetcher(), store, fx.CIK, set(F.DEBT_CONCEPTS))
    snap = snapshot.build(store, "EXW", AS_OF)
    d = snap.values["debt"]
    assert d.value == pytest.approx(380 + 20 + 150)
    assert "TermLoanNoncurrent" in d.method
    fu = F.Fundamentals(store, fx.CIK, AS_OF)
    blank = fu.balance(F.LINES_OF_CREDIT_CURRENT, "current credit lines", AT)
    assert blank.value == 0.0 and "without a value" in blank.warnings[0]
    assert distress._debt_due(fu, AT).value == pytest.approx(20)


def test_configured_line_without_a_read_filing_makes_debt_unknown(configured: None) -> None:
    snap = snapshot.build(with_market(load()), "EXW", AS_OF)  # instance never read
    assert snap.values["debt"].value is None
    assert "exw:TermLoanNoncurrent" in snap.values["debt"].method
    assert "missing netDebt" in " ".join(snap.blocked_by)


def test_lines_are_point_in_time(configured: None) -> None:
    store = with_market(load())
    L.refresh_lines(fetcher(), store, fx.CIK, set(F.DEBT_CONCEPTS))
    # Before the 10-Q was filed (4 Nov 2025) its lines were not known, so debt at the year end is unknown.
    v = F.Fundamentals(store, fx.CIK, date(2025, 3, 1)).total_debt(date(2024, 12, 31))
    assert v.value is None


def test_shipped_config_is_well_formed() -> None:
    cfg = L.config()
    assert {c.ticker for c in cfg.values()} >= {"PLUG", "KSS"}
    for c in cfg.values():
        assert c.reviewed and all(ln.role in L.ROLES and ln.label for ln in c.lines)


def test_bad_role_is_refused() -> None:
    doc: dict[str, Any] = {"company": [{"ticker": "X", "cik": 1, "reviewed": "r",
                                        "lines": [{"concept": "x:Y", "role": "guess"}]}]}  # fmt: skip
    with pytest.raises(ValueError):
        L.parse_config(doc)


# pre-tax income filed under the "including non-controlling interest, before tax" concept ------------------------
def test_pretax_fallback_reads_the_including_nci_concept() -> None:  # Xerox
    rows = rows_without("OperatingIncomeLoss")
    for c, vals in (("IncomeLossIncludingPortionAttributableToNoncontrollingInterest", (150, 105, 128)),
                    ("InterestExpense", (40, 30, 33))):  # fmt: skip
        fy, y24, y25 = vals
        rows += [fx._dur(c, "2024-01-01", "2024-12-31", fy, fx.K24),
                 fx._dur(c, "2024-01-01", "2024-09-30", y24, fx.Q3_25),
                 fx._dur(c, "2025-01-01", "2025-09-30", y25, fx.Q3_25)]  # fmt: skip
    ebit = snapshot.build(with_market(load(rows)), "EXW", AS_OF).values["ebit"]
    assert ebit.value == pytest.approx((150 - 105 + 128) + (40 - 30 + 33))
    assert any("pre-tax income + interest" in w for w in ebit.warnings)


def test_store_is_untouched_for_unlisted_companies() -> None:
    store = Store(":memory:")
    assert L.refresh_lines(fetcher(), store, 123, set()) is None
