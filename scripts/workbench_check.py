"""Live check of the research workbench: refresh real companies, call every view, save screenshots.

Run in GitHub Actions (workbench-live.yml), where SEC, Yahoo and FRED are reachable:

    python scripts/workbench_check.py DBX ADBE VOD.L

Writes workbench-out/summary.json and, with --screens, PNG screenshots of each view. Exits non-zero when a
US filer fails to refresh or a view errors; a non-US listing is expected to be prices-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

OUT = Path("workbench-out")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["DBX", "ADBE", "VOD.L"])
    ap.add_argument("--screens", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    os.environ.setdefault("KEERS_DB", str(OUT / "keers.sqlite"))

    from fastapi.testclient import TestClient

    from keers.web.app import create_app

    client = TestClient(create_app())
    summary: dict[str, Any] = {"status": client.get("/api/status").json()}
    failures: list[str] = []
    for sym in a.symbols:
        row: dict[str, Any] = {}
        t = time.monotonic()
        row["refresh"] = client.post(f"/api/companies/{sym}/refresh").json()
        row["refreshSeconds"] = round(time.monotonic() - t, 1)
        c = client.get(f"/api/companies/{sym}").json()
        row["listing"] = c["listing"]
        row["snapshot"] = c["snapshot"]
        row["figures"] = {f["key"]: [f["value"], f["label"], f["stale"]] for f in c["figures"]}
        row["quote"] = client.get(f"/api/companies/{sym}/quote").json()
        v = client.get(f"/api/companies/{sym}/valuation").json()
        row["valuation"] = {"ran": v["ran"], "blockedBy": v.get("blockedBy"),
                            "comparison": v.get("comparison"), "perShare": v.get("perShare")}  # fmt: skip
        d = client.get(f"/api/companies/{sym}/distress").json()
        row["distress"] = {
            "status": d["status"],
            "checks": [(x["name"], x["level"], x["reason"]) for x in d["checks"]],
        }
        for interval in ("1d", "5m"):
            b = client.get(f"/api/companies/{sym}/bars", params={"interval": interval}).json()
            row[f"bars_{interval}"] = {k: b[k] for k in ("source", "last", "stale", "error", "notes")} | {
                "n": len(b["bars"])
            }
        n = client.get(f"/api/companies/{sym}/news").json()
        row["news"] = {"n": len(n["items"]), "error": n["error"]}
        row["search"] = [
            x["symbol"] for x in client.get("/api/search", params={"q": sym}).json()["results"][:5]
        ]
        summary[sym] = row
        is_us = "." not in sym
        if is_us and not row["refresh"]["ok"]:
            failures.append(f"{sym}: refresh failed: {row['refresh']['errors']}")
        if row["bars_1d"]["n"] == 0:
            failures.append(f"{sym}: no daily bars")
        print(f"{sym}: refresh {'ok' if row['refresh']['ok'] else row['refresh']['errors']} in {row['refreshSeconds']}s; "
              f"valuation ran={v['ran']}; distress={d['status']}; daily bars={row['bars_1d']['n']}; "
              f"5m bars={row['bars_5m']['n']}; news={row['news']['n']}")  # fmt: skip
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    if a.screens:
        screens(a.symbols)
    for f in failures:
        print(f"::error::{f}")
    return 1 if failures else 0


def screens(symbols: list[str]) -> None:
    import uvicorn
    from playwright.sync_api import sync_playwright

    from keers.web.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=8777, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
        for sym in symbols:
            for tab in ("overview", "valuation", "distress", "chart", "news"):
                page.goto(f"http://127.0.0.1:8777/#/c/{sym}/{tab}")
                page.wait_for_timeout(2500)
                page.screenshot(path=str(OUT / f"{sym}-{tab}.png"), full_page=True)
        browser.close()
    server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
