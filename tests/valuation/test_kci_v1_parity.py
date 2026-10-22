"""The Python port of KCI must give the same answers as the frozen JavaScript engine.

Two checks:

1. All 100 companies in KCI's dataset, through the full analysis step, against the golden file that the
   JavaScript engine produced (``tests/golden/kci_v1.json``).
2. Randomised cases for every engine function, run through both engines side by side. The cases are
   generated from a fixed seed so any failure can be reproduced, and they deliberately include the awkward
   paths: zero debt, losses, revolver draws, equity cures, wiped-out equity, rollovers and short holds.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from keers.valuation import kci_v1 as K
from keers.valuation.kci_v1_defaults import summarise

ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).with_name("js_runner.js")
GOLDEN = ROOT / "tests" / "golden" / "kci_v1.json"
SEED = ROOT / "legacy" / "kci" / "seed.json"
REL_TOL = 1e-9
N_LBO, N_DCF, N_OTHER = 1000, 500, 200

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for parity tests")


# ---------- comparison ----------


def normalise(v: Any) -> Any:
    """Match JSON.stringify: non-finite numbers become null; tuples become lists."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: normalise(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [normalise(x) for x in v]
    return v


def assert_same(py: Any, js: Any, where: str) -> None:
    py_num = isinstance(py, (int, float)) and not isinstance(py, bool)
    js_num = isinstance(js, (int, float)) and not isinstance(js, bool)
    if py_num and js_num:
        assert math.isclose(py, js, rel_tol=REL_TOL, abs_tol=1e-9), f"{where}: python {py} != js {js}"
    elif isinstance(js, dict):
        assert isinstance(py, dict), f"{where}: python {type(py).__name__}, js object"
        extra = set(py) - set(js) - {"calcVersion"}  # Python results also record their calculation version
        missing = set(js) - set(py)
        assert not extra and not missing, f"{where}: python extra {extra}, missing {missing}"
        for k in js:
            assert_same(py[k], js[k], f"{where}.{k}")
    elif isinstance(js, list):
        assert isinstance(py, list) and len(py) == len(js), f"{where}: length {len(py)} != {len(js)}"
        for i, (a, b) in enumerate(zip(py, js, strict=True)):
            assert_same(a, b, f"{where}[{i}]")
    else:
        assert py == js, f"{where}: python {py!r} != js {js!r}"


# ---------- 1. the KCI universe against the golden file ----------


def test_all_100_companies_match_javascript_golden_file() -> None:
    golden = {c["id"]: c for c in json.loads(GOLDEN.read_text())["companies"]}
    deals = list(json.loads(SEED.read_text())["deals"].values())
    py = {c["id"]: normalise(c) for c in summarise(deals)}
    assert py.keys() == golden.keys()
    for cid, expected in golden.items():
        assert_same(py[cid], expected, cid)


def test_dropbox_headline_in_python() -> None:
    dbx = json.loads(SEED.read_text())["deals"]["us-dbx"]
    r = K.run_lbo(dbx["lbo"])
    assert r["allChecksPass"]
    assert round(r["returns"]["irr"] * 100, 1) == 0.5
    m = K.max_entry_multiple(dbx["lbo"], 0.2)
    assert m is not None
    price = (m * dbx["lbo"]["entryEbitda"] - dbx["netDebt"]) / dbx["market"]["sharesM"]
    assert round(price, 2) == 24.26


# ---------- 2. randomised cases ----------


def _growth(rng: random.Random, n: int) -> Any:
    kind = rng.random()
    if kind < 0.2:
        return round(rng.uniform(-0.1, 0.25), 4)  # one rate for every year
    if kind < 0.3:
        return [round(rng.uniform(-0.1, 0.25), 4) for _ in range(rng.randint(1, max(1, n - 1)))]  # short list
    return [round(rng.uniform(-0.15, 0.3), 4) for _ in range(n)]


def _margins(rng: random.Random, n: int) -> Any:
    lo = -0.08 if rng.random() < 0.15 else 0.03  # some loss-making plans
    if rng.random() < 0.2:
        return round(rng.uniform(lo, 0.5), 4)
    return [round(rng.uniform(lo, 0.5), 4) for _ in range(n)]


