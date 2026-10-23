"""The risk limits, as one versioned, validated set of numbers.

The defaults below are ``risk-limits-v1``. They can be replaced by a JSON file (``KEERS_RISK_LIMITS``), which
must give every limit: a file with a missing, extra or out-of-range key is refused, so a typo can never switch a
limit off.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STRATEGIES = ("A", "B", "C")
STRATEGY_NAMES = {
    "A": "Distressed and special situations",
    "B": "Durable-moat compounders",
    "C": "Systematic quant multi-strategy",
}


def _default_strategy_caps() -> dict[str, float]:
    return {"A": 0.40, "B": 0.60, "C": 0.40}


@dataclass(frozen=True)
class Limits:
    version: str = "risk-limits-v1"
    # Concentration, as a share of net asset value (NAV). Long and short both count (absolute market value).
    max_position: float = 0.10  # one position
    max_sector: float = 0.25  # all positions in one sector
    max_strategy: dict[str, float] = field(default_factory=_default_strategy_caps)  # one strategy's sleeve
    # Liquidity, as a share of the average daily volume (ADV) in shares.
    adv_days: int = 20  # sessions averaged
    min_adv_days: int = 10  # fewer sessions with volume than this: ADV is unknown
    max_trade_adv: float = 0.10  # one trade
    max_position_adv: float = 0.25  # the whole position after the trade
    # Exposure, as a share of NAV.
    max_gross: float = 1.50  # longs + |shorts|
    max_net: float = 1.00  # longs - |shorts|
    min_net: float = -0.30
    # Drawdown from the highest NAV so far.
    drawdown_warn: float = 0.10  # flagged
    drawdown_halt: float = 0.15  # no trade may add gross exposure
    # Data quality.
    max_price_age_days: int = 3  # business days; older prices are stale
    base_currency: str = "USD"  # no FX conversion yet, so other currencies are refused

    def __post_init__(self) -> None:
        problems = validate(self)
        if problems:
            raise ValueError("invalid risk limits: " + "; ".join(problems))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Limits:
        names = set(cls.__dataclass_fields__)
        missing, extra = names - set(d), set(d) - names
        if missing or extra:
            raise ValueError(
                f"risk limits file: missing {sorted(missing) or 'nothing'}, unknown {sorted(extra) or 'nothing'}"
            )
        return cls(**d)

    @classmethod
    def from_json(cls, path: str | Path) -> Limits:
        return cls.from_dict(json.loads(Path(path).read_text()))


def _share(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and 0 < x <= 5


def validate(lim: Limits) -> list[str]:
    p: list[str] = []
    for name in ("max_position", "max_sector", "max_trade_adv", "max_position_adv", "max_gross", "max_net",
                 "drawdown_warn", "drawdown_halt"):  # fmt: skip
        if not _share(getattr(lim, name)):
            p.append(f"{name} must be a number above 0 and at most 5")
    if not isinstance(lim.min_net, int | float) or not -5 <= lim.min_net <= 0:
        p.append("min_net must be between -5 and 0")
    if not isinstance(lim.max_strategy, dict) or set(lim.max_strategy) != set(STRATEGIES):
        p.append(f"max_strategy must give a cap for each of {', '.join(STRATEGIES)}")
    elif not all(_share(v) for v in lim.max_strategy.values()):
        p.append("each max_strategy cap must be above 0 and at most 5")
    for name in ("adv_days", "min_adv_days", "max_price_age_days"):
        v = getattr(lim, name)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 260:
            p.append(f"{name} must be a whole number from 1 to 260")
    if (
        isinstance(lim.adv_days, int)
        and isinstance(lim.min_adv_days, int)
        and lim.min_adv_days > lim.adv_days
    ):
        p.append("min_adv_days cannot exceed adv_days")
    if _share(lim.drawdown_warn) and _share(lim.drawdown_halt) and lim.drawdown_warn > lim.drawdown_halt:
        p.append("drawdown_warn cannot exceed drawdown_halt")
    if not isinstance(lim.base_currency, str) or len(lim.base_currency) != 3:
        p.append("base_currency must be a 3-letter currency code")
    return p
