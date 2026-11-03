"""Strategy A candidates: the distress screen's flagged companies that are big and liquid enough to research.

From a screen run (``keers.strategies.distress.run``):

1. Only ``distressed`` and ``stressed`` companies are candidates. ``incomplete`` ones are listed apart as needing
   data, never dropped silently; ``blocked`` ones are listed with the reason.
2. Each candidate must pass the size and trading filters below, all in US dollars: market value, share price and
   average daily traded value over the last 20 sessions. A company that fails one is listed as excluded, with
   the reason. A figure that cannot be measured excludes the company (unknown never passes).
3. Candidates are ranked by severity: two points for each severe check and one for each watch check, then by the
   fall from the 52-week high (bigger falls first), then by ticker.
4. With the previous run's result, the changes are listed: new candidates, ones that dropped out, and status
   changes.

Being a candidate is not a finding. It says a company is worth a researcher's time, nothing more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from keers.data.store import Store
from keers.portfolio.market import daily_rows

CANDIDATES_VERSION = "a-candidates-v1"

MIN_MARKET_CAP_M = 100.0  # USD millions
MIN_PRICE = 1.0  # USD a share
MIN_DOLLAR_ADV_M = 1.0  # USD millions traded a day, 20-session average
ADV_SESSIONS, ADV_MIN_SESSIONS = 20, 10
CANDIDATE_STATUSES = ("distressed", "stressed")


def dollar_adv(store: Store, ticker: str, as_of: date) -> tuple[float | None, int]:
    """Average daily traded value in millions (close × volume) over the last ``ADV_SESSIONS`` sessions."""
    rows = daily_rows(store, ticker, as_of - timedelta(days=45), as_of)
    traded = [c * v for _, c, v, _, _ in rows if c and v and v > 0][-ADV_SESSIONS:]
    if len(traded) < ADV_MIN_SESSIONS:
        return None, len(traded)
    return sum(traded) / len(traded) / 1e6, len(traded)


def severity(row: Mapping[str, Any]) -> int:
    return sum({"severe": 2, "watch": 1}.get(c["level"], 0) for c in row.get("checks") or [])


def _check(row: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return next((c for c in row.get("checks") or [] if c["name"] == name), {})


def _market(store: Store, row: Mapping[str, Any], as_of: date) -> tuple[dict[str, Any], list[str]]:
    """Market value, price and traded value for one screened company, and why it fails the filters."""
    z = _check(row, "altmanZ").get("inputs") or {}
    mcap = (z.get("marketCap") or {}).get("value")
    px = store.last_price(row["ticker"], as_of)
    adv, n = dollar_adv(store, row["ticker"], as_of)
    fig = {"marketCapM": mcap, "price": px.close if px else None, "priceDay": px.day.isoformat() if px else None,
           "dollarAdvM": adv, "advSessions": n, "currency": row.get("currency")}  # fmt: skip
    why: list[str] = []
    if row.get("currency") != "USD":
        why.append(f"accounts in {row.get('currency')}; the paper desk trades US dollars only")
    if mcap is None:
        why.append("market value unknown")
    elif mcap < MIN_MARKET_CAP_M:
        why.append(f"market value {mcap:,.0f}m is below {MIN_MARKET_CAP_M:,.0f}m")
    if px is None:
        why.append("no share price")
    elif px.close < MIN_PRICE:
        why.append(f"share price {px.close:,.2f} is below {MIN_PRICE:,.2f}")
    if adv is None:
        why.append(f"traded value unknown ({n} sessions with volume, needs {ADV_MIN_SESSIONS})")
    elif adv < MIN_DOLLAR_ADV_M:
        why.append(f"trades {adv:,.2f}m a day, below {MIN_DOLLAR_ADV_M:,.2f}m")
    return fig, why


def build(store: Store, screen: Mapping[str, Any], as_of: date,
          previous: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    rows = screen.get("companies") or []
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    needs_data: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for r in rows:
        flags = [{"name": c["name"], "level": c["level"], "reason": c["reason"]}
                 for c in r.get("checks") or [] if c["level"] in ("severe", "watch")]  # fmt: skip
        head = {"ticker": r["ticker"], "name": r.get("name"), "status": r["status"], "severity": severity(r),
                "flags": flags}  # fmt: skip
        if r["status"] == "blocked":
            blocked.append(head | {"blockedBy": r.get("blockedBy") or []})
            continue
        if r["status"] == "incomplete":
            unknown = [c["name"] for c in r.get("checks") or [] if c["level"] == "unknown"]
            needs_data.append(head | {"unknownChecks": unknown})
            continue
        if r["status"] not in CANDIDATE_STATUSES:
            continue
        fig, why = _market(store, r, as_of)
        drop = _check(r, "drawdown").get("value")
        item = head | fig | {"drawdown": drop}
        (excluded if why else included).append(item | ({"excludedBecause": why} if why else {}))
    included.sort(
        key=lambda c: (-c["severity"], c["drawdown"] if c["drawdown"] is not None else 0.0, c["ticker"])
    )
    for i, c in enumerate(included, 1):
        c["rank"] = i
    out = {"asOf": as_of.isoformat(), "candidatesVersion": CANDIDATES_VERSION,
           "screenVersion": screen.get("screenVersion"), "screened": len(rows),
           "filters": {"statuses": list(CANDIDATE_STATUSES), "minMarketCapM": MIN_MARKET_CAP_M, "minPrice": MIN_PRICE,
                       "minDollarAdvM": MIN_DOLLAR_ADV_M, "advSessions": ADV_SESSIONS},
           "candidates": included, "excluded": excluded, "needsData": needs_data, "blocked": blocked}  # fmt: skip
    if previous is not None:
        out["changes"] = changes(previous, out)
    return out


def changes(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """New candidates, ones that dropped out (with their status now, if screened), and status changes."""
    before = {c["ticker"]: c for c in previous.get("candidates") or []}
    now = {c["ticker"]: c for c in current.get("candidates") or []}
    status_now: dict[str, str] = {}
    for group in ("excluded", "needsData", "blocked"):
        for c in current.get(group) or []:
            status_now[c["ticker"]] = c["status"] if group != "excluded" else f"{c['status']} (excluded)"
    return {
        "since": previous.get("asOf"),
        "new": sorted(set(now) - set(before)),
        "dropped": [
            {"ticker": t, "now": status_now.get(t, "no flags or not screened")}
            for t in sorted(set(before) - set(now))
        ],  # fmt: skip
        "statusChanged": [
            {"ticker": t, "from": before[t]["status"], "to": now[t]["status"]}
            for t in sorted(set(before) & set(now))
            if before[t]["status"] != now[t]["status"]
        ],  # fmt: skip
    }


def table(cands: Sequence[Mapping[str, Any]]) -> list[str]:
    """Markdown rows for the candidate list."""
    out = ["| Rank | Company | Status | Severity | Flags | Market value | Traded a day | From 52w high |",
           "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for c in cands:
        flags = ", ".join(f"{f['name']} ({f['level']})" for f in c["flags"]) or "none"
        dd = f"{c['drawdown']:.0%}" if c.get("drawdown") is not None else "?"
        out.append(f"| {c['rank']} | {c['ticker']} | {c['status']} | {c['severity']} | {flags} | "
                   f"${c['marketCapM']:,.0f}m | ${c['dollarAdvM']:,.1f}m | {dd} |")  # fmt: skip
    return out
