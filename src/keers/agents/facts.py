"""The fact sheet: every figure an agent may use, read from the deterministic views and never from a model.

Each fact has a short key (``ev``, ``dcf.vsPrice``, ``distress.interestCover`` ...). Agents refer to a figure only by
writing its key in braces, ``{ev}``; code puts the formatted value and its source in place afterwards (see
``guard``). So a model can never type a number, and every number in a memo traces back to a fact here.

Facts come from the workbench views: the company profile (filed figures with their SEC filings), the kci-v2
valuation and the distress screen. Text facts (a check's reason, the engine's rationale) are sentences those
engines wrote themselves.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class Views(Protocol):
    """What the fact sheet reads. ``keers.web.service.Workbench`` provides all three."""

    def company(self, symbol: str) -> dict[str, Any]: ...
    def valuation(self, symbol: str) -> dict[str, Any]: ...
    def distress(self, symbol: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Fact:
    key: str
    title: str
    value: float | str | None
    unit: str  # USDm | USD | x | ratio | % | m shares | score | text
    label: str  # reported | derived | market | assumption | estimate | engine
    method: str
    source: str  # where it came from, in words
    link: str | None = None
    period_end: str | None = None
    stale: bool = False

    @property
    def known(self) -> bool:
        return self.value is not None and self.value != ""

    def formatted(self) -> str:
        return fmt_value(self.value, self.unit)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "formatted": self.formatted()}


def fmt_value(v: float | str | None, unit: str) -> str:
    """How a figure is written in a memo. Formatting only: rounding for display, no arithmetic on the figure."""
    if v is None or v == "":
        return "not available"
    if isinstance(v, str):
        return v
    if unit == "USDm":
        return f"${v:,.1f}m" if abs(v) < 1e5 else f"${v / 1000:,.1f}bn"
    if unit == "USD":
        return f"${v:,.2f}"
    if unit == "x":
        return f"{v:,.2f}x"
    if unit in ("ratio", "pct"):
        return f"{v * 100:,.1f}%"
    if unit == "%":
        return f"{v:,.2f}%"
    if unit == "shares":
        return f"{v:,.0f} shares"
    if unit == "m shares":
        return f"{v:,.1f}m shares"
    if unit == "score":
        return f"{v:,.0f} out of 100"
    return f"{v:,.4g} {unit}".strip()


@dataclass
class FactSheet:
    symbol: str
    name: str
    as_of: str | None
    facts: dict[str, Fact] = field(default_factory=dict)
    blocked_by: list[str] = field(
        default_factory=list
    )  # why the numbers cannot be relied on (stale, missing)
    warnings: list[str] = field(default_factory=list)
    distress_status: str | None = None
    valuation_ran: bool = False

    def add(self, f: Fact) -> None:
        self.facts[f.key] = f

    def get(self, key: str) -> float | str | None:
        f = self.facts.get(key)
        return f.value if f is not None else None

    def num(self, key: str) -> float | None:
        v = self.get(key)
        return float(v) if isinstance(v, int | float) else None

    def table(self) -> str:
        """The facts as the model sees them: key, title, value, label and source, one per line."""
        lines = []
        for f in self.facts.values():
            stale = " [STALE]" if f.stale else ""
            lines.append(f"{{{f.key}}} | {f.title} | {f.formatted()}{stale} | {f.label} | {f.source}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "name": self.name, "asOf": self.as_of, "blockedBy": self.blocked_by,
                "warnings": self.warnings, "distressStatus": self.distress_status, "valuationRan": self.valuation_ran,
                "facts": [f.as_dict() for f in self.facts.values()]}  # fmt: skip

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True, default=str).encode()).hexdigest()


def _src(fig: Mapping[str, Any]) -> tuple[str, str | None]:
    srcs = fig.get("sources") or []
    text = "; ".join(str(s.get("text") or "") for s in srcs if s.get("text"))
    link = next((s.get("link") for s in srcs if s.get("link")), None)
    return text or str(fig.get("method") or ""), link


def _get(d: Any, *path: str | int) -> Any:
    for p in path:
        if isinstance(p, int):
            d = d[p] if isinstance(d, list) and len(d) > p else None
        else:
            d = d.get(p) if isinstance(d, Mapping) else None
    return d


def build(views: Views, symbol: str) -> FactSheet:
    """Read the profile, the valuation and the distress screen for one listing into a fact sheet."""
    c = views.company(symbol)
    sym = c.get("symbol") or symbol
    name = _get(c, "sec", "name") or _get(c, "listing", "name") or sym
    snap = c.get("snapshot") or {}
    sheet = FactSheet(sym, str(name), snap.get("asOf"))
    if not c.get("sec"):
        sheet.blocked_by.append("no SEC filings in the store for this listing, so there are no filed figures")
    sheet.blocked_by += list(snap.get("blockedBy") or [])
    sheet.warnings += list(snap.get("warnings") or [])

    for f in c.get("figures") or []:
        src, link = _src(f)
        sheet.add(Fact(f["key"], f["title"], f.get("value"), f.get("unit") or "", f.get("label") or "",
                       str(f.get("method") or ""), src, link, f.get("periodEnd"), bool(f.get("stale"))))  # fmt: skip
    for r in c.get("ratios") or []:
        sheet.add(Fact(r["key"], r["title"], r.get("value"), r.get("unit") or "x", "derived", r.get("method") or "",
                       "worked out in code from the figures above: " + str(r.get("method") or "")))  # fmt: skip

    _valuation(sheet, views.valuation(sym))
    _distress(sheet, views.distress(sym))
    return sheet


def _valuation(sheet: FactSheet, v: Mapping[str, Any]) -> None:
    if not v.get("ran"):
        sheet.warnings += [f"valuation did not run: {b}" for b in v.get("blockedBy") or []]
        return
    sheet.valuation_ran = True
    calc = str(v.get("calcVersion") or "")
    eng = f"{calc} engine output" + (f" as of {v['asOf']}" if v.get("asOf") else "")
    v2 = v.get("v2") or {}
    ps = v.get("perShare") or {}

    def add(key: str, title: str, value: Any, unit: str, method: str, label: str = "estimate") -> None:
        if value is None:
            return
        sheet.add(Fact(key, title, value, unit, label, method, eng))

    add("dcf.perShare", "DCF value per share", ps.get("value"), "USD", str(ps.get("method") or ""))
    add("dcf.vsPrice", "DCF value per share against the last close", _get(ps, "vsPrice", "value"), "ratio",
        str(_get(ps, "vsPrice", "method") or ""), "derived")  # fmt: skip
    dcf = v2.get("dcf") or {}
    if dcf.get("ok"):
        add("dcf.ev", "DCF enterprise value", _get(dcf, "primary", "ev"), "USDm", "kci-v2 DCF")
        add("dcf.tvShare", "Terminal value share of DCF enterprise value", _get(dcf, "primary", "tvShareOfEV"),
            "ratio", "present value of terminal value ÷ DCF enterprise value")  # fmt: skip
    wacc = v2.get("wacc") or {}
    if wacc.get("ok"):
        add("dcf.wacc", "Discount rate (WACC)", wacc.get("value"), "ratio", "kci-v2 WACC", "assumption")
    for sc in ("downside", "base", "upside"):
        r = _get(v2, "scenarios", sc, "result", "returns") or {}
        add(f"lbo.{sc}.irr", f"Buyout IRR, {sc} case", r.get("irr"), "ratio", f"kci-v2 LBO, {sc} scenario")
        add(f"lbo.{sc}.moic", f"Buyout MOIC, {sc} case", r.get("moic"), "x", f"kci-v2 LBO, {sc} scenario")
    entry = _get(v2, "lbo", "entry") or {}
    add("lbo.entryMultiple", "Buyout entry EV / EBITDA (with takeover premium)", entry.get("entryMultiple"), "x",
        "kci-v2 LBO entry", "assumption")  # fmt: skip
    score = v2.get("score") or {}
    add("score.total", "Attractiveness score", score.get("total"), "score", "kci-v2 weighted score")
    for comp in score.get("components") or []:
        if comp.get("score") is not None:
            add(f"score.{comp['key']}", f"Score: {comp.get('label')}", comp.get("score"), "score",
                str(comp.get("why") or ""))  # fmt: skip
    rec = v2.get("rec") or {}
    if rec.get("decision"):
        sheet.add(Fact("lbo.decision", "Buyout engine's decision", str(rec["decision"]), "text", "engine",
                       "kci-v2 recommendation rule", eng))  # fmt: skip
        sheet.add(Fact("lbo.rationale", "Buyout engine's reason", str(rec.get("rationale") or ""), "text", "engine",
                       "kci-v2 recommendation rule", eng))  # fmt: skip
    sheet.warnings += [str(w) for w in v.get("warnings") or [] if str(w) not in sheet.warnings]


def _distress(sheet: FactSheet, d: Mapping[str, Any]) -> None:
    sheet.distress_status = d.get("status")
    ver = str(d.get("screenVersion") or "distress screen")
    if d.get("status") and d.get("status") != "not available":
        sheet.add(Fact("distress.status", "Distress screen result", str(d["status"]), "text", "engine",
                       ver, f"{ver} as of {d.get('asOf')}"))  # fmt: skip
    for chk in d.get("checks") or []:
        k = f"distress.{chk['name']}"
        sheet.add(Fact(k, f"Distress check {chk['name']}: level", str(chk.get("level")), "text", "engine",
                       str(chk.get("thresholds") or ""), f"{ver}: {chk.get('thresholds') or ''}"))  # fmt: skip
        sheet.add(Fact(k + ".reason", f"Distress check {chk['name']}: reason", str(chk.get("reason") or ""), "text",
                       "engine", ver, f"{ver} as of {d.get('asOf')}"))  # fmt: skip
    cr = d.get("credit") or {}
    if cr.get("reason"):
        sheet.add(Fact("credit.backdrop", "Credit market backdrop", str(cr["reason"]), "text", "market",
                       "Baa spread level and 3-month change", f"FRED BAA10Y on {cr.get('day')}"))  # fmt: skip
    sheet.warnings += [str(w) for w in d.get("dataWarnings") or []]
