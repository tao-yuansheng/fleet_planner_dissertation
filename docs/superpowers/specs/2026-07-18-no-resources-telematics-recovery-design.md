# NO_RESOURCES telematics-recovery probe — design

Date: 2026-07-18
Status: approved (diagnostic-first, isolated output)

## Problem

The planning universe excludes NO_RESOURCES orders — Qargo rows with no powered
fleet vehicle (`resource_rigid`/`tractor`/`van` all empty), currently dropped at
`verify_legs.py::_eligible` and mirrored by the pipeline's NO_FLEET_RESOURCE gate.
These carry a `resource_subcontractor` tag, but many were actually *served*
(887 of 1,310 candidates are `INVOICE_POSTED`). Hypothesis: our own fleet ran
some of them; the vehicle was simply never written onto the order record.

## Goal

Diagnostic only: for each NO_RESOURCES order, ask "did a fleet vehicle demonstrably
stop at the origin (~origin_timestamp) and/or destination (~destination_timestamp)?"
and **report how many recover, at what evidence strength, and to which flow** — so
the finding can be validated before anything is folded into the universe.

## Scope / isolation (hard constraint)

- Non-destructive. Writes ONLY a new `freight_planner/data/no_resources_recovery.csv`
  plus a printed summary.
- Does NOT modify `verified_legs.csv`, `_eligible`, `build_scoped_orders`, or the
  universe. Gated behind `--recover-no-resources`; default run is unchanged.

## Candidate set

Orders where `len(_order_regs(row)) == 0`, `status != CANCELLED`, and transport is
not Specialist Movement / Crane Hire. = 1,310 (Jan+Feb). All have both timestamps.

## Matching (reuse existing calibrated machinery)

- Same stopped-ping index (`build_indexes`), same postcode/coords helpers, same
  windows: COLLECTION ±3h at origin, DELIVERY ±2h at destination, +3h placeholder
  widening (`_window_for`) since ~half of origin stamps are round-hour slots.
- Per order, match at both endpoints via the substitute matcher with an empty
  `exclude_regs` (there is no assigned vehicle). `_any_fleet_at` is extended to
  return ALL matching regs at an endpoint (not just the first) so we can detect the
  SAME vehicle appearing at both ends.

## Reported dimensions (per matched order → CSV columns)

- `tier`: `same_vehicle_both` · `any_both` · `origin_only` · `dest_only` · `no_match`
- `endpoint_quality`: `unique` vs `shared` (match relied on a ≥5-orders/day endpoint;
  the anti-fabrication gate flags, does not drop — reported for validation)
- `implied_leg` (origin→COLLECTION, dest→DELIVERY, both→FULL_FLEET-shape) and
  `api_flow` (`classify_order`: PL_IMPORT/PL_EXPORT/FULL_FLEET/UNKNOWN) so each
  recovery is attributed to a flow.
- Evidence: `matched_regs_origin`, `matched_regs_dest`, `stop_time_origin`,
  `stop_time_dest`, `dist_m_origin`, `dist_m_dest`, plus `status`, `order_id`, `name`.

## Summary printout

- Candidate count; matched vs no_match.
- Counts by `tier`, by `tier × endpoint_quality`, and by `tier × api_flow`.
- Headline: how many of 1,310 recover at each strength, and the fraction of the
  excluded universe that represents.

## Explicitly deferred (needs a further decision)

- Folding recovered orders into `verified_legs.csv` / the universe.
- Any relaxation of the shared-endpoint gate.

## Note

Repo is not under git in this environment, so this spec is not committed.
