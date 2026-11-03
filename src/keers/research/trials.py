"""The trial register: every model version tried for a strategy, and its one-time holdout runs.

The Deflated Sharpe Ratio is only honest if it knows how many models were tried, including the ones that were
dropped. So every backtested configuration is recorded here, in a reviewed file in the repository
(``config/research/trials.json``), with the run it came from. A backtest of a configuration that is not in the
register counts as one more trial and says so.

The holdout is the stretch of history from ``holdoutStart`` on. It is left out of the backtest statistics, and a
model's holdout result is looked at once and recorded here (``holdoutRuns``). Looking again is allowed only on
purpose (``--again``) and is recorded as such, because every extra look spends the holdout.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_REGISTER = Path(__file__).resolve().parents[3] / "config" / "research" / "trials.json"


def config_hash(config: dict[str, Any]) -> str:
    """Short SHA-256 of the model configuration, with keys sorted, so the same settings always hash the same."""
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Register:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> Register:
        p = Path(path) if path else DEFAULT_REGISTER
        data = json.loads(p.read_text())
        if data.get("version") != 1:
            raise ValueError(f"{p}: unknown register version {data.get('version')}")
        return cls(p, data)

    def strategy(self, name: str) -> dict[str, Any]:
        try:
            out: dict[str, Any] = self.data["strategies"][name]
            return out
        except KeyError:
            raise KeyError(f"strategy {name} is not in the trial register {self.path}") from None

    def trials(self, name: str) -> list[dict[str, Any]]:
        return list(self.strategy(name).get("trials", []))

    def holdout_start(self, name: str) -> date:
        return date.fromisoformat(self.strategy(name)["holdoutStart"])

    def summary(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """Trial count and recorded Sharpe ratios, counting ``config`` as one more trial if it is not registered."""
        trials = self.trials(name)
        h = config_hash(config)
        registered = any(t.get("configHash") == h for t in trials)
        return {
            "configHash": h,
            "registered": registered,
            "trials": len(trials) + (0 if registered else 1),
            "recordedSharpes": [t["sharpe"] for t in trials if t.get("sharpe") is not None],
            "holdoutStart": self.holdout_start(name).isoformat(),
        }

    def holdout_runs(self, name: str, h: str) -> list[dict[str, Any]]:
        return [r for r in self.strategy(name).get("holdoutRuns", []) if r.get("configHash") == h]

    def record_holdout(self, name: str, entry: dict[str, Any]) -> None:
        self.strategy(name).setdefault("holdoutRuns", []).append(entry)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
