"""Lock in what the KCI engine produces today.

These tests do not say the outputs are right. The audit found problems with
KCI's default assumptions. They say the outputs have not changed. Any later
fix ships as a new calculation version with its own expected outputs, so every
change is visible and deliberate.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "legacy" / "kci" / "harness.js"
GOLDEN = ROOT / "tests" / "golden" / "kci_v1.json"
REL_TOL = 1e-9

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required to run the KCI engine"
)


@pytest.fixture(scope="module")
def output() -> dict[str, Any]:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=True, timeout=120)
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text())


def assert_same(actual: Any, expected: Any, where: str = "") -> None:
    if isinstance(expected, float) or isinstance(actual, float):
        assert isinstance(actual, (int, float)) and isinstance(expected, (int, float)), where
        close = math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=1e-12)
        assert close, f"{where}: {actual} != {expected}"
    elif isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys(), f"{where}: keys differ"
        for k in expected:
            assert_same(actual[k], expected[k], f"{where}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{where}: length differs"
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            assert_same(a, e, f"{where}[{i}]")
    else:
        assert actual == expected, f"{where}: {actual!r} != {expected!r}"


def test_all_19_controlled_cases_pass(output: dict[str, Any]) -> None:
    cases = output["controlledCases"]
    assert len(cases) == 19
    failed = [c["name"] for c in cases if not c["ok"]]
    assert not failed, failed


def test_dropbox_headline_matches_readme(output: dict[str, Any]) -> None:
    # README: gross IRR 0.5% a year; highest price that still earns 20% is $24.26.
    d = output["dropbox"]
    assert d["allChecksPass"]
    assert round(d["irr"] * 100, 1) == 0.5
    assert round(d["maxPricePerShareAt20"], 2) == 24.26
    # README: "with no debt at all the deal would earn 2.8% a year".
    assert round(d["unleveredIrr"] * 100, 1) == 2.8


def test_dropbox_irr_independent_check(output: dict[str, Any]) -> None:
    """Re-solve the Dropbox IRR in Python by bisection, independent of the JS engine."""
    cfs = output["dropbox"]["sponsorCashFlows"]

    def npv(r: float) -> float:
        return sum(c / (1 + r) ** t for t, c in enumerate(cfs))

    lo, hi = -0.9, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    assert math.isclose(mid, output["dropbox"]["irr"], rel_tol=1e-8)


def test_universe_covers_100_companies(output: dict[str, Any]) -> None:
    assert len(output["companies"]) == 100


def test_outputs_match_golden_file(output: dict[str, Any], golden: dict[str, Any]) -> None:
    assert_same(output, golden, "kci_v1")
