"""Diagnostics for reviewing company-specific lines of non-US filers (read-only; stores nothing permanent).

For each listing it prints the company's own (extension) concepts, and the standard ones that match the lines the
data layer misses (capex, operating profit, D&A, borrowings, interest), with each fact's value and period, and,
for ESEF reports, the text of the face-statement row the fact sits on in the published iXBRL report. That row
text (the company's own label and the printed figures) is what a reviewer checks before a concept is added to
``company_lines.toml``.

Usage: python scripts/foreign_lines_diag.py [SYMBOL ...]   (default: the probe list). Needs SEC_USER_AGENT.
Run in GitHub Actions (the build sandbox cannot reach these hosts).
"""

from __future__ import annotations

import html
import json
import re
import sys
from typing import Any

from keers.data import esef
from keers.data.filers import Filers
from keers.data.http import Fetcher, FetchError
from keers.data.refresh import user_agent
from keers.data.sec import ParseError
from keers.data.store import Store
from keers.data.xbrl import fetch_instance, fetch_linkbase, latest_periodic, parse_labels

LIST = "config/universe/foreign-test.txt"
PATTERN = re.compile(
    r"(?i)(purchase|capital ?expend|capex|invest|acqui|addition|depreci|amorti|impair|borrow|debt|loan|bond|note|"
    r"financ|operating|ebit|interest|lease)"
)
FOREIGN_FORMS = ("20-F", "40-F", "20-F/A", "40-F/A")
MAX_ROWS = 120


def read_list(path: str) -> list[tuple[str, str]]:
    out = []
    for line in open(path).read().splitlines():  # noqa: SIM115 (small config file)
        line = line.split("#", 1)[0].strip()
        if line:
            sym, _, rest = line.partition("|")
            out.append((sym.strip().upper(), rest.strip()))
    return out


def row_text(doc: str, concept: str) -> list[str]:
    """The face-statement rows (table rows) that hold an ix:nonFraction tagged ``concept``, as plain text."""
    rows = []
    for m in re.finditer(r'<ix:nonFraction[^>]*name="' + re.escape(concept) + '"', doc):
        a, b = doc.rfind("<tr", 0, m.start()), doc.find("</tr>", m.end())
        if a < 0 or b < 0 or b - a > 20000:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", " ", doc[a:b]))
        text = " ".join(text.split())
        if text and text not in rows:
            rows.append(text[:300])
    return rows[:3]


def esef_dump(fetcher: Fetcher, filers: Filers, lei: str) -> None:
    found, how = filers.esef.filings(lei)
    latest = esef.latest_per_period(found, 1)
    if not latest:
        print(f"  no readable ESEF report for {lei} ({how}; {len(found)} listed)")
        return
    f = latest[0]
    print(f"  ESEF {f.fxo_id} period {f.period_end} added {f.added}")
    print(f"  json {f.json_url}\n  report {f.viewer_url}")
    doc: dict[str, Any] = json.loads(fetcher.get(f.json_url).body)
    try:
        page = fetcher.get(f.viewer_url).body.decode("utf-8", "replace") if f.viewer_url else ""
    except FetchError as e:
        print(f"  report page error: {e}")
        page = ""
    seen: dict[str, list[str]] = {}
    for body in (doc.get("facts") or {}).values():
        dims = body.get("dimensions") or {}
        c = str(dims.get("concept") or "")
        if set(dims) - esef.PLAIN_DIMENSIONS or not PATTERN.search(c.split(":")[-1]):
            continue
        prefix = c.split(":", 1)[0]
        if prefix == "ifrs-full" and not re.search(
            r"(?i)purchase|depreci|amorti|operating|borrow|interest", c
        ):
            continue
        seen.setdefault(c, []).append(f"{dims.get('period')} {dims.get('unit')} = {body.get('value')}")
    print(f"  -- matching facts without dimensions ({len(seen)} concepts)")
    for c in sorted(seen, key=lambda x: (x.startswith("ifrs-full"), x))[:MAX_ROWS]:
        print(f"    {c}: {'; '.join(seen[c][:4])}")
        for r in row_text(page, c):
            print(f"        row: {r}")


def sec_dump(fetcher: Fetcher, cik: int) -> None:
    subs = json.loads(fetcher.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").body)
    accn, form, filed = latest_periodic(subs, FOREIGN_FORMS)
    inst = fetch_instance(fetcher, cik, accn, form, filed)
    print(f"  {inst.form} {inst.accn} filed {inst.filed}, instance {inst.url}")
    try:
        labels = parse_labels(fetch_linkbase(fetcher, cik, inst.accn, "_lab.xml"))
    except Exception as e:  # noqa: BLE001 (diagnostics: report and carry on)
        print(f"  label linkbase error: {e}")
        labels = {}
    rows = [
        x for x in inst.facts if not x.dims and PATTERN.search(x.concept) and x.prefix not in ("dei", "srt")
    ]
    rows = [
        x for x in rows if x.prefix != "ifrs-full" or re.search(r"(?i)purchase|depreci|amorti", x.concept)
    ]
    print(f"  extension prefixes: {sorted(inst.extension_prefixes)}; matching facts: {len(rows)}")
    for x in sorted(rows, key=lambda x: (x.prefix, x.concept, x.end))[:MAX_ROWS]:
        c = f"{x.prefix}:{x.concept}"
        print(f'    {c} "{labels.get(c, "")}" {x.start or ""}..{x.end} {x.unit} = {x.value:,.0f}')


def main() -> int:
    want = {s.upper() for s in sys.argv[1:]}
    fetcher = Fetcher(user_agent())
    filers = Filers(fetcher, Store(":memory:"))
    for sym, spec in read_list(LIST):
        if want and sym not in want:
            continue
        print(f"\n===== {sym} ({spec})")
        try:
            if spec.startswith("cik="):
                sec_dump(fetcher, int(spec[4:]))
                continue
            if not spec:  # an SEC ticker of a foreign filer
                sec_dump(fetcher, filers.sec.tickers()[sym][0])
                continue
            r = filers.find(sym, spec)
            if not r.get("linked"):
                print(f"  not linked: {r.get('reason')}")
                continue
            esef_dump(fetcher, filers, r["lei"])
        except (FetchError, ParseError, ValueError, KeyError) as e:
            print(f"  error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
