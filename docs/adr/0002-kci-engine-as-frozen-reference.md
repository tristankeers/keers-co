# 2. KCI's engine is a frozen reference

Date: 2026-10-22. Status: accepted.

## Decision

KCI's engine, default-assumption code, self-checks and dataset are cut from KCI's published `index.html` at a pinned commit, byte for byte, and never edited. A test re-checks the bytes against the published page. Another test locks in the engine's outputs (`tests/golden/kci_v1.json`).

## Why

KCI's original source and tests could not be recovered. The published page is the only record, and its engine code is readable and runs unchanged in Node.

## Consequences

- The Python port must reproduce `kci-v1` outputs to a relative tolerance of 1e-9 before any fix is applied.
- Fixes found in the audit are made in the Python core as calculation version 2, never in `legacy/`.
