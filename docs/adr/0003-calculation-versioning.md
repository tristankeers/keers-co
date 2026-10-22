# 3. Calculations are versioned

Date: 2026-10-22. Status: accepted.

## Decision

Every calculation result records the calculation version that produced it. Changing a formula or a default assumption creates a new version, with a golden-output file and a written list of which outputs changed and why.

## Why

Investment conclusions must be reproducible. A silent methodology change makes old results impossible to explain and lets performance be tuned without anyone noticing.

## Consequences

Old versions stay runnable for comparison. `kci-v1` is the first version.
