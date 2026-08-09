# W0 Day-Ahead Oracle

Date: 2026-07-31

## Purpose

The W0 day-ahead oracle measures the operational value of knowing every
collection booking for a day when that day's plan is constructed. It is an
upper-bound comparator for the dynamic W0 baseline, not a deployable policy and
not a whole-week perfect-information solve.

The oracle changes information timing only. Demand, physical service dates,
fleet, route costs, constraints, objective weights, random seed and convergence
settings remain aligned with `W0_baseline`.

## Information rule

For each order, preserve the original `timestamp_created` in
`timestamp_created_original`, then replace `timestamp_created` with 00:00 on its
original booking date. In January and February the source and planner clocks are
both GMT, so no daylight-saving conversion is required.

At the midnight anchor on date `d`:

- every collection-flow order originally booked during `d` is visible because
  visibility uses `created <= epoch`;
- orders booked before `d` remain visible;
- delivery-flow visibility continues to use the existing 18:00 reveal on the
  evening before its service date.

This is daily perfect information. Orders originally booked on a future date
are not visible before that future date's midnight.

## Isolated architecture

The implementation adds two new entry points and does not modify the shared
rolling runner or visibility logic:

1. A preprocessing utility reads the combined January-February enriched
   parquet and writes a separate oracle parquet plus a transformation summary.
2. A dedicated `run_static_oracle` wrapper selects that oracle parquet and
   delegates to the existing rolling runner.

The live combined parquet is never overwritten. The wrapper must fail clearly
if the oracle file or its matching transformation summary is absent.

Restoring a general-purpose `--qargo` override is explicitly out of scope. It
would weaken the single-source dataset protection used by all ordinary runs.

## Decision schedule

The oracle uses:

- one planning anchor per calendar day at 00:00;
- no noon re-optimisation;
- no micro-insertion passes;
- the existing 18:00 close event for trunk sizing and day-close accounting.

The intended command therefore supplies `--epochs 00:00` and
`--micro-every-min 0`.

## Reproducibility metadata

The preprocessing summary records:

- source and output paths;
- source and output row counts;
- W0 start and end dates;
- number of non-null creation timestamps;
- number of timestamps changed;
- number already at midnight;
- a deterministic source-file fingerprint.

The transformed parquet retains all source columns and adds only
`timestamp_created_original`. All columns other than the two timestamp fields
must remain value-identical to the source.

## Verification

Automated tests must demonstrate:

1. creation timestamps are floored to midnight without changing their date;
2. the original timestamp is preserved;
3. row count and unrelated fields remain unchanged;
4. a collection booked at any time during date `d` is visible at `d 00:00` in
   the transformed data but not in the original data;
5. a future day's collection is not revealed early;
6. delivery visibility retains the existing evening-before rule;
7. the dedicated wrapper selects only the oracle parquet and forwards the
   single-anchor/no-micro configuration;
8. the generated W0 oracle dataset passes a full per-day visibility census.

The completed run must additionally report:

- seven midnight planning anchors and no noon anchors;
- zero micro passes;
- zero hard, ledger, temporal, non-anticipation and route-backdating
  violations;
- the same in-universe demand definition as `W0_baseline`.

## Run isolation

The oracle run receives its own output directory, stdout/stderr logs and audit
file. It may reuse the completed W0 lane's postcode and OSRM caches only when no
other W0 process is writing them. It must never use the live C0 lane's cache or
output tree.
