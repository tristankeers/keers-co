"""The workbench's HTTP API and static front end (FastAPI).

A thin layer: every route calls one ``Workbench`` method and returns its JSON. No calculation happens here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from keers.data.market import price_sources
from keers.data.refresh import DEFAULT_DB

from .service import InputError, Live, LiveConfig, Workbench

STATIC = Path(__file__).parent / "static"
SYMBOL = r"^[A-Za-z0-9.\-=^]{1,24}$"


def workbench_from_env() -> Workbench:
    """Configuration from the environment, as for the command line. ``KEERS_OFFLINE=1`` turns live fetching off."""
    db = os.environ.get("KEERS_DB", DEFAULT_DB)
    if os.environ.get("KEERS_OFFLINE", "").strip() in ("1", "true", "yes"):
        return Workbench(db)
    cfg = LiveConfig(
        sec_user_agent=os.environ.get("SEC_USER_AGENT", "").strip() or None,
        tiingo_token=os.environ.get("TIINGO_TOKEN", "").strip() or None,
        price_sources=price_sources(os.environ.get("KEERS_PRICE_SOURCES")),
    )
    return Workbench(db, Live(cfg))


def create_app(wb: Workbench | None = None) -> FastAPI:
    wb = wb or workbench_from_env()
    app = FastAPI(title="Keers & Co research workbench", version="0.1", docs_url="/api/docs", redoc_url=None)

    @app.exception_handler(InputError)
    async def _input_error(_: Any, exc: InputError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def sym(s: str) -> str:
        import re

        if not re.match(SYMBOL, s):
            raise HTTPException(422, "not a valid symbol")
        return s

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return wb.status()

    @app.get("/api/search")
    def search(q: str = Query("", max_length=60), limit: int = Query(20, ge=1, le=50)) -> dict[str, Any]:
        return wb.search(q, limit)

    @app.get("/api/companies/{symbol}")
    def company(symbol: str) -> dict[str, Any]:
        return wb.company(sym(symbol))

    @app.post("/api/companies/{symbol}/refresh")
    def refresh(symbol: str) -> dict[str, Any]:
        return wb.refresh(sym(symbol))

    @app.get("/api/companies/{symbol}/quote")
    def quote(symbol: str) -> dict[str, Any]:
        return wb.quote(sym(symbol))

    @app.get("/api/companies/{symbol}/bars")
    def bars(symbol: str, interval: str = Query("1d", max_length=4)) -> dict[str, Any]:
        return wb.bars(sym(symbol), interval)

    @app.get("/api/companies/{symbol}/news")
    def news(symbol: str) -> dict[str, Any]:
        return wb.news(sym(symbol))

    @app.get("/api/companies/{symbol}/valuation")
    def valuation(symbol: str) -> dict[str, Any]:
        return wb.valuation(sym(symbol))

    @app.post("/api/companies/{symbol}/valuation")
    def valuation_custom(symbol: str, edits: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        return wb.valuation(sym(symbol), {k: edits.get(k) or {} for k in ("lbo", "dcf")})

    @app.get("/api/companies/{symbol}/distress")
    def distress(symbol: str) -> dict[str, Any]:
        return wb.distress(sym(symbol))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
