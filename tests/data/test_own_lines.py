"""Reviewed company-specific lines (own_lines.toml): read only for listed companies, used only when the standard
line is missing, and always flagged. Every company and figure here is fictional (tests/data/foreign_fixtures.py),
except the checks that the real config parses."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from keers.data import esef, own_lines, snapshot
from keers.data.fundamentals import Fundamentals
from keers.data.models import PricePoint, RatePoint
from keers.data.store import Store
from keers.data.xbrl import Instance, InstanceFact

from . import foreign_fixtures as ff

AS_OF = date(2026, 10, 23)
CAPEX = "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"
OWN = "exm:CapitalExpenditureOnTangibleAssets"


def cfg(sign: int = 1, stands_for: str = "capex", concept: str = OWN) -> tuple[own_lines.OwnLines, ...]:
    return tuple(own_lines.parse_config({"company": [{
        "name": "Exemplar", "lei": ff.LEI, "reviewed": "test: annual report 2025",
        "lines": [{"concept": concept, "stands_for": stands_for, "label": "Capital expenditure", "sign": sign,
                   "note": "includes intangibles"}],
    }]}))  # fmt: skip


def esef_filing(year: int) -> esef.EsefFiling:
    return esef.parse_filings(ff.filings_list([ff.filing(year, f"{year + 1}-03-20")]), ff.LEI)[0]


def load(own: str | None, value_sign: int = 1, keep_standard: bool = False) -> Store:
    """The fictional company with its capex filed under ``own`` (its own concept) instead of the standard one."""
    st = Store(":memory:")
    cik = st.entity_id("lei", ff.LEI, ff.LEGAL_NAME, "GB", "test")
    st.put_company(cik, ff.LEI, ff.LEGAL_NAME, None, None)
    st.put_link(ff.SYMBOL, cik, "manual", f"LEI {ff.LEI}")
    for y in (2024, 2025):
        doc: dict[str, Any] = ff.report(y)
        for body in doc["facts"].values():
            dims = body["dimensions"]
            if dims["concept"] == f"ifrs-full:{CAPEX}" and own:
                if keep_standard:
                    extra = {**body, "dimensions": {**dims, "concept": own}, "value": str(999 * ff.M)}
                    doc["facts"][f"own-{dims['period']}"] = extra
                    break
                dims["concept"] = own
                body["value"] = str(value_sign * float(body["value"]))
        listed = own_lines.for_lei(ff.LEI)
        own_set = listed.extension_concepts() if listed else set()
        facts, _ = esef.parse_xbrl_json(doc, ff.LEI, cik, esef_filing(y), f"sha-{y}", own=own_set)
        st.put_facts(facts)
        st.put_filings([esef_filing(y).as_filing(cik)])
    st.put_prices([PricePoint(ff.SYMBOL, date(2026, 10, 22), 450.0, "yahoo", raw_sha256="px")])
    st.put_price_currency(ff.SYMBOL, "yahoo", "GBp", "px")
    st.put_rates([RatePoint("DEXUSUK", date(2026, 10, 22), 1.30, "fred", "uk"),
                  RatePoint("DEXUSEU", date(2026, 10, 22), 1.10, "fred", "eu"),
                  RatePoint("DGS10", date(2026, 10, 22), 4.0, "fred", "t")])  # fmt: skip
    return st


def test_the_real_config_parses_and_every_entry_names_its_review() -> None:
    entries = own_lines.config()
    assert entries
    for e in entries:
        assert e.reviewed and (e.lei is None) != (e.cik is None) and e.lines
        assert all(ln.stands_for in own_lines.STANDS_FOR and ln.label for ln in e.lines)


def test_config_rules() -> None:
    base = {"name": "X", "reviewed": "r", "lines": [{"concept": "x:A", "stands_for": "capex"}]}
    with pytest.raises(ValueError, match="exactly one"):
        own_lines.parse_config({"company": [{**base, "lei": ff.LEI, "cik": 1}]})
    with pytest.raises(ValueError, match="no reviewed"):
        own_lines.parse_config({"company": [{**base, "lei": ff.LEI, "reviewed": " "}]})
    with pytest.raises(ValueError, match="bad line"):
        own_lines.parse_config({"company": [{**base, "lei": ff.LEI,
                                             "lines": [{"concept": "x:A", "stands_for": "revenue"}]}]})  # fmt: skip
    with pytest.raises(ValueError, match="bad line"):
        own_lines.parse_config({"company": [{**base, "cik": 1,
                                             "lines": [{"concept": "x:A", "stands_for": "capex", "sign": 2}]}]})  # fmt: skip


def test_esef_parser_keeps_only_the_listed_own_concepts() -> None:
    doc = ff.report(2025)
    st = Store(":memory:")
    cik = st.entity_id("lei", ff.LEI, ff.LEGAL_NAME, "GB", "test")
    facts, rep = esef.parse_xbrl_json(doc, ff.LEI, cik, esef_filing(2025), "sha", own={"exm:AdjustedRevenue"})
    ext = [x for x in facts if x.taxonomy == "exm"]
    assert [(x.concept, x.value) for x in ext] == [("AdjustedRevenue", 123.0)]
    assert "not an ifrs-full concept" not in rep.skipped
    facts, rep = esef.parse_xbrl_json(doc, ff.LEI, cik, esef_filing(2025), "sha")
    assert not [x for x in facts if x.taxonomy == "exm"] and rep.skipped["not an ifrs-full concept"] == 1


def test_own_capex_is_used_only_when_the_standard_line_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not listed: capex is unknown and the company cannot be valued.
    monkeypatch.setattr(own_lines, "config", lambda: ())
    snap = snapshot.build(load(OWN), ff.SYMBOL, AS_OF)
    assert not snap.values["capex"].known
    # Listed and reviewed: used, with a warning naming the line and the review.
    monkeypatch.setattr(own_lines, "config", cfg)
    snap = snapshot.build(load(OWN), ff.SYMBOL, AS_OF)
    capex = snap.values["capex"]
    assert capex.value == pytest.approx(40) and capex.method.startswith(OWN)
    assert any("own line" in w and "test: annual report 2025" in w and "includes intangibles" in w
               for w in capex.warnings)  # fmt: skip
    # The standard line wins when both are filed.
    snap = snapshot.build(load(OWN, keep_standard=True), ff.SYMBOL, AS_OF)
    assert snap.values["capex"].value == pytest.approx(40) and "ifrs-full" in snap.values["capex"].method
    assert not any("own line" in w for w in snap.values["capex"].warnings)


def test_own_line_filed_negative_is_reversed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(own_lines, "config", lambda: cfg(sign=-1))
    capex = snapshot.build(load(OWN, value_sign=-1), ff.SYMBOL, AS_OF).values["capex"]
    assert capex.value == pytest.approx(40) and "sign reversed" in capex.method


def test_own_operating_income_d_and_a_and_interest(monkeypatch: pytest.MonkeyPatch) -> None:
    at = date(2025, 12, 31)
    for stands_for, standard, attr in (("operating-income", "ProfitLossFromOperatingActivities", "ebit"),
                                       ("da", "DepreciationAndAmortisationExpense", "da"),
                                       ("interest", "FinanceCosts", "interest")):  # fmt: skip
        own = f"exm:Own{standard}"
        monkeypatch.setattr(own_lines, "config", lambda s=stands_for, c=own: cfg(stands_for=s, concept=c))
        st = Store(":memory:")
        cik = st.entity_id("lei", ff.LEI, ff.LEGAL_NAME, "GB", "test")
        for y in (2024, 2025):
            doc = ff.report(y)
            for body in doc["facts"].values():
                if body["dimensions"]["concept"] == f"ifrs-full:{standard}":
                    body["dimensions"]["concept"] = own
            st.put_facts(esef.parse_xbrl_json(doc, ff.LEI, cik, esef_filing(y), "s", own={own})[0])
        fu = Fundamentals(st, cik, AS_OF)
        if attr == "ebit":
            v = fu.operating_income(at)
            assert v.value == pytest.approx(130)
        elif attr == "da":
            v = fu.depreciation_amortisation(at)
            assert v.value == pytest.approx(50)
        else:
            v = fu.interest_expense(at, fu.total_debt(at))
            assert v.value == pytest.approx(24)
        assert v.method.startswith(own) and any("own line" in w for w in v.warnings)


def test_sec_instance_facts_keep_listed_extension_lines_only() -> None:
    own = own_lines.parse_config({"company": [{"name": "X", "cik": 99, "reviewed": "r", "lines": [
        {"concept": "xco:CapexPaid", "stands_for": "capex", "label": "Capex", "sign": -1}]}]})[0]  # fmt: skip
    nan = float("nan")
    facts = [
        InstanceFact("xco", "CapexPaid", "CAD", date(2025, 1, 1), date(2025, 12, 31), -6.0e9, (), "c1"),
        InstanceFact(
            "xco", "CapexPaid", "CAD", date(2025, 1, 1), date(2025, 12, 31), -1.0e9, (("a", "m"),), "c2"
        ),
        InstanceFact("xco", "CapexPaid", "pure", date(2025, 1, 1), date(2025, 12, 31), 1.0, (), "c3"),
        InstanceFact("xco", "CapexPaid", "CAD", date(2024, 1, 1), date(2024, 12, 31), nan, (), "c4"),
        InstanceFact("xco", "OtherLine", "CAD", date(2025, 1, 1), date(2025, 12, 31), 5.0, (), "c5"),
    ]
    inst = Instance("u", "0000000099-26-000001", "40-F", date(2026, 3, 1), "sha", facts, {"xco"})
    out = own_lines.instance_facts(inst, 99, own)
    assert [(f.taxonomy, f.concept, f.unit, f.value, f.form) for f in out] == [
        ("xco", "CapexPaid", "CAD", -6.0e9, "40-F")
    ]
