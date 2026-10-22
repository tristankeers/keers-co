# 1. Python modular monolith

Date: 2026-10-22. Status: accepted (chosen by the project owner).

## Decision

One repository and one Python application, split into modules (data, valuation, screens, portfolio, risk, agents). A thin web API and a React front end come later. No microservices, message queues or agent frameworks.

## Why

Live data ingestion, quant screens and backtesting are best served by Python's libraries. One process is the simplest thing that supports the next milestones. Modules keep strategy code, engines and risk separate without the overhead of separate services.

## Consequences

KCI's engine is JavaScript, so it is ported to Python. The original stays in `legacy/kci` as an independent reference, and the port must agree with it (ADR 2).
