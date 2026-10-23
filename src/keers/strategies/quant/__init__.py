"""Strategy C, systematic quant: deterministic price signals, a point-in-time backtest and a rebalance plan.

Every number is worked out in code here. Signals read the store only through ``pit.PointInTime``, which refuses
any date after the as-of date, so a backtest cannot see the future. Trades are never placed from this package:
``rebalance`` only plans them, and the workbench sends each one through the paper desk and the risk limits.
"""

MODEL_VERSION = "quant-c-v1"
