"""Strategy A, distressed and special situations: from the distress screen to research-ready candidates.

- ``analysis``: liquidity runway, the funding gap over the next year, and what the equity and the debt would be
  worth if the business were sold at stated distressed multiples (``a-analysis-v1``).
- ``candidates``: the screen's flagged companies, filtered for size, price and trading volume, ranked, and
  compared with the previous run (``a-candidates-v1``).
- ``pipeline``: screen, candidates, research agents and paper-desk previews in one run, with a report.

Deterministic throughout. The research agents (``keers.agents``) read these figures as facts; they never compute
them.
"""
