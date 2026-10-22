# 4. Research workbench: FastAPI and plain JavaScript, run locally

Date: 2026-10-22. Status: proposed (for the project owner's review).

## Decision

The first interface is a FastAPI app (`keers.web`) serving a JSON API and a static front end written in plain
JavaScript modules, with no build step. Price charts use TradingView Lightweight Charts (Apache 2.0, vendored
at a pinned version, attribution logo kept on) drawing bars from our own store. It runs on the user's machine
with `python -m keers.web` and binds to 127.0.0.1.

## Why

- **FastAPI**, as the audit suggested. The engines, data layer and screens are Python, so the API is a thin
  layer that calls them directly; every number on screen comes from the same tested code as the command line.
  FastAPI adds request validation and a self-describing API (`/api/docs`) that the planned agents can call too.
- **No React yet.** Five views and one editable form do not need a component framework. Plain modules mean no
  Node toolchain, no `node_modules`, and nothing to rebuild; the Python package ships the files as they are.
  If the interface grows (portfolios, IC memos, collaboration), moving the views to React or Preact is a
  contained change because the API stays the same.
- **Lightweight Charts, not TradingView widgets.** Widgets draw TradingView's data, which cannot be traced to
  a stored response. Lightweight Charts draws our own bars, so the chart and the numbers agree and the source
  is known. Its licence asks for attribution, which the chart logo and the footer give.
- **Local, not public hosting.** Yahoo's data is for personal use, and this repository is public. A public
  site (GitHub Pages, a free cloud tier) would redistribute it. Running locally is free and keeps SEC requests
  under the user's own contact details. A private free option, if needed away from the desk, is a GitHub
  Codespace forwarding the port privately.

## Consequences

- One extra optional dependency group (`pip install -e ".[web]"`: FastAPI and Uvicorn). The core packages keep
  no third-party runtime dependencies.
- Live fetches happen when a page is opened or Refresh is pressed, one at a time, with per-company minimum
  intervals so a browser cannot hammer a source.
- Before any commercial use, Yahoo must be replaced (see docs/data-sources.md) and the hosting decision revisited.
