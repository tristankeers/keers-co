"""Live probe of the non-US financials route: GLEIF, filings.xbrl.org, SEC foreign filers, FRED and ECB FX.

Usage: python scripts/foreign_probe.py [--as-of YYYY-MM-DD] [--list FILE]
Needs SEC_USER_AGENT. Run in GitHub Actions (the build sandbox cannot reach these hosts). Writes probe-out/.
Every link it makes is printed with its basis, so a wrong automatic link can be spotted and corrected.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from keers.data import snapshot
from keers.data.filers import Filers
from keers.data.http import Fetcher, FetchError
from keers.data.refresh import refresh, user_agent
from keers.data.sec import ParseError
from keers.data.store import Store
from keers.strategies import distress

DEFAULT_LIST = "config/universe/foreign-test.txt"
SHAPE_URLS = (
    "https://filings.xbrl.org/api/filings?page%5Bsize%5D=1",
    "https://filings.xbrl.org/api/entities?page%5Bsize%5D=1",
)


def read_list(path: str) -> list[tuple[str, str | None]]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            sym, _, rest = line.partition("|")
            out.append((sym.strip().upper(), rest.strip() or None))
    return out


def shape(fetcher: Fetcher) -> dict[str, Any]:
    """The first record of each filings.xbrl.org list, to check the field names the reader expects."""
    out: dict[str, Any] = {}
    for u in SHAPE_URLS:
        try:
            doc = json.loads(fetcher.get(u, headers={"Accept": "application/vnd.api+json"}).body)
            first = (doc.get("data") or [{}])[0]
            out[u] = {"keys": sorted(doc), "attributes": first.get("attributes"),
                      "relationships": first.get("relationships"), "links": doc.get("links")}  # fmt: skip
        except (FetchError, ValueError) as e:
            out[u] = {"error": str(e)}
    return out


# Lines that were missing in the first live check; the probe lists every concept a company filed that looks like one.
DIAG = re.compile(r"(?i)(depreciation|amorti[sz]|purchase.*(property|intangible)|capitalexpend|operating(profit|income)|"
                  r"profitlossfromoperating|borrowing|loan|notespayable|bond|leaseliabilit|weightedaverage|"
                  r"numberofshares|sharesoutstanding|earnings(loss)?pershare|financecost|interestexpense)")  # fmt: skip


def concept_diag(store: Store, cik: int) -> dict[str, Any]:
    """Concepts in the latest stored raw report (ESEF xBRL-JSON or SEC companyfacts) that match ``DIAG``.

    Only names and how often each appears are listed; no figures are read from them.
    """
    ent = store.entity(cik)
    like = f"%/{ent['identifier']}/%.json" if ent else f"%/companyfacts/CIK{cik:010d}.json"
    row = store.db.execute(
        "SELECT url, body FROM raw_documents WHERE url LIKE ? AND body IS NOT NULL ORDER BY url DESC, size DESC "
        "LIMIT 1",
        (like,),
    ).fetchone()
    if row is None:
        stored = store.filings(cik, forms=())
        return {"error": f"no stored report matching {like}",
                "filingsListed": [(f.accn, f.report_date, f.primary_document) for f in stored[-5:]],
                "note": "filings.xbrl.org lists a report only when it has an xBRL-JSON (json_url) and a period end"}  # fmt: skip
    try:
        doc = json.loads(row[1])
    except ValueError:
        return {"url": row[0], "error": "not JSON"}
    names: dict[str, int] = {}
    if "facts" in doc and isinstance(doc["facts"], dict) and "documentInfo" in doc:
        for f in doc["facts"].values():
            dims = (f or {}).get("dimensions") or {}
            c = str(dims.get("concept") or "")
            extra = sorted(k for k in dims if k not in ("concept", "entity", "period", "unit", "language"))
            key = c + (" [dims: " + ",".join(extra) + "]" if extra else "")
            if DIAG.search(c):
                names[key] = names.get(key, 0) + 1
    else:
        for tax, concepts in (doc.get("facts") or {}).items():
            for c, body in concepts.items():
                if DIAG.search(c):
                    ends = [x.get("end", "") for u in (body.get("units") or {}).values() for x in u]
                    names[f"{tax}:{c} (latest {max(ends) if ends else '?'})"] = len(ends)
    return {"url": row[0], "concepts": dict(sorted(names.items()))}


def brief(v: Any) -> Any:
    return None if v is None else {"value": v.value, "unit": v.unit, "method": v.method[:160],
                                   "warnings": list(v.warnings)[:3]}  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--list", default=DEFAULT_LIST)
    a = ap.parse_args()
    fetcher = Fetcher(user_agent())
    store = Store("probe-out/foreign.sqlite")
    Path("probe-out").mkdir(exist_ok=True)
    report: dict[str, Any] = {
        "asOf": a.as_of.isoformat(),
        "apiShape": shape(fetcher),
        "links": {},
        "companies": {},
    }
    print(json.dumps(report["apiShape"], indent=1, default=str)[:4000])
    filers = Filers(fetcher, store)
    items = read_list(a.list)
    for sym, spec in items:
        try:
            if spec and spec.startswith("cik="):
                report["links"][sym] = filers.link_cik(sym, int(spec[4:]))
            elif spec:
                r = filers.find(sym, spec)
                r["candidates"] = r.get("candidates", [])[:5]
                report["links"][sym] = r
        except (FetchError, ParseError, ValueError) as e:
            report["links"][sym] = {"error": str(e)}
        print(sym, json.dumps(report["links"].get(sym), default=str)[:600])
    res = refresh(store, [s for s, _ in items], fetcher, today=a.as_of)
    report["refresh"] = {"status": res["status"], "failed": res["failed"],
                         "done": {k: {kk: vv for kk, vv in v.items() if kk != "prices"} for k, v in res["done"].items()
                                  if not k.startswith("_")}}  # fmt: skip
    print("refresh:", res["status"], json.dumps(res["failed"], indent=1)[:3000])
    for sym, _ in items:
        snap = snapshot.build(store, sym, a.as_of)
        v = snap.values
        ana = snapshot.analyse(snap)
        scr = distress.screen(store, sym, a.as_of)
        row = {
            "name": snap.name, "currency": snap.currency, "priceCurrency": snap.price_currency,
            "cadence": snap.cadence, "source": snap.source, "blocked": snap.blocked_by,
            "warnings": snap.warnings[:12], "valuationRan": ana["ran"], "screen": scr["status"],
            "checks": {c["name"]: [c["level"], c["value"], c["reason"][:120]] for c in scr.get("checks", [])},
            "values": {k: brief(v.get(k)) for k in ("revenue", "ebitda", "netIncome", "capex", "debt", "netDebt",
                                                    "shares", "price", "marketCap", "ev", "fx_USD", "beta")},
        }  # fmt: skip
        report["companies"][sym] = row
        print(
            f"\n== {sym} {snap.name!r} {snap.currency} {snap.cadence} ran={ana['ran']} screen={scr['status']}"
        )
        for b in snap.blocked_by:
            print("   blocked:", b[:220])
        for k in ("revenue", "ebitda", "netDebt", "price", "shares", "marketCap", "ev", "fx_USD"):
            x = v.get(k)
            if x is not None:
                print(f"   {k:10} {x.value!s:>16} {x.unit:8} {x.method[:110]}")
        co = store.company(sym)
        if co is not None and (not ana["ran"] or snap.blocked_by):
            row["conceptDiag"] = concept_diag(store, int(co["cik"]))
            print("   concepts:", json.dumps(row["conceptDiag"], default=str)[:3000])
    Path("probe-out/foreign.json").write_text(json.dumps(report, indent=2, default=str))
    ok = sum(1 for r in report["companies"].values() if r["valuationRan"])
    print(f"\nvaluation ran for {ok} of {len(items)}; screen statuses:",
          {s: r["screen"] for s, r in report["companies"].items()})  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
