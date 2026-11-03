"""Strategy B, durable-moat compounders: a deterministic quality screen (``screen``) and its point-in-time backtest
(``backtest``). Separate mandate from Strategy A (distressed) and C (systematic quant); it shares only the data
layer, the risk limits and the paper desk."""

from .screener import LEVELS, PASSING, SCREEN_VERSION, STATUSES, run, screen

__all__ = ["LEVELS", "PASSING", "SCREEN_VERSION", "STATUSES", "run", "screen"]