def random_lbo(rng: random.Random) -> dict[str, Any]:
    hold = rng.randint(1, 10)
    ebitda = round(rng.uniform(5, 3000), 2)
    revenue = round(ebitda / rng.uniform(0.05, 0.6), 2)
    entry = round(rng.uniform(4, 20), 3)
    n_tr = rng.choice([0, 1, 2, 2, 3])
    tranches = [
        {
            "name": f"T{k + 1}",
            "leverage": round(rng.uniform(0, 3), 3),
            "rate": round(rng.uniform(0, 0.15), 4),
            "amortPct": rng.choice([0, 0, 0.01, 0.05, 0.1, round(rng.uniform(0, 0.3), 3)]),
            "sweep": rng.random() < 0.6,
        }
        for k in range(n_tr)
    ]
    inp: dict[str, Any] = {
        "entryRevenue": revenue,
        "entryEbitda": ebitda,
        "entryMultiple": entry,
        "holdYears": hold,
        "tranches": tranches,
        "revenueGrowth": _growth(rng, hold),
        "ebitdaMargin": _margins(rng, hold),
        "taxRate": rng.choice([0, 0.21, 0.25, round(rng.uniform(0, 0.4), 3)]),
        "exitMultiple": round(rng.uniform(1, 20), 3),
        "daPct": round(rng.uniform(0, 0.1), 4),
        "capexPct": round(rng.uniform(0, 0.15), 4),
        "nwcPct": round(rng.uniform(-0.05, 0.2), 4),
    }
    if rng.random() < 0.5:
        inp.update(
            transactionFeePct=round(rng.uniform(0, 0.04), 4),
            financingFeePct=round(rng.uniform(0, 0.04), 4),
            exitFeePct=round(rng.uniform(0, 0.03), 4),
        )
    if rng.random() < 0.5:
        inp["minCash"] = round(revenue * rng.uniform(0, 0.05), 2)
    if rng.random() < 0.5:
        inp["revolver"] = {
            "commitment": round(ebitda * rng.uniform(0, 1), 2),
            "rate": round(rng.uniform(0.03, 0.1), 4),
        }
    if rng.random() < 0.4:
        inp["cashSweepPct"] = round(rng.uniform(0, 1), 3)
    if rng.random() < 0.3:
        inp["rollover"] = round(ebitda * rng.uniform(0, 2), 2)
    if rng.random() < 0.4:
        net = ebitda * rng.uniform(-1, 3)
        inp["existingDebt"] = round(max(0, net), 2)
        inp["existingCash"] = round(max(0, -net), 2)
    if rng.random() < 0.3:
        inp["interestOnCashRate"] = round(rng.uniform(0, 0.05), 4)
    if rng.random() < 0.2:
        inp["rateShift"] = round(rng.uniform(-0.01, 0.03), 4)
    if rng.random() < 0.15:
        inp["entryEVOverride"] = round(ebitda * rng.uniform(4, 20), 2)
    if rng.random() < 0.03:
        inp["entryEbitda"] = rng.choice([0, -5, None])  # rejected inputs must be rejected the same way
    return inp


def random_dcf(rng: random.Random) -> dict[str, Any]:
    n = rng.randint(1, 15)
    w = round(rng.uniform(0.05, 0.15), 4)
    inp: dict[str, Any] = {
        "baseRevenue": round(rng.uniform(10, 50000), 2),
        "years": n,
        "revenueGrowth": _growth(rng, n),
        "ebitdaMargin": _margins(rng, n),
        "daPct": round(rng.uniform(0, 0.1), 4),
        "capexPct": round(rng.uniform(0, 0.15), 4),
        "nwcPct": round(rng.uniform(-0.05, 0.2), 4),
        "taxRate": round(rng.uniform(0, 0.35), 3),
        "wacc": w,
        "terminalMethod": rng.choice(["gordon", "exit", "both"]),
        "terminalGrowth": round(rng.uniform(-0.01, min(0.04, w - 0.005)), 4),
        "exitMultiple": round(rng.uniform(3, 25), 2),
        "midYear": rng.random() < 0.6,
    }
    if rng.random() < 0.8:
        inp["netDebt"] = round(rng.uniform(-2000, 8000), 2)
    if rng.random() < 0.6:
        inp["sharesOutstanding"] = round(rng.uniform(10, 2000), 3)
    if rng.random() < 0.3:
        inp["otherClaims"] = round(rng.uniform(0, 500), 2)
        inp["nonOperatingAssets"] = round(rng.uniform(0, 500), 2)
    if rng.random() < 0.03:
        inp["terminalGrowth"] = w + 0.01  # must be rejected
    return inp


