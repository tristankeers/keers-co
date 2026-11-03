"""Research statistics, checked against the papers' worked examples and against FICTIONAL random returns."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from keers.research import stats
from keers.research.__main__ import main as cli
from keers.research.trials import Register, config_hash


def test_dsr_matches_the_papers_worked_example() -> None:
    # Bailey and López de Prado (2014), section 5: annual SR 2.5 over 1,250 days (250 a year), skew -3,
    # kurtosis 10, 100 trials whose annual Sharpe ratios vary by 0.5. The paper gives DSR = 0.9004.
    sr = 2.5 / math.sqrt(250)
    bar = stats.expected_max_sharpe(100, 0.5 / 250)
    assert bar * math.sqrt(250) == pytest.approx(1.7894, abs=1e-4)
    assert stats.psr(sr, 1250, -3, 10, bar) == pytest.approx(0.9004, abs=1e-4)


def test_psr_by_hand_for_normal_returns() -> None:
    # Normal returns: PSR(0) = Φ(SR √(n−1) / √(1 + SR²/2)).
    sr, n = 0.05, 1000
    want = stats._N.cdf(sr * math.sqrt(n - 1) / math.sqrt(1 + sr**2 / 2))
    assert stats.psr(sr, n, 0.0, 3.0) == pytest.approx(want)
    assert stats.min_track_record(sr, 0.0, 3.0) == pytest.approx(1 + (1 + sr**2 / 2) * (1.6448536 / sr) ** 2)
    assert stats.min_track_record(-0.01, 0.0, 3.0) is None


def test_one_trial_has_no_selection_bar_and_more_trials_raise_it() -> None:
    assert stats.expected_max_sharpe(1, 0.01) == 0.0
    bars = [stats.expected_max_sharpe(n, 1e-4) for n in (2, 10, 100, 1000)]
    assert bars == sorted(bars) and bars[0] > 0


def test_sharpe_test_needs_two_trial_sharpes_when_several_were_tried() -> None:
    rng = random.Random(1)
    xs = [rng.gauss(0.0005, 0.01) for _ in range(1000)]
    t = stats.sharpe_test(xs, [0.6], n_trials=3)
    assert t.dsr is None and t.psr is not None and "fewer than two" in t.reason
    one = stats.sharpe_test(xs, [], n_trials=1)
    assert one.dsr == pytest.approx(one.psr)  # nothing selected, so DSR is PSR against zero
    many = stats.sharpe_test(xs, [0.2, 0.9], n_trials=50)
    assert many.dsr < one.dsr


def test_noise_does_not_pass_and_a_strong_edge_does() -> None:
    rng = random.Random(7)
    noise = [rng.gauss(0.0, 0.01) for _ in range(2500)]
    edge = [rng.gauss(0.002, 0.01) for _ in range(2500)]  # annual Sharpe about 3: fictional
    assert stats.sharpe_test(noise, [0.5, 0.8], 2).dsr < 0.95
    assert stats.sharpe_test(edge, [0.5, 0.8], 2).dsr > 0.99


def test_bootstrap_interval_is_seeded_and_brackets_the_estimate() -> None:
    rng = random.Random(3)
    xs = [rng.gauss(0.0004, 0.01) for _ in range(1500)]
    a = stats.bootstrap_sharpe_ci(xs, resamples=300)
    assert a == stats.bootstrap_sharpe_ci(xs, resamples=300)
    est = stats.sharpe(xs) * math.sqrt(252)
    assert a[0] < est < a[1]
    assert stats.bootstrap_sharpe_ci(xs[:30]) is None


def test_pbo_low_for_a_real_edge_high_for_picking_among_noise() -> None:
    rng = random.Random(11)
    t = 1600
    real = {"edge": [rng.gauss(0.002, 0.01) for _ in range(t)], **{f"n{i}": [rng.gauss(0, 0.01) for _ in range(t)]
                                                                     for i in range(4)}}  # fmt: skip
    assert stats.pbo_cscv(real, blocks=8).pbo < 0.1
    noise = {f"n{i}": [rng.gauss(0, 0.01) for _ in range(t)] for i in range(10)}
    r = stats.pbo_cscv(noise, blocks=8)
    assert 0.25 < r.pbo < 0.75  # selection among noise is a coin toss out of sample
    assert r.splits == math.comb(8, 4) and sum(r.selected.values()) == r.splits


def test_pbo_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        stats.pbo_cscv({"a": [0.0] * 400})
    with pytest.raises(ValueError):
        stats.pbo_cscv({"a": [0.0] * 400, "b": [0.0] * 399})
    with pytest.raises(ValueError):
        stats.pbo_cscv({"a": [0.0] * 100, "b": [0.0] * 100}, blocks=16)


def test_register_counts_unregistered_configurations(tmp_path: Path) -> None:
    reg = Register.load()  # the real register must load and hold strategy C's recorded trials
    assert len(reg.trials("C")) >= 2
    cfg = {"version": "x", "nLong": 3}
    s = reg.summary("C", cfg)
    assert not s["registered"] and s["trials"] == len(reg.trials("C")) + 1
    assert config_hash(cfg) == config_hash({"nLong": 3, "version": "x"})


def _fake_backtest(tmp: Path, name: str, h: str, drift: float, seed: int) -> str:
    rng, v, curve = random.Random(seed), 1.0, []
    d0 = 738000  # ordinal days, FICTIONAL
    import datetime as dt

    for i in range(900):
        v *= 1 + rng.gauss(drift, 0.01)
        curve.append([dt.date.fromordinal(d0 + i).isoformat(), v])
    p = tmp / f"{name}.json"
    p.write_text(json.dumps({"ran": True, "model": name, "curve": curve, "riskFreeDaily": [0.0] * 900,
                             "research": {"configHash": h}}))  # fmt: skip
    return str(p)


def test_cli_pbo_and_one_time_holdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = tmp_path / "trials.json"
    import datetime as dt

    holdout = dt.date.fromordinal(738000 + 700).isoformat()
    reg.write_text(json.dumps({"version": 1, "strategies": {"C": {"holdoutStart": holdout, "trials": []}}}))
    a = _fake_backtest(tmp_path, "a", "h1", 0.001, 1)
    b = _fake_backtest(tmp_path, "b", "h2", 0.0, 2)
    assert cli(["--register", str(reg), "pbo", a, b, "--blocks", "8"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["models"] == 2 and out["to"] < holdout  # the holdout is left out
    assert cli(["--register", str(reg), "holdout", a, "--record"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["days"] == 200 and first["look"] == 1 and first["warnings"]
    with pytest.raises(SystemExit):
        cli(["--register", str(reg), "holdout", a])
    assert cli(["--register", str(reg), "holdout", a, "--again"]) == 0
    assert json.loads(capsys.readouterr().out)["look"] == 2