def random_company(rng: random.Random) -> dict[str, Any]:
    def maybe(v: Any, p: float = 0.1) -> Any:
        return None if rng.random() < p else v

    ebitda = round(rng.uniform(-200, 5000), 2)
    return {
        "name": f"Co{rng.randint(1, 9999)}",
        "include": rng.random() > 0.1,
        "ev": maybe(round(rng.uniform(50, 90000), 2)),
        "revenue": maybe(round(rng.uniform(10, 40000), 2)),
        "ebitda": maybe(ebitda),
        "ebit": maybe(round(ebitda * rng.uniform(0.3, 1), 2)),
        "marketCap": maybe(round(rng.uniform(50, 80000), 2)),
        "netIncome": maybe(round(rng.uniform(-500, 4000), 2)),
        "netDebt": maybe(round(rng.uniform(-3000, 9000), 2)),
        "capex": maybe(round(rng.uniform(0, 900), 2)),
        "revenueGrowth": maybe(round(rng.uniform(-0.2, 0.4), 4)),
        "strategicFit": maybe(rng.randint(1, 5), 0.5),
        "sector": rng.choice(["Software", "Energy", "Defence"]),
        "geography": rng.choice(["United States", "UK"]),
        "dataStatus": rng.choice(["verified", "crosschecked", "sourced"]),
        "reportingPeriod": rng.choice(["TTM to Jun 30, 2026", "FY2025", None]),
    }


def build_cases() -> list[tuple[str, dict[str, Any]]]:
    rng = random.Random(20261022)
    cases: list[tuple[str, dict[str, Any]]] = []
    for _ in range(N_LBO):
        cases.append(("runLBO", random_lbo(rng)))
    for _ in range(N_DCF):
        cases.append(("runDCF", random_dcf(rng)))
    for _ in range(N_OTHER):
        n = rng.randint(2, 12)
        cfs = [round(rng.uniform(-1000, 1000), 2) for _ in range(n)]
        cases.append(("irr", {"cfs": cfs}))
        cases.append(("npv", {"rate": round(rng.uniform(-0.5, 0.5), 4), "cfs": cfs}))
        cases.append(
            ("stats", {"values": [round(rng.uniform(-10, 60), 3) for _ in range(rng.randint(0, 15))]})
        )
        cases.append(
            (
                "evBridge",
                {
                    "ebitda": round(rng.uniform(-50, 900), 2),
                    "multiple": round(rng.uniform(2, 25), 2),
                    "netDebt": rng.choice([None, round(rng.uniform(-500, 2000), 2)]),
                    "otherClaims": round(rng.uniform(0, 50), 2),
                    "nonOperatingAssets": round(rng.uniform(0, 50), 2),
                },
            )
        )
        cases.append(
            (
                "wacc",
                {
                    "riskFree": round(rng.uniform(0, 0.06), 4),
                    "beta": round(rng.uniform(0.4, 2), 3),
                    "erp": round(rng.uniform(0.03, 0.08), 4),
                    "sizePremium": rng.choice([0, 0.01, 0.02]),
                    "preTaxCostOfDebt": round(rng.uniform(0.03, 0.12), 4),
                    "taxRate": round(rng.uniform(0, 0.35), 3),
                    "debtWeight": rng.choice([round(rng.uniform(0, 0.8), 3), 1.0, -0.1]),
                },
            )
        )
        lbo = random_lbo(rng)
        cases.append(("breakEven", {"lbo": lbo, "target": rng.choice([0.15, 0.2, 0.25])}))
        cases.append(("chartSeries", {"lbo": lbo}))
        sc = {
            "revenueGrowth": round(rng.uniform(-0.1, 0.2), 4),
            "exitMargin": round(rng.uniform(0.05, 0.5), 4),
            "exitMultiple": round(rng.uniform(4, 18), 2),
            "rateShift": rng.choice([0, 0.015]),
            "leverage": rng.choice([None, round(rng.uniform(0, 6), 2)]),
            "entryMultiple": rng.choice([None, lbo["entryMultiple"], round(rng.uniform(4, 20), 2)]),
            "holdYears": rng.choice([None, rng.randint(1, 10)]),
        }
        if (
            isinstance(lbo["revenueGrowth"], list)
            and isinstance(lbo["ebitdaMargin"], list)
            and K.is_num(lbo.get("entryEbitda"))
        ):
            cases.append(("applyScenario", {"base": lbo, "sc": sc}))
        comps = [random_company(rng) for _ in range(rng.randint(0, 9))]
        cases.append(
            ("compsAnalysis", {"companies": comps, "target": rng.choice([None, random_company(rng)])})
        )
        co = random_company(rng)
        cases.append(("scoreTarget", {"deal": co}))
        cases.append(
            (
                "passesFilters",
                {
                    "deal": co,
                    "filters": {
                        "minRevenue": rng.choice([None, 100]),
                        "maxMultiple": rng.choice([None, 12]),
                        "maxLeverage": rng.choice([None, 4]),
                        "minMargin": rng.choice([None, 0.1]),
                        "sectors": rng.choice([[], ["Software"]]),
                        "verifiedOnly": rng.random() < 0.2,
                        "minPeriod": rng.choice(["", "FY2026"]),
                    },
                },
            )
        )
        cases.append(
            (
                "recommend",
                {
                    "m": {
                        "baseIrr": rng.choice([None, round(rng.uniform(-0.2, 0.4), 4)]),
                        "baseMoic": rng.choice([None, round(rng.uniform(0.2, 4), 3)]),
                        "downsideMoic": rng.choice([None, round(rng.uniform(0, 3), 3)]),
                        "entryLeverage": round(rng.uniform(0, 8), 2),
                        "score": rng.choice([None, round(rng.uniform(0, 100), 2)]),
                        "checksPass": rng.random() < 0.9,
                        "dataVerified": rng.random() < 0.5,
                        "dataLabel": "sourced",
                    }
                },
            )
        )
    for _ in range(40):  # each call runs dozens of LBOs, so fewer cases
        cases.append(("maxEntryMultiple", {"lbo": random_lbo(rng), "target": rng.choice([0.15, 0.2])}))
    return cases


def run_python(fn: str, a: dict[str, Any]) -> Any:
    if fn == "runLBO":
        return K.run_lbo(a)
    if fn == "runDCF":
        return K.run_dcf(a)
    if fn == "irr":
        return K.irr(a["cfs"])
    if fn == "npv":
        return K.npv(a["rate"], a["cfs"])
    if fn == "stats":
        return K.stats(a["values"])
    if fn == "evBridge":
        return K.ev_bridge(**a)
    if fn == "wacc":
        return K.wacc(a)
    if fn == "breakEven":
        return K.break_even(K.run_lbo(a["lbo"]), a["target"])
    if fn == "chartSeries":
        return K.chart_series(K.run_lbo(a["lbo"]))
    if fn == "applyScenario":
        return K.apply_scenario(a["base"], a["sc"])
    if fn == "compsAnalysis":
        return K.comps_analysis(a["companies"], a["target"])
    if fn == "scoreTarget":
        return K.score_target(a["deal"])
    if fn == "passesFilters":
        return K.passes_filters(a["deal"], a["filters"])
    if fn == "recommend":
        return K.recommend(a["m"])
    if fn == "maxEntryMultiple":
        return K.max_entry_multiple(a["lbo"], a["target"])
    raise ValueError(fn)


@pytest.fixture(scope="module")
def paired() -> list[tuple[str, dict[str, Any], Any, Any]]:
    cases = build_cases()
    payload = json.dumps([{"fn": fn, "args": a} for fn, a in cases])
    proc = subprocess.run(
        ["node", str(RUNNER)], input=payload, capture_output=True, text=True, check=True, timeout=600
    )
    js_out = json.loads(proc.stdout)
    assert len(js_out) == len(cases)
    out = []
    for (fn, a), js in zip(cases, js_out, strict=True):
        assert js["ok"], f"JS engine threw on {fn}: {js.get('error')}"
        out.append((fn, a, normalise(run_python(fn, a)), js["value"]))
    return out


@needs_node
def test_random_cases_cover_the_awkward_paths(paired: list[tuple[str, dict[str, Any], Any, Any]]) -> None:
    lbos = [js for fn, _, _, js in paired if fn == "runLBO"]
    ok = [r for r in lbos if r["ok"]]
    assert len(lbos) == N_LBO
    assert len(ok) > N_LBO * 0.5
    assert sum(1 for r in ok if r["returns"]["cures"] > 0) >= 20, "equity cures"
    assert sum(1 for r in ok if any(y["revolverDraw"] > 0 for y in r["years"])) >= 20, "revolver draws"
    assert sum(1 for r in ok if r["exit"]["exitEquityRaw"] < 0) >= 20, "wiped-out equity"
    assert sum(1 for r in ok if r["entry"]["newDebt"] == 0) >= 10, "zero debt"
    assert sum(1 for r in lbos if not r["ok"]) >= 10, "rejected inputs"


@needs_node
@pytest.mark.parametrize(
    "fn",
    [
        "runLBO",
        "runDCF",
        "irr",
        "npv",
        "stats",
        "evBridge",
        "wacc",
        "breakEven",
        "chartSeries",
        "applyScenario",
        "compsAnalysis",
        "scoreTarget",
        "passesFilters",
        "recommend",
        "maxEntryMultiple",
    ],
)
def test_python_matches_javascript(paired: list[tuple[str, dict[str, Any], Any, Any]], fn: str) -> None:
    subset = [(a, py, js) for f, a, py, js in paired if f == fn]
    assert subset, f"no cases for {fn}"
    for i, (_a, py, js) in enumerate(subset):
        assert_same(py, js, f"{fn}[{i}]")
